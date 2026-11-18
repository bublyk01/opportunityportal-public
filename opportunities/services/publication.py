"""Explicit staff publication, including an opt-in for missing descriptive details."""
from dataclasses import dataclass

from django.core.exceptions import ValidationError
from django.db import transaction

from opportunities.models import ImportedItem, Opportunity
from .importing import EDITABLE_FIELDS
from .source_identity import normalize_source_identity


@dataclass(frozen=True)
class PublicationResult:
    opportunity: Opportunity
    created: bool = False
    published: bool = False
    reason: str = "published"


def _validate_override(value):
    if not isinstance(value, bool):
        raise ValidationError("The missing-details publication option must be explicitly true or false.")


def _validate_import_identity(item):
    try:
        normalize_source_identity(item.source, item.source_key, item.external_id, item.source_url)
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc


def _has_source_warnings(item):
    payload = item.source_payload
    raw_data = payload.get("raw_data", {}) if isinstance(payload, dict) else {}
    return bool(raw_data.get("warnings")) if isinstance(raw_data, dict) else False


def _retain_review(item, opportunity):
    if (opportunity.has_missing_details or _has_source_warnings(item)) and not item.review_required:
        item.review_required = True
        item.save(update_fields=["review_required"])


def _skip_result(opportunity):
    if opportunity.status == Opportunity.Status.PUBLISHED:
        return PublicationResult(opportunity, reason="already_published")
    if opportunity.status == Opportunity.Status.ARCHIVED:
        return PublicationResult(opportunity, reason="archived")
    return None


def _prepare_publication(opportunity, *, allow_missing_details):
    if opportunity.has_missing_details:
        if not allow_missing_details:
            raise ValidationError({
                field: "This field cannot be blank."
                for field in ("organization", "type")
                if getattr(opportunity, field) is None
                or (isinstance(getattr(opportunity, field), str) and not getattr(opportunity, field).strip())
            })
        opportunity.allow_missing_details = True
    opportunity.status = Opportunity.Status.PUBLISHED
    opportunity.full_clean()


def _publish_locked(opportunity, *, allow_missing_details, item=None):
    skipped = _skip_result(opportunity)
    if skipped is not None:
        return skipped
    if opportunity.status != Opportunity.Status.DRAFT:
        raise ValidationError({"status": "Only draft opportunities can be published."})
    if item is not None:
        # A linked opportunity owns its edited fields. Validate source identity
        # without letting unrelated staging fields replace or invalidate them.
        _validate_import_identity(item)
    _prepare_publication(opportunity, allow_missing_details=allow_missing_details)
    opportunity.save(update_fields=["status", "allow_missing_details"])
    if item is not None:
        _retain_review(item, opportunity)
    return PublicationResult(opportunity, published=True)


@transaction.atomic
def publish_imported_item(item_id, *, allow_missing_details=False):
    """Publish the linked staff draft or create one published record from staging."""
    _validate_override(allow_missing_details)
    item = ImportedItem.objects.select_for_update().get(pk=item_id)
    if item.opportunity_id:
        opportunity = Opportunity.objects.select_for_update().get(pk=item.opportunity_id)
        return _publish_locked(opportunity, allow_missing_details=allow_missing_details, item=item)

    item.full_clean()
    opportunity = Opportunity(
        **{field: getattr(item, field) for field in EDITABLE_FIELDS},
        source=item.source, source_url=item.source_url,
    )
    _prepare_publication(opportunity, allow_missing_details=allow_missing_details)
    opportunity.save()
    item.opportunity = opportunity
    if opportunity.has_missing_details or _has_source_warnings(item):
        item.review_required = True
    item.save(update_fields=["opportunity", "review_required"])
    return PublicationResult(opportunity, created=True, published=True)


@transaction.atomic
def publish_opportunity(opportunity_id, *, allow_missing_details=False):
    """Publish a draft without replacing its fields with an imported snapshot."""
    _validate_override(allow_missing_details)
    # Match the import-first lock order used by publication and draft promotion.
    item = ImportedItem.objects.select_for_update().filter(opportunity_id=opportunity_id).first()
    opportunity = Opportunity.objects.select_for_update().get(pk=opportunity_id)
    return _publish_locked(opportunity, allow_missing_details=allow_missing_details, item=item)
