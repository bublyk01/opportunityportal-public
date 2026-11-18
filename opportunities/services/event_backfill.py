"""Add event-format metadata to saved records without fetching source messages."""
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.db import transaction

from opportunities.models import ImportedItem, Opportunity
from .event_classification import classify_event_type


def classify_saved_events(*, apply=False):
    """Fill only blank formats; preserve every existing editorial field/status."""
    report = {
        "mode": "apply" if apply else "preview", "opportunities_updated": 0,
        "imports_updated": 0, "unclassified": 0, "errors": [], "details": [],
    }
    for model, counter in (
        (Opportunity, "opportunities_updated"), (ImportedItem, "imports_updated"),
    ):
        ids = list(model.objects.filter(type=Opportunity.Type.EVENT, event_type="").values_list("pk", flat=True))
        for record_id in ids:
            try:
                with transaction.atomic():
                    record = model.objects.select_for_update().get(pk=record_id)
                    if record.type != Opportunity.Type.EVENT or record.event_type:
                        continue
                    event_type = classify_event_type(record.title, record.description)
                    if not event_type:
                        report["unclassified"] += 1
                        continue
                    record.event_type = event_type
                    record.full_clean()
                    if apply:
                        changed = model.objects.filter(
                            pk=record.pk, type=Opportunity.Type.EVENT, event_type="",
                            title=record.title, description=record.description,
                        ).update(event_type=event_type)
                        if not changed:
                            report["errors"].append(f"{model._meta.verbose_name} {record.pk}: changed concurrently; retry.")
                            continue
                    report[counter] += 1
                    report["details"].append({
                        "model": model._meta.model_name, "id": record.pk,
                        "title": record.title, "event_type": event_type,
                    })
            except ValidationError as exc:
                report["errors"].append(f"{model._meta.verbose_name} {record_id}: {'; '.join(exc.messages)}")
            except ObjectDoesNotExist:
                report["errors"].append(f"{model._meta.verbose_name} {record_id}: deleted concurrently; skipped.")
    return report
