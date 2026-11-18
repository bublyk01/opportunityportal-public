from io import StringIO
import json

from django.core.management import call_command
from django.test import TestCase

from opportunities.models import ImportedItem, Opportunity
from opportunities.services.event_backfill import classify_saved_events
from opportunities.services.importing import import_candidates


class EventBackfillTests(TestCase):
    def event(self, **overrides):
        values = {
            "title": "Department of Mathematics Seminar: Random walks",
            "organization": "Example University", "type": "event",
            "description": "A scientific seminar for students.",
        }
        values.update(overrides)
        return Opportunity.objects.create(**values)

    def test_preview_does_not_change_saved_records(self):
        record = self.event(status="published")
        before = Opportunity.objects.values().get(pk=record.pk)
        report = classify_saved_events()
        self.assertEqual(report["opportunities_updated"], 1)
        self.assertEqual(Opportunity.objects.values().get(pk=record.pk), before)

    def test_apply_adds_only_format_to_published_record(self):
        record = self.event(status="published", source="slack", source_url="https://example.org/source")
        before = Opportunity.objects.values().get(pk=record.pk)
        report = classify_saved_events(apply=True)
        after = Opportunity.objects.values().get(pk=record.pk)
        self.assertFalse(report["errors"])
        self.assertEqual(after, {**before, "event_type": "seminar"})

    def test_staff_format_non_events_and_ambiguous_formats_are_preserved(self):
        reviewed = self.event(event_type="other")
        internship = self.event(title="Internship with weekly seminars", type="internship")
        ambiguous = self.event(title="Seminar and workshop", description="")
        before = list(Opportunity.objects.values())
        report = classify_saved_events(apply=True)
        self.assertEqual(report["opportunities_updated"], 0)
        self.assertEqual(list(Opportunity.objects.values()), before)
        self.assertEqual(report["unclassified"], 1)

    def test_import_metadata_and_linked_draft_are_classified_independently(self):
        candidate = {
            "external_id": "a" * 32,
            "source_url": "https://notion.example.invalid/" + "a" * 32,
            "title": "Research seminar", "organization": "Example University", "type": "event",
            "description": "A research seminar.", "raw_data": {"warnings": ["Check source"]},
        }
        import_candidates([candidate], source_key="example-opportunities")
        item = ImportedItem.objects.get()
        # Simulate a snapshot saved before this field existed.
        ImportedItem.objects.filter(pk=item.pk).update(event_type="", notes="Reviewer note")
        Opportunity.objects.filter(pk=item.opportunity_id).update(event_type="", status="archived")
        before = ImportedItem.objects.values().get(pk=item.pk)
        before_draft = Opportunity.objects.values().get(pk=item.opportunity_id)
        report = classify_saved_events(apply=True)
        self.assertEqual((report["imports_updated"], report["opportunities_updated"]), (1, 1))
        self.assertEqual(ImportedItem.objects.values().get(pk=item.pk), {**before, "event_type": "seminar"})
        self.assertEqual(Opportunity.objects.values().get(pk=item.opportunity_id), {**before_draft, "event_type": "seminar"})

    def test_backfill_is_idempotent_and_json_preview_is_machine_readable(self):
        record = self.event()
        stdout = StringIO()
        call_command("classify_events", "--json", stdout=stdout)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["mode"], "preview")
        record.refresh_from_db()
        self.assertEqual(record.event_type, "")
        self.assertEqual(classify_saved_events(apply=True)["opportunities_updated"], 1)
        self.assertEqual(classify_saved_events(apply=True)["opportunities_updated"], 0)

    def test_one_invalid_record_does_not_prevent_valid_event_classification(self):
        valid = self.event()
        invalid = self.event(organization="")
        report = classify_saved_events(apply=True)
        valid.refresh_from_db()
        invalid.refresh_from_db()
        self.assertEqual(valid.event_type, "seminar")
        self.assertEqual(invalid.event_type, "")
        self.assertEqual(report["opportunities_updated"], 1)
        self.assertEqual(len(report["errors"]), 1)
