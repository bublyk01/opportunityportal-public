"""Persist validated public-source snapshots; publication stays in Admin."""
import hashlib
import json
from datetime import date

from django.core.exceptions import ValidationError
from django.core.serializers.json import DjangoJSONEncoder
from django.db import IntegrityError, transaction
from django.utils import timezone

from opportunities.models import ImportedItem, Opportunity
from .source_identity import normalize_source_identity


EDITABLE_FIELDS = (
    "title", "organization", "type", "event_type", "employment_type", "work_mode",
    "description", "location", "deadline",
)


def _normalized_candidate(candidate, *, source=Opportunity.Source.NOTION, source_key=None):
    if not isinstance(candidate, dict):
        raise ValidationError("Expected a listing object.")
    external_id, source_url = normalize_source_identity(
        source, source_key, candidate.get("external_id"), candidate.get("source_url"),
    )
    fields = {}
    for name in EDITABLE_FIELDS:
        value = candidate.get(name)
        if name == "deadline":
            if value in (None, ""):
                value = None
            elif isinstance(value, str):
                value = date.fromisoformat(value)
            elif type(value) is not date:
                raise ValidationError("Deadline must be an explicit date.")
        else:
            if value is None:
                value = ""
            if not isinstance(value, str):
                raise ValidationError(f"{name} must be text.")
            value = value.strip()
        fields[name] = value
    if fields["type"] == Opportunity.Type.EVENT and not fields["event_type"]:
        from .event_classification import classify_event_type

        fields["event_type"] = classify_event_type(fields["title"], fields["description"])
    raw_data = candidate.get("raw_data", {})
    if not isinstance(raw_data, dict):
        raise ValidationError("raw_data must be a listing object.")
    payload = {
        **fields, "external_id": external_id, "source_url": source_url,
        "raw_data": raw_data,
    }
    serialized = json.dumps(payload, cls=DjangoJSONEncoder, sort_keys=True, ensure_ascii=False)
    return fields, json.loads(serialized), hashlib.sha256(serialized.encode("utf-8")).hexdigest()


@transaction.atomic
def promote_imported_item(item_id):
    """Create one draft after validation, without ever updating a linked entry."""
    item = ImportedItem.objects.select_for_update().get(pk=item_id)
    if item.opportunity_id:
        return None
    item.full_clean()
    opportunity = Opportunity(
        **{name: getattr(item, name) for name in EDITABLE_FIELDS},
        source=item.source,
        source_url=item.source_url,
        status=Opportunity.Status.DRAFT,
    )
    opportunity.full_clean()
    opportunity.save()
    item.opportunity = opportunity
    item.review_required = False
    item.save(update_fields=["opportunity", "review_required"])
    return opportunity


def import_candidates(candidates, *, source_key, source=Opportunity.Source.NOTION):
    """Import each validated listing once, preserving all reviewer corrections."""
    report = {key: 0 for key in ("created", "staged", "unchanged", "changed", "errors")}
    report["messages"] = []
    if not isinstance(source_key, str) or not source_key.strip() or len(source_key) > 64:
        raise ValueError("source_key must be nonempty text of at most 64 characters.")
    source_key = source_key.strip()

    for candidate in candidates:
        try:
            fields, payload, source_hash = _normalized_candidate(
                candidate, source=source, source_key=source_key,
            )
            with transaction.atomic():
                item = ImportedItem.objects.select_for_update().filter(
                    source_key=source_key, external_id=payload["external_id"]
                ).first()
                if item is not None:
                    if item.source != source or item.source_url != payload["source_url"]:
                        raise ValidationError("An existing import's source identity cannot be changed.")
                    item.last_seen = timezone.now()
                    if item.source_hash == source_hash:
                        item.save(update_fields=["last_seen"])
                        report["unchanged"] += 1
                    else:
                        # Never replace staff edits, even on an unlinked staging item.
                        item.source_payload = payload
                        item.source_hash = source_hash
                        item.review_required = True
                        item.save(update_fields=[
                            "source_payload", "source_hash", "last_seen", "review_required",
                        ])
                        report["changed"] += 1
                    continue

                raw_warnings = payload["raw_data"].get("warnings", [])
                item = ImportedItem(
                    source=source,
                    source_key=source_key,
                    external_id=payload["external_id"],
                    source_url=payload["source_url"],
                    source_payload=payload,
                    source_hash=source_hash,
                    notes="\n".join(str(warning) for warning in raw_warnings),
                    **fields,
                )
                item.full_clean()
                item.save()
                try:
                    opportunity = promote_imported_item(item.pk)
                except ValidationError as exc:
                    details = "; ".join(
                        f"{field}: {', '.join(errors)}"
                        for field, errors in exc.message_dict.items()
                    ) if hasattr(exc, "message_dict") else "; ".join(exc.messages)
                    item.notes = "\n".join(filter(None, [item.notes, details]))
                    item.save(update_fields=["notes"])
                    report["staged"] += 1
                else:
                    if opportunity is not None:
                        if raw_warnings:
                            item.review_required = True
                            item.save(update_fields=["review_required"])
                        report["created"] += 1
        except (ValidationError, ValueError, TypeError, IntegrityError) as exc:
            report["errors"] += 1
            identifier = candidate.get("external_id", "unknown") if isinstance(candidate, dict) else "unknown"
            detail = "; ".join(exc.messages) if isinstance(exc, ValidationError) else str(exc)
            report["messages"].append(f"{identifier}: {detail}")
    return report
