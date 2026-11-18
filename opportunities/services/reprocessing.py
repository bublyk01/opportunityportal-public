"""Repair saved extraction results without fetching or replacing reviewer edits."""
from copy import deepcopy
from datetime import date

from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.db import OperationalError, transaction

from opportunities.models import ImportedItem, Opportunity
from .importing import EDITABLE_FIELDS, _normalized_candidate, promote_imported_item
from .notion_public import SOURCE_KEY, canonical_page_id


NEW_FIELDS = {"employment_type", "work_mode", "event_type"}
REQUIRED_FIELDS = {"title", "organization", "type"}
MISSING = object()


def _baseline(payload, field):
    value = payload.get(field, MISSING)
    if field == "deadline" and isinstance(value, str):
        try:
            return date.fromisoformat(value) if value else None
        except ValueError:
            return MISSING
    return value


def _merge(instance, fields, baseline, *, required=False):
    changes, preserved, blocked = {}, [], []
    for field, proposed in fields.items():
        current = getattr(instance, field)
        if current == proposed:
            continue
        previous = _baseline(baseline, field)
        untouched = previous is not MISSING and current == previous
        newly_added = previous is MISSING and field in NEW_FIELDS and not current
        if not (untouched or newly_added):
            preserved.append(field)
        elif required and field in REQUIRED_FIELDS and not proposed:
            blocked.append(field)
        else:
            changes[field] = proposed
            setattr(instance, field, proposed)
    return changes, preserved, blocked


def _warnings(payload):
    raw = payload.get("raw_data", {})
    values = raw.get("warnings", []) if isinstance(raw, dict) else []
    return [str(value) for value in values] if isinstance(values, list) else []


def _refreshed_notes(notes, old_payload, new_payload, item):
    old_warnings = set(_warnings(old_payload))
    lines = []
    for line in notes.splitlines():
        if line in old_warnings:
            continue
        kept = []
        for part in line.split("; "):
            resolved = any(
                part == f"{field}: This field cannot be blank." and getattr(item, field)
                for field in REQUIRED_FIELDS
            )
            if not resolved:
                kept.append(part)
        if kept:
            lines.append("; ".join(kept))
    for warning in _warnings(new_payload):
        if warning not in lines:
            lines.append(warning)
    return "\n".join(lines)


def _error_text(exc):
    return "; ".join(exc.messages) if isinstance(exc, ValidationError) else str(exc)


def _process(item, *, expected_hash, apply, candidate=None):
    from .notion_public import candidate_from_snapshot

    if item.source_hash != expected_hash:
        raise ValueError("The source snapshot changed concurrently; retry this item.")
    if not isinstance(item.source_payload, dict):
        raise ValueError("The saved source snapshot is not a listing object.")
    old_payload = deepcopy(item.source_payload)
    if candidate is None:
        candidate = candidate_from_snapshot(old_payload)
    fields, payload, source_hash = _normalized_candidate(candidate)
    if payload["external_id"] != item.external_id or payload["source_url"] != item.source_url:
        raise ValueError("Reprocessing must not change the saved source identity.")

    original_fields = {field: getattr(item, field) for field in EDITABLE_FIELDS}
    original_notes, original_review = item.notes, item.review_required
    original_opportunity_id = item.opportunity_id
    item_changes, preserved, _ = _merge(item, fields, old_payload)
    item.source_payload, item.source_hash = payload, source_hash
    item.notes = _refreshed_notes(original_notes, old_payload, payload, item)
    item.full_clean()

    draft_changes, draft_preserved, blocked = {}, [], []
    opportunity = None
    protected = False
    create_draft = False
    old_draft_fields = {}
    if item.opportunity_id:
        opportunity = Opportunity.objects.select_for_update().get(pk=item.opportunity_id)
        if opportunity.status != Opportunity.Status.DRAFT:
            protected = True
        else:
            old_draft_fields = {
                field: getattr(opportunity, field) for field in EDITABLE_FIELDS
            }
            draft_changes, draft_preserved, blocked = _merge(
                opportunity, fields, old_payload, required=True
            )
            opportunity.full_clean()
    elif all(getattr(item, field) for field in REQUIRED_FIELDS):
        opportunity = Opportunity(
            **{field: getattr(item, field) for field in EDITABLE_FIELDS},
            source=Opportunity.Source.NOTION,
            source_url=item.source_url,
            status=Opportunity.Status.DRAFT,
        )
        opportunity.full_clean()
        create_draft = True

    unresolved = (
        bool(preserved or draft_preserved or blocked or _warnings(payload))
        or not all(getattr(item, field) for field in REQUIRED_FIELDS)
        or protected
    )
    if unresolved:
        item.review_required = True
    elif create_draft:
        item.review_required = False
    updates = {**item_changes, "source_payload": payload, "source_hash": source_hash}
    if item.notes != original_notes:
        updates["notes"] = item.notes
    if item.review_required != original_review:
        updates["review_required"] = item.review_required
    item_updated = bool(
        item_changes or source_hash != expected_hash or item.notes != original_notes
        or item.review_required != original_review
    )
    detail = {
        "id": item.pk,
        "title": item.title,
        "changes": {
            field: {"before": original_fields[field], "after": value}
            for field, value in item_changes.items()
        },
        "draft_changes": {
            field: {"before": old_draft_fields[field], "after": value}
            for field, value in draft_changes.items()
        },
        "preserved_fields": preserved,
        "preserved_draft_fields": draft_preserved,
        "required_draft_fields_preserved": blocked,
        "protected": protected,
        "draft_created": create_draft,
        "needs_review": unresolved,
    }
    if apply:
        # The predicates protect staff edits on SQLite too, where
        # select_for_update() alone does not acquire a row lock.
        guard = {
            "pk": item.pk, "source_hash": expected_hash, **original_fields,
            "notes": original_notes, "review_required": original_review,
            "opportunity_id": original_opportunity_id,
        }
        if ImportedItem.objects.filter(**guard).update(**updates) != 1:
            raise ValueError("The imported item changed concurrently; retry this item.")
        if draft_changes:
            updated = Opportunity.objects.filter(
                pk=opportunity.pk, status=Opportunity.Status.DRAFT, **old_draft_fields
            ).update(**draft_changes)
            if updated != 1:
                raise ValueError("The linked draft changed concurrently; retry this item.")
        if create_draft:
            promote_imported_item(item.pk)
            if unresolved:
                ImportedItem.objects.filter(pk=item.pk).update(review_required=True)
    return detail, item_updated


def reprocess_saved_items(*, apply=False, source_key=SOURCE_KEY, candidates=None):
    """Repair existing records using saved snapshots or fresh candidate data.

    Fresh candidates are a list, or an external-ID-to-candidate mapping. They
    are merged against the OLD stored snapshot before it is replaced. Unknown
    IDs are not imported, and existing records absent from the batch are left
    alone. This function itself never fetches source content.
    """
    report = {
        key: 0 for key in (
            "items_updated", "drafts_updated", "drafts_created", "preserved_fields",
            "protected", "errors",
        )
    }
    report.update(mode="apply" if apply else "preview", details=[])
    fresh = None
    if candidates is not None:
        fresh = {}
        entries = candidates.items() if isinstance(candidates, dict) else (
            (None, candidate) for candidate in candidates
        )
        for supplied_id, candidate in entries:
            try:
                if not isinstance(candidate, dict):
                    raise ValueError("Expected a fresh listing object.")
                external_id = canonical_page_id(candidate.get("external_id"))
                if supplied_id is not None and canonical_page_id(supplied_id) != external_id:
                    raise ValueError("Fresh candidate map key and external ID do not match.")
                if external_id in fresh:
                    raise ValueError("The fresh batch contains a duplicate external ID.")
                fresh[external_id] = candidate
            except (ValueError, TypeError) as exc:
                report["errors"] += 1
                report["details"].append({"external_id": supplied_id, "error": str(exc)})
    queryset = ImportedItem.objects.filter(source_key=source_key)
    if fresh is not None:
        queryset = queryset.filter(external_id__in=fresh)
    snapshots = list(queryset.values_list("pk", "source_hash", "external_id"))
    for item_id, expected_hash, external_id in snapshots:
        try:
            with transaction.atomic():
                item = ImportedItem.objects.select_for_update().get(pk=item_id)
                detail, item_updated = _process(
                    item, expected_hash=expected_hash, apply=apply,
                    candidate=fresh[external_id] if fresh is not None else None,
                )
        except (ValidationError, ValueError, TypeError, OperationalError, ObjectDoesNotExist) as exc:
            report["errors"] += 1
            report["details"].append({"id": item_id, "error": _error_text(exc)})
            continue
        report["items_updated"] += int(item_updated)
        report["drafts_updated"] += int(bool(detail["draft_changes"]))
        report["drafts_created"] += int(detail["draft_created"])
        report["preserved_fields"] += len(detail["preserved_fields"]) + len(detail["preserved_draft_fields"])
        report["protected"] += int(detail["protected"])
        report["details"].append(detail)
    return report
