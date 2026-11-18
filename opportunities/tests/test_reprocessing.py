from copy import deepcopy
from datetime import date
from io import StringIO
import json
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import OperationalError
from django.db.models.query import QuerySet
from django.test import TestCase

from opportunities.models import ImportedItem, Opportunity
from opportunities.services.importing import import_candidates
from opportunities.services.reprocessing import reprocess_saved_items


SOURCE_KEY = "example-opportunities"
PAGE_ID = "a1b2c3d4e5f6478899aabbccddeeff00"
SOURCE_HOST = "notion.example.invalid"


def listing(**overrides):
    value = {
        "external_id": PAGE_ID,
        "source_url": f"https://{SOURCE_HOST}/{PAGE_ID}",
        "title": "Research assistant",
        "organization": "Old employer",
        "type": "job",
        "location": "Remote",
        "deadline": date(2026, 11, 30),
        "description": "Original public description.",
        "raw_data": {"text": "Original public text", "warnings": []},
    }
    value.update(overrides)
    return value


class ReprocessingTests(TestCase):
    def setUp(self):
        self.reconstruct = patch(
            "opportunities.services.notion_public.candidate_from_snapshot", create=True
        ).start()
        self.addCleanup(patch.stopall)
        self.reconstruct.side_effect = self.repaired_candidate

    def repaired_candidate(self, payload):
        return {
            **deepcopy(payload), "organization": "Correct employer", "type": "internship",
            "location": "Example City", "employment_type": "full_time", "work_mode": "remote",
        }

    def create_item(self, **overrides):
        report = import_candidates([listing(**overrides)], source_key=SOURCE_KEY)
        self.assertEqual(report["errors"], 0)
        item = ImportedItem.objects.get(external_id=overrides.get("external_id", PAGE_ID))
        # Simulate records saved before the two separate employment fields existed.
        item.source_payload.pop("employment_type", None)
        item.source_payload.pop("work_mode", None)
        item.source_hash = "a" * 64
        item.save(update_fields=["source_payload", "source_hash"])
        return item

    def test_preview_reports_repairs_without_writing(self):
        item = self.create_item()
        before = ImportedItem.objects.values().get(pk=item.pk)
        before_draft = Opportunity.objects.values().get(pk=item.opportunity_id)

        report = reprocess_saved_items()

        self.assertEqual(report["mode"], "preview")
        self.assertEqual(report["items_updated"], 1)
        self.assertEqual(report["drafts_updated"], 1)
        self.assertEqual(ImportedItem.objects.values().get(pk=item.pk), before)
        self.assertEqual(Opportunity.objects.values().get(pk=item.opportunity_id), before_draft)

    def test_apply_repairs_untouched_fields_and_keeps_identity_and_timestamps(self):
        item = self.create_item()
        identity = (item.source_key, item.external_id, item.source_url, item.first_seen, item.last_seen)
        opportunity_id = item.opportunity_id

        report = reprocess_saved_items(apply=True)
        item.refresh_from_db()
        opportunity = Opportunity.objects.get(pk=opportunity_id)

        self.assertEqual(report["errors"], 0)
        self.assertEqual(report["drafts_updated"], 1)
        for record in (item, opportunity):
            self.assertEqual(record.organization, "Correct employer")
            self.assertEqual(record.type, "internship")
            self.assertEqual(record.location, "Example City")
            self.assertEqual(record.employment_type, "full_time")
            self.assertEqual(record.work_mode, "remote")
            self.assertEqual(record.deadline, date(2026, 11, 30))
        self.assertEqual(
            (item.source_key, item.external_id, item.source_url, item.first_seen, item.last_seen),
            identity,
        )
        self.assertEqual(item.opportunity_id, opportunity_id)
        self.assertEqual(opportunity.status, Opportunity.Status.DRAFT)

    def test_stage_and_linked_draft_staff_edits_are_preserved_independently(self):
        item = self.create_item()
        item.organization = "Stage reviewer correction"
        item.save(update_fields=["organization"])
        Opportunity.objects.filter(pk=item.opportunity_id).update(location="Lviv")

        report = reprocess_saved_items(apply=True)
        item.refresh_from_db()
        draft = item.opportunity

        self.assertEqual(item.organization, "Stage reviewer correction")
        self.assertEqual(item.location, "Example City")
        self.assertEqual(draft.organization, "Correct employer")
        self.assertEqual(draft.location, "Lviv")
        self.assertEqual(report["preserved_fields"], 2)

    def test_published_and_archived_opportunities_are_never_modified(self):
        item = self.create_item()
        for status in (Opportunity.Status.PUBLISHED, Opportunity.Status.ARCHIVED):
            with self.subTest(status=status):
                Opportunity.objects.filter(pk=item.opportunity_id).update(status=status)
                before = Opportunity.objects.values().get(pk=item.opportunity_id)
                report = reprocess_saved_items(apply=True)
                self.assertEqual(report["protected"], 1)
                self.assertEqual(report["drafts_updated"], 0)
                self.assertEqual(Opportunity.objects.values().get(pk=item.opportunity_id), before)

    def test_corrected_complete_staging_creates_one_missing_draft(self):
        item = self.create_item(organization="")
        self.assertIsNone(item.opportunity_id)

        report = reprocess_saved_items(apply=True)
        item.refresh_from_db()
        self.assertEqual(report["drafts_created"], 1)
        self.assertIsNotNone(item.opportunity_id)
        self.assertEqual(item.opportunity.status, Opportunity.Status.DRAFT)
        self.assertEqual(item.opportunity.source, Opportunity.Source.NOTION)

        repeated = reprocess_saved_items(apply=True)
        self.assertEqual(repeated["drafts_created"], 0)
        self.assertEqual(repeated["items_updated"], 0)
        self.assertEqual(repeated["drafts_updated"], 0)
        self.assertEqual(Opportunity.objects.count(), 1)

    def test_required_draft_field_cannot_be_cleared_by_repair(self):
        item = self.create_item()
        self.reconstruct.side_effect = lambda payload: {
            **self.repaired_candidate(payload), "organization": "",
        }
        report = reprocess_saved_items(apply=True)
        item.refresh_from_db()

        self.assertEqual(item.organization, "")
        self.assertEqual(item.opportunity.organization, "Old employer")
        self.assertTrue(item.review_required)
        self.assertIn("organization", report["details"][0]["required_draft_fields_preserved"])

    def test_new_field_with_staff_value_is_preserved_without_old_baseline(self):
        item = self.create_item()
        item.work_mode = "on_site"
        item.save(update_fields=["work_mode"])
        report = reprocess_saved_items(apply=True)
        item.refresh_from_db()

        self.assertEqual(item.work_mode, "on_site")
        self.assertEqual(item.opportunity.work_mode, "remote")
        self.assertIn("work_mode", report["details"][0]["preserved_fields"])

    def test_missing_legacy_baseline_is_not_treated_as_an_untouched_blank(self):
        item = self.create_item(organization="")
        item.source_payload.pop("organization")
        item.save(update_fields=["source_payload"])
        report = reprocess_saved_items(apply=True)
        item.refresh_from_db()

        self.assertEqual(item.organization, "")
        self.assertIsNone(item.opportunity_id)
        self.assertIn("organization", report["details"][0]["preserved_fields"])

    def test_notes_preserve_staff_text_and_refresh_only_exact_generated_messages(self):
        item = self.create_item(organization="")
        item.source_payload["raw_data"]["warnings"] = ["Old parser warning."]
        item.notes = (
            "Staff note: keep this.\nOld parser warning.\n"
            "organization: This field cannot be blank.\n"
            "Reviewer said organization: This field cannot be blank."
        )
        item.save(update_fields=["notes", "source_payload"])
        self.reconstruct.side_effect = lambda payload: {
            **self.repaired_candidate(payload), "raw_data": {"warnings": ["New parser warning."]},
        }

        report = reprocess_saved_items(apply=True)
        item.refresh_from_db()
        self.assertEqual(report["errors"], 0)
        self.assertIn("Staff note: keep this.", item.notes)
        self.assertIn("Reviewer said organization: This field cannot be blank.", item.notes)
        self.assertIn("New parser warning.", item.notes)
        self.assertNotIn("Old parser warning.", item.notes)
        self.assertNotIn("\norganization: This field cannot be blank.", "\n" + item.notes)
        self.assertTrue(item.review_required)

    def test_invalid_candidate_rolls_back_all_changes_for_that_item(self):
        item = self.create_item()
        before = ImportedItem.objects.values().get(pk=item.pk)
        before_draft = Opportunity.objects.values().get(pk=item.opportunity_id)
        self.reconstruct.side_effect = lambda payload: {
            **self.repaired_candidate(payload), "type": "invalid-choice",
        }
        report = reprocess_saved_items(apply=True)

        self.assertEqual(report["errors"], 1)
        self.assertEqual(report["items_updated"], 0)
        self.assertEqual(ImportedItem.objects.values().get(pk=item.pk), before)
        self.assertEqual(Opportunity.objects.values().get(pk=item.opportunity_id), before_draft)

    def test_item_cas_conflict_leaves_both_records_unchanged(self):
        item = self.create_item()
        before = ImportedItem.objects.values().get(pk=item.pk)
        before_draft = Opportunity.objects.values().get(pk=item.opportunity_id)
        real_update = QuerySet.update

        def conflicting_update(queryset, **updates):
            if queryset.model is ImportedItem and "source_hash" in updates:
                return 0
            return real_update(queryset, **updates)

        with patch.object(QuerySet, "update", conflicting_update):
            report = reprocess_saved_items(apply=True)
        self.assertEqual(report["errors"], 1)
        self.assertIn("concurrently", report["details"][0]["error"])
        self.assertEqual(ImportedItem.objects.values().get(pk=item.pk), before)
        self.assertEqual(Opportunity.objects.values().get(pk=item.opportunity_id), before_draft)

    def test_draft_cas_conflict_rolls_back_the_prior_item_update(self):
        item = self.create_item()
        before = ImportedItem.objects.values().get(pk=item.pk)
        real_update = QuerySet.update

        def conflicting_update(queryset, **updates):
            return 0 if queryset.model is Opportunity else real_update(queryset, **updates)

        with patch.object(QuerySet, "update", conflicting_update):
            report = reprocess_saved_items(apply=True)
        self.assertEqual(report["errors"], 1)
        self.assertEqual(ImportedItem.objects.values().get(pk=item.pk), before)

    def test_database_lock_is_reported_without_partial_updates(self):
        item = self.create_item()
        before = ImportedItem.objects.values().get(pk=item.pk)
        with patch.object(QuerySet, "update", side_effect=OperationalError("database is locked")):
            report = reprocess_saved_items(apply=True)
        self.assertEqual(report["errors"], 1)
        self.assertIn("database is locked", report["details"][0]["error"])
        self.assertEqual(ImportedItem.objects.values().get(pk=item.pk), before)

    def test_source_identity_change_is_rejected(self):
        item = self.create_item()
        self.reconstruct.side_effect = lambda payload: {
            **self.repaired_candidate(payload),
            "external_id": "b" * 32, "source_url": f"https://{SOURCE_HOST}/" + "b" * 32,
        }
        report = reprocess_saved_items(apply=True)
        self.assertEqual(report["errors"], 1)
        item.refresh_from_db()
        self.assertEqual(item.external_id, PAGE_ID)
        self.assertEqual(item.source_hash, "a" * 64)

    def test_invalid_snapshot_does_not_stop_valid_sibling(self):
        self.create_item()
        second_id = "b" * 32
        self.create_item(external_id=second_id, source_url=f"https://{SOURCE_HOST}/{second_id}")

        def reconstruct(payload):
            if payload["external_id"] == PAGE_ID:
                raise ValidationError("Snapshot cannot be normalized.")
            return self.repaired_candidate(payload)

        self.reconstruct.side_effect = reconstruct
        report = reprocess_saved_items(apply=True)
        self.assertEqual(report["errors"], 1)
        self.assertEqual(report["items_updated"], 1)
        self.assertEqual(ImportedItem.objects.get(external_id=PAGE_ID).source_hash, "a" * 64)
        self.assertEqual(ImportedItem.objects.get(external_id=second_id).organization, "Correct employer")

    def test_command_defaults_preview_and_serializes_dates(self):
        self.create_item()
        stdout = StringIO()
        call_command("reprocess_notion", "--json", stdout=stdout)
        report = json.loads(stdout.getvalue())
        self.assertEqual(report["mode"], "preview")
        self.assertEqual(ImportedItem.objects.get().organization, "Old employer")

    def test_fresh_candidates_update_description_against_old_snapshot(self):
        item = self.create_item()
        fresh = self.repaired_candidate(item.source_payload)
        fresh["description"] = "Complete fresh public description with all sections."

        report = reprocess_saved_items(apply=True, candidates=[fresh])
        item.refresh_from_db()

        self.reconstruct.assert_not_called()
        self.assertEqual(report["errors"], 0)
        self.assertEqual(item.description, fresh["description"])
        self.assertEqual(item.opportunity.description, fresh["description"])
        self.assertEqual(item.source_payload["description"], fresh["description"])

    def test_fresh_mapping_preserves_staff_description_in_each_record(self):
        item = self.create_item()
        item.description = "Staff corrected stage description."
        item.save(update_fields=["description"])
        Opportunity.objects.filter(pk=item.opportunity_id).update(
            description="Staff corrected draft description."
        )
        fresh = self.repaired_candidate(item.source_payload)
        fresh["description"] = "Expanded source description."

        report = reprocess_saved_items(apply=True, candidates={PAGE_ID: fresh})
        item.refresh_from_db()

        self.assertEqual(item.description, "Staff corrected stage description.")
        self.assertEqual(item.opportunity.description, "Staff corrected draft description.")
        self.assertEqual(item.source_payload["description"], "Expanded source description.")
        self.assertIn("description", report["details"][0]["preserved_fields"])
        self.assertIn("description", report["details"][0]["preserved_draft_fields"])

    def test_fresh_batch_does_not_process_missing_or_create_unknown_records(self):
        item = self.create_item()
        unknown = listing(external_id="b" * 32, source_url=f"https://{SOURCE_HOST}/" + "b" * 32)

        report = reprocess_saved_items(apply=True, candidates=[unknown])
        item.refresh_from_db()

        self.assertEqual(report["items_updated"], 0)
        self.assertEqual(report["drafts_created"], 0)
        self.assertEqual(ImportedItem.objects.count(), 1)
        self.assertEqual(item.source_hash, "a" * 64)

    def test_fresh_candidate_identity_mismatch_is_rejected(self):
        item = self.create_item()
        fresh = self.repaired_candidate(item.source_payload)
        fresh["source_url"] = f"https://{SOURCE_HOST}/" + "b" * 32

        report = reprocess_saved_items(apply=True, candidates=[fresh])
        item.refresh_from_db()

        self.assertEqual(report["errors"], 1)
        self.assertEqual(item.source_hash, "a" * 64)
        self.assertEqual(item.organization, "Old employer")

    def test_command_apply_saves_and_returns_nonzero_for_errors(self):
        self.create_item()
        stdout = StringIO()
        call_command("reprocess_notion", "--apply", "--json", stdout=stdout)
        self.assertEqual(json.loads(stdout.getvalue())["mode"], "apply")
        self.assertEqual(ImportedItem.objects.get().organization, "Correct employer")
        self.reconstruct.side_effect = ValueError("Broken snapshot")
        with self.assertRaisesMessage(CommandError, "Some snapshots could not"):
            call_command("reprocess_notion", "--apply", stdout=StringIO())
