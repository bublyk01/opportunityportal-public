from datetime import date

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models.deletion import ProtectedError
from django.test import TestCase, override_settings
from django.urls import reverse

from opportunities.models import ImportedItem, Opportunity
from opportunities.services.importing import import_candidates, promote_imported_item


SOURCE_KEY = "example-opportunities"
PAGE_ID = "a1b2c3d4e5f6478899aabbccddeeff00"
PAGE_UUID = "a1b2c3d4-e5f6-4788-99aa-bbccddeeff00"
SOURCE_HOST = "notion.example.invalid"


def candidate(**overrides):
    item = {
        "external_id": PAGE_ID,
        "source_url": f"https://{SOURCE_HOST}/{PAGE_ID}",
        "title": "Research internship",
        "organization": "Example University",
        "type": Opportunity.Type.INTERNSHIP,
        "description": "Join the research team.",
        "location": "Example City / Remote",
        "deadline": date(2026, 11, 30),
        "raw_data": {"text": "Original public source text", "properties": {}},
    }
    item.update(overrides)
    return item


class ImportPersistenceTests(TestCase):
    def import_items(self, *items):
        return import_candidates(list(items), source_key=SOURCE_KEY)

    def test_complete_listing_creates_validated_notion_draft(self):
        report = self.import_items(candidate())

        self.assertEqual(report["created"], 1)
        self.assertEqual(report["errors"], 0)
        imported = ImportedItem.objects.get()
        opportunity = Opportunity.objects.get()
        self.assertEqual(imported.opportunity_id, opportunity.pk)
        self.assertEqual(opportunity.status, Opportunity.Status.DRAFT)
        self.assertEqual(opportunity.source, Opportunity.Source.NOTION)
        self.assertEqual(opportunity.organization, "Example University")
        self.assertEqual(opportunity.deadline, date(2026, 11, 30))
        opportunity.full_clean()

    def test_source_cannot_request_publication_or_override_provenance(self):
        report = self.import_items(
            candidate(status="published", source="manual", is_staff=True)
        )
        self.assertEqual(report["created"], 1)
        opportunity = Opportunity.objects.get()
        self.assertEqual(opportunity.status, Opportunity.Status.DRAFT)
        self.assertEqual(opportunity.source, Opportunity.Source.NOTION)

    def test_extraction_warnings_stay_visible_on_automatically_created_drafts(self):
        report = self.import_items(candidate(raw_data={"warnings": ["Verify the deadline."]}))
        self.assertEqual(report["created"], 1)
        item = ImportedItem.objects.get()
        self.assertTrue(item.review_required)
        self.assertIn("Verify the deadline.", item.notes)

    def test_missing_required_opportunity_fields_are_retained_for_review(self):
        for index, field in enumerate(("title", "organization", "type"), start=1):
            external_id = f"{index:032x}"
            report = self.import_items(
                candidate(
                    **{
                        field: "",
                        "external_id": external_id,
                        "source_url": f"https://{SOURCE_HOST}/{external_id}",
                    }
                )
            )
            with self.subTest(field=field):
                self.assertEqual(report["staged"], 1)
                self.assertEqual(report["created"], 0)
                self.assertEqual(report["errors"], 0)
        self.assertEqual(ImportedItem.objects.count(), 3)
        self.assertFalse(Opportunity.objects.exists())

    def test_repeated_source_id_creates_only_one_opportunity(self):
        first = self.import_items(candidate())
        repeated = self.import_items(candidate())

        self.assertEqual(first["created"], 1)
        self.assertEqual(repeated["unchanged"], 1)
        self.assertEqual(repeated["created"], 0)
        self.assertEqual(ImportedItem.objects.count(), 1)
        self.assertEqual(Opportunity.objects.count(), 1)

    def test_dashed_uppercase_source_id_is_same_identity(self):
        self.import_items(candidate())
        report = self.import_items(candidate(external_id=PAGE_UUID.upper()))

        self.assertEqual(report["created"], 0)
        self.assertEqual(ImportedItem.objects.count(), 1)
        self.assertEqual(Opportunity.objects.count(), 1)
        self.assertEqual(ImportedItem.objects.get().external_id, PAGE_ID)

    def test_database_enforces_unique_source_and_external_id(self):
        self.import_items(candidate(organization=""))
        original = ImportedItem.objects.get()
        original.pk = None
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                original.save(force_insert=True)
        self.assertEqual(ImportedItem.objects.count(), 1)

    def test_invalid_candidate_does_not_prevent_valid_sibling_import(self):
        invalid_id = "b" * 32
        report = self.import_items(
            candidate(
                external_id=invalid_id,
                source_url=f"https://example.com/{invalid_id}",
            ),
            candidate(),
        )

        self.assertEqual(report["errors"], 1)
        self.assertEqual(report["created"], 1)
        self.assertTrue(report["messages"])
        self.assertEqual(ImportedItem.objects.count(), 1)

    def test_invalid_ids_dates_choices_and_mismatched_urls_are_rejected(self):
        for overrides in (
            {"external_id": "invalid"},
            {"source_url": f"https://{SOURCE_HOST}/" + "b" * 32},
            {"source_url": f"http://{SOURCE_HOST}/{PAGE_ID}"},
            {"deadline": "not-a-date"},
            {"type": "invalid"},
        ):
            with self.subTest(overrides=overrides):
                report = self.import_items(candidate(**overrides))
                self.assertEqual(report["errors"], 1)
                self.assertEqual(report["created"], 0)
                self.assertFalse(ImportedItem.objects.exists())
                self.assertFalse(Opportunity.objects.exists())

    def test_updated_source_preserves_reviewer_fields_in_staging(self):
        self.import_items(candidate(organization=""))
        imported = ImportedItem.objects.get()
        original_hash = imported.source_hash
        imported.title = "Reviewed title"
        imported.organization = "Reviewed organization"
        imported.notes = "Staff correction"
        imported.save()

        report = self.import_items(
            candidate(title="New source title", organization="Source organization")
        )
        imported.refresh_from_db()

        self.assertEqual(report["changed"], 1)
        self.assertEqual(imported.title, "Reviewed title")
        self.assertEqual(imported.organization, "Reviewed organization")
        self.assertEqual(imported.notes, "Staff correction")
        self.assertNotEqual(imported.source_hash, original_hash)
        self.assertEqual(imported.source_payload["title"], "New source title")
        self.assertTrue(imported.review_required)
        self.assertFalse(Opportunity.objects.exists())

    def test_updated_source_never_overwrites_linked_opportunity(self):
        self.import_items(candidate())
        imported = ImportedItem.objects.get()
        opportunity = imported.opportunity
        for status in Opportunity.Status.values:
            with self.subTest(status=status):
                opportunity.title = "Staff-reviewed title"
                opportunity.organization = "Staff-reviewed employer"
                opportunity.status = status
                opportunity.save()
                report = self.import_items(
                    candidate(title=f"Changed source {status}", organization="New source employer")
                )
                opportunity.refresh_from_db()
                imported.refresh_from_db()
                self.assertEqual(report["changed"], 1)
                self.assertEqual(opportunity.title, "Staff-reviewed title")
                self.assertEqual(opportunity.organization, "Staff-reviewed employer")
                self.assertEqual(opportunity.status, status)
                self.assertEqual(Opportunity.objects.count(), 1)
                self.assertTrue(imported.review_required)

    def test_unchanged_reimport_preserves_staff_corrections(self):
        self.import_items(candidate(organization=""))
        imported = ImportedItem.objects.get()
        imported.organization = "Staff supplied employer"
        imported.save()

        report = self.import_items(candidate(organization=""))
        imported.refresh_from_db()

        self.assertEqual(report["unchanged"], 1)
        self.assertEqual(imported.organization, "Staff supplied employer")
        self.assertFalse(Opportunity.objects.exists())

    def test_corrected_staged_listing_promotes_to_draft_once(self):
        self.import_items(candidate(organization=""))
        imported = ImportedItem.objects.get()
        imported.organization = "Corrected employer"
        imported.save()

        opportunity = promote_imported_item(imported.pk)
        imported.refresh_from_db()
        self.assertEqual(imported.opportunity_id, opportunity.pk)
        self.assertEqual(opportunity.status, Opportunity.Status.DRAFT)
        self.assertEqual(opportunity.source, Opportunity.Source.NOTION)
        self.assertEqual(opportunity.organization, "Corrected employer")

        promote_imported_item(imported.pk)
        self.assertEqual(Opportunity.objects.count(), 1)

    def test_incomplete_staging_cannot_be_promoted(self):
        self.import_items(candidate(organization=""))
        imported = ImportedItem.objects.get()
        with self.assertRaises(ValidationError):
            promote_imported_item(imported.pk)
        imported.refresh_from_db()
        self.assertIsNone(imported.opportunity_id)
        self.assertFalse(Opportunity.objects.exists())

    def test_provenance_protects_linked_opportunity_from_accidental_deletion(self):
        self.import_items(candidate())
        opportunity = Opportunity.objects.get()
        with self.assertRaises(ProtectedError):
            opportunity.delete()
        self.assertEqual(Opportunity.objects.count(), 1)


@override_settings(SECURE_SSL_REDIRECT=False)
class ImportedItemAdminTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        users = get_user_model()
        cls.admin = users.objects.create_superuser(
            username="import-admin", email="imports@example.com", password="test-password"
        )
        cls.editor = users.objects.create_user(username="import-editor", is_staff=True)
        cls.viewer = users.objects.create_user(username="import-viewer", is_staff=True)
        cls.editor.user_permissions.add(
            Permission.objects.get(
                content_type__app_label="opportunities", codename="change_importeditem"
            )
        )
        cls.viewer.user_permissions.add(
            Permission.objects.get(
                content_type__app_label="opportunities", codename="view_importeditem"
            ),
            Permission.objects.get(
                content_type__app_label="opportunities", codename="add_opportunity"
            ),
        )

    def setUp(self):
        import_candidates([candidate(organization="")], source_key=SOURCE_KEY)
        self.item = ImportedItem.objects.get()
        self.item.organization = "Corrected employer"
        self.item.save()
        self.url = reverse("admin:opportunities_importeditem_changelist")

    def action_payload(self):
        return {"action": "create_drafts", "_selected_action": [str(self.item.pk)]}

    def test_anonymous_users_cannot_promote_imported_items(self):
        self.assertEqual(self.client.post(self.url, self.action_payload()).status_code, 302)
        self.assertFalse(Opportunity.objects.exists())

    def test_staging_edit_permission_alone_does_not_allow_opportunity_creation(self):
        self.client.force_login(self.editor)
        self.client.post(self.url, self.action_payload())
        self.assertFalse(Opportunity.objects.exists())

    def test_opportunity_add_permission_with_view_only_staging_cannot_promote(self):
        self.client.force_login(self.viewer)
        self.client.post(self.url, self.action_payload())
        self.assertFalse(Opportunity.objects.exists())

    def test_authorized_admin_can_promote_corrected_imported_item(self):
        self.client.force_login(self.admin)
        response = self.client.post(self.url, self.action_payload())
        self.assertIn(response.status_code, (200, 302))
        self.item.refresh_from_db()
        self.assertIsNotNone(self.item.opportunity_id)
        self.assertEqual(self.item.opportunity.status, Opportunity.Status.DRAFT)

    def test_list_explains_an_uncreated_opportunity_that_is_ready(self):
        self.client.force_login(self.admin)
        response = self.client.get(self.url)

        self.assertContains(response, "Created opportunity")
        self.assertContains(response, "Not created — ready to create draft")

    def test_missing_fields_are_named_in_list_and_detail_guidance(self):
        self.item.organization = ""
        self.item.type = ""
        self.item.save()
        self.client.force_login(self.admin)

        response = self.client.get(self.url)
        self.assertContains(response, "Not created — complete organization, opportunity type")
        response = self.client.get(
            reverse("admin:opportunities_importeditem_change", args=[self.item.pk])
        )
        self.assertContains(response, "Complete these required fields: organization, opportunity type.")
        self.assertContains(response, "Required to create a draft opportunity.")
        self.assertContains(response, "Opportunity type")

    def test_created_opportunity_is_linked_with_its_current_status(self):
        opportunity = promote_imported_item(self.item.pk)
        opportunity.status = Opportunity.Status.PUBLISHED
        opportunity.save()
        self.client.force_login(self.admin)
        response = self.client.get(
            reverse("admin:opportunities_importeditem_change", args=[self.item.pk])
        )

        self.assertContains(
            response, reverse("admin:opportunities_opportunity_change", args=[opportunity.pk])
        )
        self.assertContains(response, "Research internship (Published)")
        self.assertContains(response, "changing this imported item does not update that opportunity")

    def test_bulk_action_aggregates_incomplete_items_with_field_names(self):
        incomplete_id = "b" * 32
        import_candidates(
            [candidate(
                external_id=incomplete_id,
                source_url=f"https://{SOURCE_HOST}/{incomplete_id}",
                organization="",
                type="",
            )],
            source_key=SOURCE_KEY,
        )
        incomplete_item = ImportedItem.objects.get(external_id=incomplete_id)
        self.client.force_login(self.admin)
        response = self.client.post(
            self.url,
            {
                "action": "create_drafts",
                "_selected_action": [str(self.item.pk), str(incomplete_item.pk)],
            },
            follow=True,
        )

        feedback = list(response.context["messages"])
        self.assertEqual(len(feedback), 1)
        self.assertEqual(feedback[0].level, messages.WARNING)
        self.assertIn("Created 1 draft opportunity.", str(feedback[0]))
        self.assertIn("1 item needs corrections", str(feedback[0]))
        self.assertIn("organization: This field cannot be blank.", str(feedback[0]))
        self.assertIn("opportunity type: This field cannot be blank.", str(feedback[0]))
        self.assertEqual(Opportunity.objects.count(), 1)

    def test_already_created_items_are_reported_without_duplicate_drafts(self):
        promote_imported_item(self.item.pk)
        self.client.force_login(self.admin)
        response = self.client.post(self.url, self.action_payload(), follow=True)

        feedback = list(response.context["messages"])
        self.assertEqual(len(feedback), 1)
        self.assertIn("Skipped 1 already-created item.", str(feedback[0]))
        self.assertEqual(Opportunity.objects.count(), 1)

    def test_bulk_validation_messages_escape_untrusted_titles(self):
        self.item.title = "<script>alert(1)</script>"
        self.item.organization = ""
        self.item.save()
        self.client.force_login(self.admin)
        response = self.client.post(self.url, self.action_payload(), follow=True)

        self.assertContains(response, "&lt;script&gt;alert(1)&lt;/script&gt;")
        self.assertNotContains(response, "<script>alert(1)</script>")
        self.assertFalse(Opportunity.objects.exists())

    def test_admin_keeps_opportunity_type_hours_work_mode_and_location_separate(self):
        self.client.force_login(self.admin)
        response = self.client.post(
            reverse("admin:opportunities_importeditem_change", args=[self.item.pk]),
            {
                "title": "DevOps Engineer Internship",
                "organization": "Example Training Academy",
                "type": Opportunity.Type.INTERNSHIP,
                "employment_type": Opportunity.EmploymentType.PART_TIME,
                "work_mode": Opportunity.WorkMode.REMOTE,
                "description": "Learn DevOps with the academy.",
                "location": "Example City",
                "deadline": "2026-11-30",
                "notes": "Reviewed against the source.",
                "review_required": "on",
                "_save": "Save",
            },
        )
        self.assertRedirects(response, self.url)
        self.client.post(self.url, self.action_payload())
        self.item.refresh_from_db()

        opportunity = self.item.opportunity
        self.assertIsNotNone(opportunity)
        self.assertEqual(opportunity.type, Opportunity.Type.INTERNSHIP)
        self.assertEqual(opportunity.employment_type, Opportunity.EmploymentType.PART_TIME)
        self.assertEqual(opportunity.work_mode, Opportunity.WorkMode.REMOTE)
        self.assertEqual(opportunity.location, "Example City")

    def test_admin_escapes_source_text_and_reviewer_fields(self):
        self.item.title = "<script>alert(1)</script>"
        self.item.source_payload = {"text": "<script>source()</script>"}
        self.item.save()
        self.client.force_login(self.admin)
        response = self.client.get(
            reverse("admin:opportunities_importeditem_change", args=[self.item.pk])
        )
        self.assertContains(response, "&lt;script&gt;alert(1)&lt;/script&gt;")
        self.assertNotContains(response, "<script>alert(1)</script>")
        self.assertNotContains(response, "<script>source()</script>")
