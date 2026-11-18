from datetime import date

from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings

from opportunities.models import ImportedItem, Opportunity
from opportunities.services.importing import EDITABLE_FIELDS, import_candidates
from opportunities.services.notion_public import SOURCE_KEY
from opportunities.services.publication import publish_imported_item, publish_opportunity
from opportunities.services.slack_source import CHANNEL_URL, SOURCE_KEY as SLACK_SOURCE_KEY, message_permalink


def candidate(index=1, **overrides):
    external_id = f"{index:032x}"
    values = {
        "external_id": external_id,
        "source_url": f"https://notion.example.invalid/{external_id}",
        "title": "Student internship", "organization": "", "type": "",
        "description": "Full original announcement.",
        "raw_data": {"text": "Original message"},
    }
    values.update(overrides)
    return values


@override_settings(SLACK_CHANNEL_URLS=(CHANNEL_URL,))
class IncompleteOpportunityModelTests(TestCase):
    def opportunity(self, **overrides):
        values = {"title": "Student opportunity", "organization": "Example employer", "type": "job"}
        values.update(overrides)
        return Opportunity(**values)

    def test_normal_model_validation_still_requires_organization_and_type(self):
        for field in ("organization", "type"):
            with self.subTest(field=field), self.assertRaises(ValidationError) as error:
                self.opportunity(**{field: ""}).full_clean()
            self.assertIn(field, error.exception.message_dict)
        self.assertFalse(self.opportunity().allow_missing_details)

    def test_explicit_flag_allows_actual_missing_details_with_computed_labels(self):
        opportunity = self.opportunity(organization="", type="", allow_missing_details=True)
        opportunity.full_clean()
        opportunity.save()
        opportunity.refresh_from_db()
        self.assertEqual(opportunity.organization, "")
        self.assertEqual(opportunity.type, "")
        self.assertEqual(opportunity.missing_details, ("organization", "opportunity type"))
        self.assertTrue(opportunity.has_missing_details)
        self.assertFalse(self.opportunity().has_missing_details)
        self.assertEqual(self.opportunity(organization="  ").missing_details, ("organization",))

    def test_flag_cannot_bypass_title_choices_event_consistency_lengths_or_url_validation(self):
        for overrides in (
            {"title": ""}, {"title": "   "}, {"title": "x" * 201},
            {"organization": "x" * 201}, {"type": "scholarship"},
            {"event_type": "seminar"}, {"work_mode": "anywhere"},
            {"employment_type": "flexible"}, {"source_url": "javascript:alert(1)"},
            {"source_url": "ftp://example.org/job"}, {"status": "invisible"},
        ):
            with self.subTest(overrides=overrides), self.assertRaises(ValidationError):
                self.opportunity(allow_missing_details=True, **overrides).full_clean()

    def test_imported_source_cannot_enable_override_or_publish_automatically(self):
        self.assertNotIn("allow_missing_details", EDITABLE_FIELDS)
        report = import_candidates(
            [candidate(allow_missing_details=True, status="published")], source_key=SOURCE_KEY,
        )
        self.assertEqual(report["staged"], 1)
        self.assertFalse(Opportunity.objects.exists())
        self.assertNotIn("allow_missing_details", ImportedItem.objects.get().source_payload)
        report = import_candidates(
            [candidate(2, organization="Example employer", type="job", allow_missing_details=True, status="published")],
            source_key=SOURCE_KEY,
        )
        self.assertEqual(report["created"], 1)
        opportunity = Opportunity.objects.get()
        self.assertEqual(opportunity.status, Opportunity.Status.DRAFT)
        self.assertFalse(opportunity.allow_missing_details)


@override_settings(SLACK_CHANNEL_URLS=(CHANNEL_URL,))
class IncompletePublicationServiceTests(TestCase):
    def staged(self, **overrides):
        report = import_candidates([candidate(**overrides)], source_key=SOURCE_KEY)
        self.assertEqual(report["staged"], 1)
        return ImportedItem.objects.get()

    def linked(self):
        report = import_candidates(
            [candidate(organization="Original employer", type="internship")], source_key=SOURCE_KEY,
        )
        self.assertEqual(report["created"], 1)
        return ImportedItem.objects.select_related("opportunity").get()

    def test_default_publication_rejects_missing_details_without_side_effects(self):
        item = self.staged()
        before = ImportedItem.objects.values().get(pk=item.pk)
        with self.assertRaises(ValidationError) as error:
            publish_imported_item(item.pk)
        self.assertIn("organization", error.exception.message_dict)
        self.assertIn("type", error.exception.message_dict)
        self.assertEqual(ImportedItem.objects.values().get(pk=item.pk), before)
        self.assertFalse(Opportunity.objects.exists())

    def test_explicit_publication_creates_one_linked_record_with_real_blanks_and_keeps_snapshot(self):
        item = self.staged()
        item.notes = "Staff note, retained for review."
        item.review_required = False
        item.save(update_fields=["notes", "review_required"])
        before = ImportedItem.objects.values().get(pk=item.pk)
        result = publish_imported_item(item.pk, allow_missing_details=True)
        opportunity = result.opportunity
        self.assertTrue(result.created)
        self.assertTrue(result.published)
        self.assertEqual(result.reason, "published")
        self.assertEqual(opportunity.status, Opportunity.Status.PUBLISHED)
        self.assertTrue(opportunity.allow_missing_details)
        self.assertEqual((opportunity.organization, opportunity.type), ("", ""))
        self.assertEqual(opportunity.source, Opportunity.Source.NOTION)
        self.assertEqual(opportunity.source_url, item.source_url)
        opportunity.full_clean()
        after = ImportedItem.objects.values().get(pk=item.pk)
        self.assertEqual(after, {**before, "opportunity_id": opportunity.pk, "review_required": True})
        repeated = publish_imported_item(item.pk, allow_missing_details=True)
        self.assertEqual(repeated.opportunity.pk, opportunity.pk)
        self.assertFalse(repeated.created)
        self.assertFalse(repeated.published)
        self.assertEqual(repeated.reason, "already_published")
        self.assertEqual(Opportunity.objects.count(), 1)

    def test_complete_staging_publishes_without_unneeded_override(self):
        item = self.staged()
        item.organization = "Staff supplied employer"
        item.type = Opportunity.Type.JOB
        item.save()
        result = publish_imported_item(item.pk, allow_missing_details=True)
        self.assertTrue(result.created)
        self.assertFalse(result.opportunity.allow_missing_details)
        self.assertFalse(result.opportunity.has_missing_details)

    def test_title_is_always_required_including_whitespace_only_staging(self):
        for title in ("", "   "):
            with self.subTest(title=title):
                report = import_candidates([candidate(title=title)], source_key=SOURCE_KEY)
                self.assertEqual(report["staged"] + report["unchanged"], 1)
                item = ImportedItem.objects.get()
                item.title = title
                item.save(update_fields=["title"])
                with self.assertRaises(ValidationError) as error:
                    publish_imported_item(item.pk, allow_missing_details=True)
                self.assertIn("title", error.exception.message_dict)
                self.assertFalse(Opportunity.objects.exists())
                item.refresh_from_db()
                self.assertIsNone(item.opportunity_id)

    def test_unlinked_invalid_source_identity_or_fields_cannot_be_bypassed(self):
        item = self.staged()
        original = {field: getattr(item, field) for field in (
            "source_url", "external_id", "type", "event_type", "title", "organization",
        )}
        for overrides in (
            {"source_url": "https://example.org/wrong-source"}, {"external_id": "invalid"},
            {"type": "grant"}, {"event_type": "seminar"}, {"title": "x" * 201},
            {"organization": "x" * 201},
        ):
            with self.subTest(overrides=overrides):
                ImportedItem.objects.filter(pk=item.pk).update(**{**original, **overrides})
                with self.assertRaises(ValidationError):
                    publish_imported_item(item.pk, allow_missing_details=True)
                self.assertFalse(Opportunity.objects.exists())

    def test_linked_draft_publishes_staff_values_without_replacing_them_from_import(self):
        item = self.linked()
        opportunity = item.opportunity
        opportunity.title = "Reviewed role"
        opportunity.organization = "Reviewed employer"
        opportunity.type = Opportunity.Type.JOB
        opportunity.description = "Staff description"
        opportunity.location = "Lviv"
        opportunity.deadline = date(2027, 1, 31)
        opportunity.employment_type = Opportunity.EmploymentType.PART_TIME
        opportunity.work_mode = Opportunity.WorkMode.REMOTE
        opportunity.source = Opportunity.Source.OTHER
        opportunity.source_url = "https://example.org/reviewed-job"
        opportunity.save()
        # Stale staging classification must not replace the staff-reviewed draft.
        ImportedItem.objects.filter(pk=item.pk).update(type="", organization="", notes="Reviewer note")
        before = Opportunity.objects.values().get(pk=opportunity.pk)
        before_item = ImportedItem.objects.values().get(pk=item.pk)
        result = publish_imported_item(item.pk, allow_missing_details=True)
        self.assertTrue(result.published)
        self.assertFalse(result.created)
        self.assertEqual(Opportunity.objects.values().get(pk=opportunity.pk), {
            **before, "status": Opportunity.Status.PUBLISHED,
        })
        self.assertEqual(ImportedItem.objects.values().get(pk=item.pk), before_item)

    def test_linked_draft_checks_original_identity_even_when_its_fields_were_corrected(self):
        item = self.linked()
        ImportedItem.objects.filter(pk=item.pk).update(source_url="https://example.org/wrong-source")
        with self.assertRaises(ValidationError):
            publish_imported_item(item.pk, allow_missing_details=True)
        item.opportunity.refresh_from_db()
        self.assertEqual(item.opportunity.status, Opportunity.Status.DRAFT)

    def test_published_and_archived_linked_records_are_unchanged(self):
        item = self.linked()
        for status, reason in ((Opportunity.Status.PUBLISHED, "already_published"), (Opportunity.Status.ARCHIVED, "archived")):
            with self.subTest(status=status):
                Opportunity.objects.filter(pk=item.opportunity_id).update(status=status)
                before = Opportunity.objects.values().get(pk=item.opportunity_id)
                before_item = ImportedItem.objects.values().get(pk=item.pk)
                result = publish_imported_item(item.pk, allow_missing_details=True)
                self.assertEqual(result.reason, reason)
                self.assertEqual(result.opportunity.pk, item.opportunity_id)
                self.assertFalse(result.created)
                self.assertFalse(result.published)
                self.assertEqual(Opportunity.objects.values().get(pk=item.opportunity_id), before)
                self.assertEqual(ImportedItem.objects.values().get(pk=item.pk), before_item)

    def test_opportunity_publication_requires_explicit_current_override_and_keeps_linked_review(self):
        item = self.linked()
        Opportunity.objects.filter(pk=item.opportunity_id).update(organization="", allow_missing_details=True)
        ImportedItem.objects.filter(pk=item.pk).update(review_required=False)
        with self.assertRaises(ValidationError):
            publish_opportunity(item.opportunity_id)
        result = publish_opportunity(item.opportunity_id, allow_missing_details=True)
        self.assertTrue(result.published)
        self.assertFalse(result.created)
        self.assertTrue(result.opportunity.allow_missing_details)
        item.refresh_from_db()
        self.assertTrue(item.review_required)

    def test_opportunity_service_still_validates_urls_choices_and_title(self):
        opportunity = Opportunity.objects.create(title="Role", organization="", type="")
        original = {"title": "Role", "type": "", "source_url": ""}
        for overrides in ({"source_url": "javascript:alert(1)"}, {"type": "grant"}, {"title": "  "}):
            with self.subTest(overrides=overrides):
                Opportunity.objects.filter(pk=opportunity.pk).update(**{**original, **overrides})
                with self.assertRaises(ValidationError):
                    publish_opportunity(opportunity.pk, allow_missing_details=True)
                opportunity.refresh_from_db()
                self.assertEqual(opportunity.status, Opportunity.Status.DRAFT)

    def test_source_warnings_retain_review_without_changing_notes_or_snapshot(self):
        item = self.linked()
        payload = {**item.source_payload, "raw_data": {"warnings": ["Verify original announcement"]}}
        ImportedItem.objects.filter(pk=item.pk).update(source_payload=payload, review_required=False)
        before = ImportedItem.objects.values().get(pk=item.pk)
        publish_opportunity(item.opportunity_id)
        self.assertEqual(ImportedItem.objects.values().get(pk=item.pk), {**before, "review_required": True})

    def test_slack_staging_keeps_slack_identity_when_missing_details_are_allowed(self):
        timestamp = "1794200400.123456"
        report = import_candidates(
            [candidate(external_id=timestamp, source_url=message_permalink(timestamp))],
            source_key=SLACK_SOURCE_KEY, source=Opportunity.Source.SLACK,
        )
        self.assertEqual(report["staged"], 1)
        item = ImportedItem.objects.get()
        result = publish_imported_item(item.pk, allow_missing_details=True)
        self.assertEqual(result.opportunity.source, Opportunity.Source.SLACK)
        self.assertEqual(result.opportunity.source_url, message_permalink(timestamp))

    def test_override_parameter_requires_a_boolean_before_any_mutation(self):
        item = self.staged()
        for value in ("true", 1, None):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                publish_imported_item(item.pk, allow_missing_details=value)
        self.assertFalse(Opportunity.objects.exists())
