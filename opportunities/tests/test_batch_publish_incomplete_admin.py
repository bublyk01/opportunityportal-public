import re
from unittest.mock import patch

from django.contrib import admin
from django.contrib.admin.models import ADDITION, CHANGE, LogEntry
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import PermissionDenied
from django.test import Client, RequestFactory, TestCase, override_settings
from django.urls import reverse

from opportunities.models import ImportedItem, Opportunity


def imported_item(index, **overrides):
    external_id = f"{index:032x}"
    fields = {
        "source": Opportunity.Source.NOTION,
        "source_key": "example-opportunities",
        "external_id": external_id,
        "source_url": f"https://notion.example.invalid/{external_id}",
        "source_payload": {"raw_data": {"text": "Original source", "warnings": []}},
        "source_hash": "a" * 64,
        "title": f"Batch import {index}",
        "organization": "",
        "type": "",
    }
    fields.update(overrides)
    return ImportedItem.objects.create(**fields)


@override_settings(SECURE_SSL_REDIRECT=False)
class PublishIncompleteAdminTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        users = get_user_model()
        cls.publisher = users.objects.create_user(username="batch-publisher", is_staff=True)
        cls.viewer = users.objects.create_user(username="batch-viewer", is_staff=True)
        cls.non_staff = users.objects.create_user(username="batch-student")
        permissions = Permission.objects.filter(
            content_type__app_label="opportunities",
            codename__in=("change_importeditem", "add_opportunity", "change_opportunity"),
        )
        cls.publisher.user_permissions.add(*permissions)
        cls.non_staff.user_permissions.add(*permissions)
        cls.viewer.user_permissions.add(*Permission.objects.filter(
            content_type__app_label="opportunities",
            codename__in=("view_importeditem", "view_opportunity"),
        ))
        cls.first_import = imported_item(1)
        cls.second_import = imported_item(2)
        cls.first_draft = Opportunity.objects.create(title="Batch draft one", organization="", type="")
        cls.second_draft = Opportunity.objects.create(title="Batch draft two", organization="", type="")
        cls.published = Opportunity.objects.create(
            title="Already published", organization="Employer", type=Opportunity.Type.JOB,
            status=Opportunity.Status.PUBLISHED,
        )
        cls.archived = Opportunity.objects.create(
            title="Archived opportunity", organization="Employer", type=Opportunity.Type.JOB,
            status=Opportunity.Status.ARCHIVED,
        )

    def setUp(self):
        self.import_url = reverse("admin:opportunities_importeditem_changelist")
        self.opportunity_url = reverse("admin:opportunities_opportunity_changelist")
        self.client.force_login(self.publisher)

    def action(self, url, name, *items, client=None, **extra):
        return (client or self.client).post(url, {
            "action": name, "_selected_action": [str(item.pk) for item in items], **extra,
        })

    def entries(self, model, pk):
        return LogEntry.objects.filter(
            content_type=ContentType.objects.get_for_model(model), object_id=str(pk),
        ).order_by("pk")

    def test_publish_selected_imports_creates_only_selected_record_with_real_blanks_and_audit(self):
        before_count = Opportunity.objects.count()
        response = self.action(self.import_url, "publish_selected_imports", self.first_import)
        self.assertRedirects(response, self.import_url)
        self.first_import.refresh_from_db()
        self.second_import.refresh_from_db()
        opportunity = self.first_import.opportunity
        self.assertEqual(Opportunity.objects.count(), before_count + 1)
        self.assertIsNone(self.second_import.opportunity_id)
        self.assertEqual(opportunity.status, Opportunity.Status.PUBLISHED)
        self.assertEqual(opportunity.organization, "")
        self.assertEqual(opportunity.type, "")
        self.assertTrue(opportunity.allow_missing_details)
        self.assertEqual(opportunity.source, Opportunity.Source.NOTION)
        self.assertEqual(opportunity.source_url, self.first_import.source_url)
        self.assertEqual(self.first_import.source_hash, "a" * 64)
        self.assertEqual(list(self.entries(Opportunity, opportunity.pk).values_list("action_flag", flat=True)), [ADDITION, CHANGE])
        self.assertEqual(self.entries(ImportedItem, self.first_import.pk).count(), 1)
        self.assertEqual(set(LogEntry.objects.values_list("user_id", flat=True)), {self.publisher.pk})

    def test_import_select_across_publishes_matching_pages_but_not_outside_filter(self):
        outside = imported_item(3, title="Outside selection")
        with patch.object(admin.site._registry[ImportedItem], "list_per_page", 1):
            response = self.action(
                self.import_url + "?q=Batch", "publish_selected_imports", self.first_import,
                select_across="1",
            )
        self.assertEqual(response.status_code, 302)
        for item in (self.first_import, self.second_import):
            item.refresh_from_db()
            self.assertEqual(item.opportunity.status, Opportunity.Status.PUBLISHED)
        outside.refresh_from_db()
        self.assertIsNone(outside.opportunity_id)

    def test_selected_draft_action_only_publishes_selected_drafts_and_audits_changes(self):
        response = self.action(self.opportunity_url, "publish_selected_drafts", self.first_draft)
        self.assertRedirects(response, self.opportunity_url)
        self.first_draft.refresh_from_db()
        self.second_draft.refresh_from_db()
        self.assertEqual(self.first_draft.status, Opportunity.Status.PUBLISHED)
        self.assertTrue(self.first_draft.allow_missing_details)
        self.assertEqual(self.second_draft.status, Opportunity.Status.DRAFT)
        entries = self.entries(Opportunity, self.first_draft.pk)
        self.assertEqual(entries.count(), 1)
        self.assertEqual(entries.get().action_flag, CHANGE)
        self.assertIn("status", entries.get().change_message)
        self.assertIn("allow_missing_details", entries.get().change_message)

    def test_draft_select_across_respects_filter_and_does_not_publish_unmatched_draft(self):
        outside = Opportunity.objects.create(title="Outside selection", organization="", type="")
        response = self.action(
            self.opportunity_url + "?q=Batch", "publish_selected_drafts", self.first_draft,
            select_across="1",
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Opportunity.objects.filter(title__startswith="Batch", status=Opportunity.Status.PUBLISHED).count(), 2)
        outside.refresh_from_db()
        self.assertEqual(outside.status, Opportunity.Status.DRAFT)

    def test_publishing_linked_draft_keeps_staff_corrections_and_creates_no_duplicate(self):
        self.first_draft.title = "Staff corrected title"
        self.first_draft.organization = "Reviewed employer"
        self.first_draft.type = Opportunity.Type.INTERNSHIP
        self.first_draft.save()
        self.first_import.opportunity = self.first_draft
        self.first_import.save()
        count = Opportunity.objects.count()
        response = self.action(self.import_url, "publish_selected_imports", self.first_import)
        self.assertEqual(response.status_code, 302)
        self.first_draft.refresh_from_db()
        self.assertEqual(Opportunity.objects.count(), count)
        self.assertEqual(self.first_draft.title, "Staff corrected title")
        self.assertEqual(self.first_draft.organization, "Reviewed employer")
        self.assertEqual(self.first_draft.type, Opportunity.Type.INTERNSHIP)
        self.assertEqual(self.first_draft.status, Opportunity.Status.PUBLISHED)
        self.assertFalse(self.first_draft.allow_missing_details)
        self.assertEqual(self.entries(Opportunity, self.first_draft.pk).count(), 1)
        self.assertFalse(self.entries(ImportedItem, self.first_import.pk).exists())

    def test_repeated_import_action_is_idempotent_and_does_not_add_skip_logs(self):
        self.action(self.import_url, "publish_selected_imports", self.first_import)
        count = Opportunity.objects.count()
        log_count = LogEntry.objects.count()
        response = self.action(self.import_url, "publish_selected_imports", self.first_import)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Opportunity.objects.count(), count)
        self.assertEqual(LogEntry.objects.count(), log_count)
        feedback = " ".join(str(message) for message in response.wsgi_request._messages)
        self.assertIn("already-published", feedback)

    def test_already_published_and_archived_records_are_skipped_for_both_actions(self):
        first = imported_item(4, opportunity=self.published)
        second = imported_item(5, opportunity=self.archived)
        self.action(self.import_url, "publish_selected_imports", first, second)
        self.action(self.opportunity_url, "publish_selected_drafts", self.published, self.archived)
        self.published.refresh_from_db()
        self.archived.refresh_from_db()
        self.assertEqual(self.published.status, Opportunity.Status.PUBLISHED)
        self.assertEqual(self.archived.status, Opportunity.Status.ARCHIVED)
        self.assertFalse(LogEntry.objects.exists())

    def test_blank_title_error_is_named_and_does_not_block_valid_import(self):
        self.first_import.title = ""
        self.first_import.save()
        response = self.action(self.import_url, "publish_selected_imports", self.first_import, self.second_import)
        self.assertEqual(response.status_code, 302)
        self.first_import.refresh_from_db()
        self.second_import.refresh_from_db()
        self.assertIsNone(self.first_import.opportunity_id)
        self.assertEqual(self.second_import.opportunity.status, Opportunity.Status.PUBLISHED)
        feedback = " ".join(str(message) for message in response.wsgi_request._messages)
        self.assertIn("title: This field cannot be blank.", feedback)
        self.assertIn("Published 1 opportunity.", feedback)

    def test_bad_type_and_source_identity_errors_cannot_bypass_validation(self):
        self.first_import.type = "unsupported"
        self.first_import.title = "<script>alert(1)</script>"
        self.first_import.save()
        unsafe = imported_item(6, source_url="https://example.com/not-the-configured-source")
        response = self.action(self.import_url, "publish_selected_imports", self.first_import, unsafe, self.second_import)
        self.assertEqual(response.status_code, 302)
        self.first_import.refresh_from_db()
        unsafe.refresh_from_db()
        self.second_import.refresh_from_db()
        self.assertIsNone(self.first_import.opportunity_id)
        self.assertIsNone(unsafe.opportunity_id)
        self.assertIsNotNone(self.second_import.opportunity_id)
        feedback = " ".join(str(message) for message in response.wsgi_request._messages)
        self.assertIn("opportunity type:", feedback)
        response = self.client.get(self.import_url)
        self.assertNotContains(response, "<script>alert(1)</script>")
        self.assertContains(response, "&lt;script&gt;")

    def test_invalid_draft_does_not_stop_valid_selected_sibling(self):
        self.first_draft.title = ""
        self.first_draft.save()
        response = self.action(self.opportunity_url, "publish_selected_drafts", self.first_draft, self.second_draft)
        self.assertEqual(response.status_code, 302)
        self.first_draft.refresh_from_db()
        self.second_draft.refresh_from_db()
        self.assertEqual(self.first_draft.status, Opportunity.Status.DRAFT)
        self.assertFalse(self.first_draft.allow_missing_details)
        self.assertEqual(self.second_draft.status, Opportunity.Status.PUBLISHED)
        self.assertEqual(LogEntry.objects.count(), 1)

    def test_import_action_is_hidden_and_cannot_execute_when_any_model_permission_is_missing(self):
        required = ("change_importeditem", "add_opportunity", "change_opportunity")
        for missing in required:
            with self.subTest(missing=missing):
                user = get_user_model().objects.create_user(username=f"missing-{missing}", is_staff=True)
                user.user_permissions.add(*Permission.objects.filter(
                    content_type__app_label="opportunities",
                    codename__in=("view_importeditem", *(name for name in required if name != missing)),
                ))
                self.client.force_login(user)
                response = self.client.get(self.import_url)
                self.assertNotContains(response, "Publish selected imports (allow missing details)")
                self.action(self.import_url, "publish_selected_imports", self.first_import)
                self.first_import.refresh_from_db()
                self.assertIsNone(self.first_import.opportunity_id)
        self.assertFalse(LogEntry.objects.exists())

    def test_view_only_staff_cannot_publish_opportunity_action_or_see_it(self):
        self.client.force_login(self.viewer)
        self.assertNotContains(self.client.get(self.opportunity_url), "Publish selected drafts (allow missing details)")
        self.action(self.opportunity_url, "publish_selected_drafts", self.first_draft)
        self.first_draft.refresh_from_db()
        self.assertEqual(self.first_draft.status, Opportunity.Status.DRAFT)
        self.assertFalse(LogEntry.objects.exists())

    def test_handlers_repeat_authorization_even_if_called_directly(self):
        factory = RequestFactory()
        request = factory.post(self.import_url)
        request.user = self.viewer
        with self.assertRaises(PermissionDenied):
            admin.site._registry[ImportedItem].publish_selected_imports(request, ImportedItem.objects.filter(pk=self.first_import.pk))
        with self.assertRaises(PermissionDenied):
            admin.site._registry[Opportunity].publish_selected_drafts(request, Opportunity.objects.filter(pk=self.first_draft.pk))
        request.user = self.non_staff
        with self.assertRaises(PermissionDenied):
            admin.site._registry[ImportedItem].publish_selected_imports(request, ImportedItem.objects.filter(pk=self.first_import.pk))

    def test_anonymous_and_non_staff_requests_cannot_publish(self):
        for user in (None, self.non_staff):
            client = Client()
            if user is not None:
                client.force_login(user)
            with self.subTest(user=user):
                response = self.action(self.import_url, "publish_selected_imports", self.first_import, client=client)
                self.assertEqual(response.status_code, 302)
                self.assertIn(reverse("admin:login"), response.url)
        self.first_import.refresh_from_db()
        self.assertIsNone(self.first_import.opportunity_id)
        self.assertFalse(LogEntry.objects.exists())

    def test_get_action_parameters_do_not_publish_and_direct_handlers_reject_get(self):
        response = self.client.get(self.import_url, {"action": "publish_selected_imports", "_selected_action": str(self.first_import.pk)})
        self.assertIn(response.status_code, (200, 302))
        self.first_import.refresh_from_db()
        self.assertIsNone(self.first_import.opportunity_id)
        request = RequestFactory().get(self.import_url)
        request.user = self.publisher
        self.assertEqual(admin.site._registry[ImportedItem].publish_selected_imports(request, ImportedItem.objects.all()).status_code, 405)
        self.assertEqual(admin.site._registry[Opportunity].publish_selected_drafts(request, Opportunity.objects.all()).status_code, 405)
        self.assertFalse(LogEntry.objects.exists())

    def test_both_native_actions_require_csrf_and_accept_rendered_token(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.publisher)
        for url, action_name, item in (
            (self.import_url, "publish_selected_imports", self.first_import),
            (self.opportunity_url, "publish_selected_drafts", self.first_draft),
        ):
            with self.subTest(action=action_name):
                response = self.action(url, action_name, item, client=client)
                self.assertEqual(response.status_code, 403)
                response = client.get(url)
                token = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', response.content.decode())
                self.assertIsNotNone(token)
                response = self.action(url, action_name, item, client=client, csrfmiddlewaretoken=token.group(1))
                self.assertEqual(response.status_code, 302)
        self.first_import.refresh_from_db()
        self.first_draft.refresh_from_db()
        self.assertEqual(self.first_import.opportunity.status, Opportunity.Status.PUBLISHED)
        self.assertEqual(self.first_draft.status, Opportunity.Status.PUBLISHED)

    def test_missing_details_override_is_editable_but_title_is_still_required(self):
        url = reverse("admin:opportunities_opportunity_change", args=[self.first_draft.pk])
        response = self.client.get(url)
        self.assertIn("allow_missing_details", response.context["adminform"].form.fields)
        response = self.client.post(url, {
            "title": "Reviewed incomplete draft", "organization": "", "type": "",
            "status": "draft", "source": "manual", "allow_missing_details": "on", "_save": "Save",
        })
        self.assertEqual(response.status_code, 302)
        self.first_draft.refresh_from_db()
        self.assertTrue(self.first_draft.allow_missing_details)
        self.assertEqual(self.first_draft.status, Opportunity.Status.DRAFT)
        response = self.client.post(url, {
            "title": "", "organization": "", "type": "", "status": "published",
            "source": "manual", "allow_missing_details": "on", "_save": "Save",
        })
        self.assertEqual(response.status_code, 200)
        self.assertIn("title", response.context["adminform"].form.errors)

    def test_normal_draft_action_still_requires_missing_details_to_be_completed(self):
        self.action(self.import_url, "create_drafts", self.first_import)
        self.first_import.refresh_from_db()
        self.assertIsNone(self.first_import.opportunity_id)
