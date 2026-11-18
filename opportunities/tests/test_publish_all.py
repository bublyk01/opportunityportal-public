from datetime import date
import re
from unittest.mock import patch

from django.contrib import admin
from django.contrib.admin.models import CHANGE, LogEntry
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.contrib.contenttypes.models import ContentType
from django.contrib.messages import get_messages
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from opportunities.models import Opportunity


@override_settings(SECURE_SSL_REDIRECT=False)
class PublishAllDraftsAdminTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        users = get_user_model()
        cls.editor = users.objects.create_user(username="publisher", is_staff=True)
        cls.viewer = users.objects.create_user(username="viewer", is_staff=True)
        cls.staff = users.objects.create_user(username="staff", is_staff=True)
        cls.non_staff = users.objects.create_user(username="student")
        change_permission = Permission.objects.get(
            content_type__app_label="opportunities", codename="change_opportunity"
        )
        cls.editor.user_permissions.add(change_permission)
        cls.non_staff.user_permissions.add(change_permission)
        cls.viewer.user_permissions.add(
            Permission.objects.get(
                content_type__app_label="opportunities", codename="view_opportunity"
            )
        )
        cls.first_draft = Opportunity.objects.create(
            title="Research internship",
            organization="Example University",
            type=Opportunity.Type.INTERNSHIP,
            employment_type=Opportunity.EmploymentType.PART_TIME,
            work_mode=Opportunity.WorkMode.REMOTE,
            description="Keep this description unchanged.",
            location="Example City",
            deadline=date(2026, 11, 30),
            source=Opportunity.Source.NOTION,
            source_url="https://example.com/research",
        )
        cls.second_draft = Opportunity.objects.create(
            title="Engineering job",
            organization="Another employer",
            type=Opportunity.Type.JOB,
            source=Opportunity.Source.EMAIL,
        )
        cls.published = Opportunity.objects.create(
            title="Already published event",
            organization="Event organizer",
            type=Opportunity.Type.EVENT,
            status=Opportunity.Status.PUBLISHED,
        )
        cls.archived = Opportunity.objects.create(
            title="Archived job",
            organization="Former employer",
            type=Opportunity.Type.JOB,
            status=Opportunity.Status.ARCHIVED,
        )

    def setUp(self):
        self.url = reverse("admin:opportunities_opportunity_publish_all_drafts")
        self.list_url = reverse("admin:opportunities_opportunity_changelist")

    def records(self):
        return list(Opportunity.objects.order_by("pk").values())

    def test_button_counts_all_drafts_independently_of_filters_and_permissions(self):
        self.client.force_login(self.editor)
        with patch.object(admin.site._registry[Opportunity], "list_per_page", 1):
            response = self.client.get(self.list_url, {"q": "Research", "p": "0"})
        self.assertEqual(list(response.context["cl"].result_list), [self.first_draft])
        self.assertContains(response, "Publish all drafts (2)")
        self.assertContains(response, self.url)

        for user in (self.viewer, self.staff):
            with self.subTest(user=user.username):
                self.client.force_login(user)
                response = self.client.get(self.list_url)
                self.assertNotIn(b"Publish all drafts", response.content)

    def test_post_publishes_all_drafts_across_filters_preserving_other_data_and_logs(self):
        before = self.records()
        self.client.force_login(self.editor)
        response = self.client.post(
            self.url + "?type__exact=internship&q=Research&p=0",
            {"_selected_action": [str(self.first_draft.pk)]},
        )
        self.assertRedirects(response, self.list_url)

        expected = []
        for record in before:
            if record["status"] == Opportunity.Status.DRAFT:
                record["status"] = Opportunity.Status.PUBLISHED
            expected.append(record)
        self.assertEqual(self.records(), expected)
        feedback = [str(message) for message in get_messages(response.wsgi_request)]
        self.assertTrue(any("2" in text and "publish" in text.lower() for text in feedback))

        entries = LogEntry.objects.filter(
            content_type=ContentType.objects.get_for_model(Opportunity),
            action_flag=CHANGE,
        )
        self.assertEqual(entries.count(), 2)
        self.assertEqual(
            set(entries.values_list("object_id", flat=True)),
            {str(self.first_draft.pk), str(self.second_draft.pk)},
        )
        self.assertEqual(set(entries.values_list("user_id", flat=True)), {self.editor.pk})

    def test_get_returns_method_not_allowed_without_publishing(self):
        before = self.records()
        self.client.force_login(self.editor)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 405)
        self.assertEqual(self.records(), before)
        self.assertFalse(LogEntry.objects.exists())

    def test_anonymous_and_non_staff_users_are_redirected_to_login(self):
        before = self.records()
        for user in (None, self.non_staff):
            client = Client()
            if user is not None:
                client.force_login(user)
            for method in (client.get, client.post):
                with self.subTest(user=user, method=method.__name__):
                    response = method(self.url)
                    self.assertRedirects(
                        response,
                        f"{reverse('admin:login')}?next={self.url}",
                        fetch_redirect_response=False,
                    )
        self.assertEqual(self.records(), before)
        self.assertFalse(LogEntry.objects.exists())

    def test_view_only_and_unprivileged_staff_cannot_publish(self):
        before = self.records()
        for user in (self.viewer, self.staff):
            with self.subTest(user=user.username):
                self.client.force_login(user)
                response = self.client.post(self.url)
                self.assertEqual(response.status_code, 403)
        self.assertEqual(self.records(), before)
        self.assertFalse(LogEntry.objects.exists())

    def test_post_without_csrf_token_is_rejected(self):
        before = self.records()
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.editor)
        response = client.post(self.url)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.records(), before)
        self.assertFalse(LogEntry.objects.exists())

    def test_button_csrf_token_allows_a_normal_staff_editor_to_publish(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.editor)
        response = client.get(self.list_url)
        token = re.search(
            r'name="csrfmiddlewaretoken" value="([^"]+)"', response.content.decode()
        )
        self.assertIsNotNone(token)
        response = client.post(self.url, {"csrfmiddlewaretoken": token.group(1)})
        self.assertRedirects(response, self.list_url)
        self.assertFalse(Opportunity.objects.filter(status=Opportunity.Status.DRAFT).exists())
        self.assertEqual(
            Opportunity.objects.filter(status=Opportunity.Status.PUBLISHED).count(), 3
        )

    def test_no_drafts_hides_button_and_post_is_a_harmless_noop(self):
        Opportunity.objects.filter(status=Opportunity.Status.DRAFT).update(
            status=Opportunity.Status.PUBLISHED
        )
        before = self.records()
        self.client.force_login(self.editor)
        self.assertNotContains(self.client.get(self.list_url), "Publish all drafts")
        response = self.client.post(self.url)
        self.assertRedirects(response, self.list_url)
        self.assertEqual(self.records(), before)
        self.assertFalse(LogEntry.objects.exists())
