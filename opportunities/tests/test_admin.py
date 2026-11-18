from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from opportunities.models import Opportunity


@override_settings(SECURE_SSL_REDIRECT=False)
class OpportunityAdminTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        users = get_user_model()
        cls.admin = users.objects.create_superuser(
            username="admin",
            email="admin@example.com",
            password="test-admin-password",
        )
        cls.non_staff = users.objects.create_user(username="student")
        cls.staff = users.objects.create_user(username="staff", is_staff=True)
        cls.viewer = users.objects.create_user(username="viewer", is_staff=True)
        cls.viewer.user_permissions.add(
            Permission.objects.get(
                content_type__app_label="opportunities",
                codename="view_opportunity",
            )
        )

    def setUp(self):
        self.add_url = reverse("admin:opportunities_opportunity_add")
        self.list_url = reverse("admin:opportunities_opportunity_changelist")

    def payload(self, **overrides):
        fields = {
            "title": "Research internship",
            "organization": "Example University",
            "type": Opportunity.Type.INTERNSHIP,
            "description": "",
            "location": "",
            "deadline": "",
            "source": Opportunity.Source.MANUAL,
            "source_url": "",
            "status": Opportunity.Status.DRAFT,
            "_save": "Save",
        }
        fields.update(overrides)
        return fields

    def test_root_serves_public_opportunities_without_admin_login(self):
        response = self.client.get(reverse("home"))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "opportunities/list.html")
        self.assertContains(response, "Find your next")

    def test_anonymous_visitors_cannot_read_or_create_in_admin(self):
        for url in (reverse("admin:index"), self.list_url, self.add_url):
            with self.subTest(url=url):
                self.assertRedirects(
                    self.client.get(url),
                    f"{reverse('admin:login')}?next={url}",
                )
        response = self.client.post(self.add_url, self.payload())
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Opportunity.objects.exists())

    def test_non_staff_users_cannot_enter_or_create(self):
        self.client.force_login(self.non_staff)
        self.assertEqual(self.client.get(self.list_url).status_code, 302)
        self.assertEqual(
            self.client.post(self.add_url, self.payload()).status_code, 302
        )
        self.assertFalse(Opportunity.objects.exists())

    def test_staff_need_model_permission_to_add(self):
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get(self.add_url).status_code, 403)
        self.assertEqual(
            self.client.post(self.add_url, self.payload()).status_code, 403
        )
        self.assertFalse(Opportunity.objects.exists())

    def test_view_permission_does_not_grant_write_access(self):
        opportunity = Opportunity.objects.create(
            title="Visible to reviewer",
            organization="Example University",
            type=Opportunity.Type.EVENT,
        )
        self.client.force_login(self.viewer)
        self.assertEqual(self.client.get(self.list_url).status_code, 200)
        change_url = reverse(
            "admin:opportunities_opportunity_change", args=[opportunity.pk]
        )
        self.assertEqual(self.client.get(change_url).status_code, 200)
        self.assertEqual(
            self.client.post(
                change_url, self.payload(title="Unauthorized edit")
            ).status_code,
            403,
        )
        opportunity.refresh_from_db()
        self.assertEqual(opportunity.title, "Visible to reviewer")

    def test_superuser_can_create_an_opportunity(self):
        self.client.force_login(self.admin)
        response = self.client.post(self.add_url, self.payload())

        self.assertRedirects(response, self.list_url)
        opportunity = Opportunity.objects.get()
        self.assertEqual(opportunity.status, Opportunity.Status.DRAFT)
        self.assertEqual(opportunity.title, "Research internship")

    def test_authorized_admin_can_publish_without_changing_created_at(self):
        opportunity = Opportunity.objects.create(
            title="Research internship",
            organization="Example University",
            type=Opportunity.Type.INTERNSHIP,
        )
        created_at = opportunity.created_at
        self.client.force_login(self.admin)
        response = self.client.post(
            reverse("admin:opportunities_opportunity_change", args=[opportunity.pk]),
            self.payload(
                status=Opportunity.Status.PUBLISHED,
                created_at="2000-01-01 00:00:00",
            ),
        )

        self.assertRedirects(response, self.list_url)
        opportunity.refresh_from_db()
        self.assertEqual(opportunity.status, Opportunity.Status.PUBLISHED)
        self.assertEqual(opportunity.created_at, created_at)

    def test_admin_form_rejects_unsafe_source_links(self):
        self.client.force_login(self.admin)
        for source_url in (
            "javascript:alert(1)",
            "data:text/html,example",
            "ftp://example.com/opportunity",
        ):
            with self.subTest(source_url=source_url):
                response = self.client.post(
                    self.add_url, self.payload(source_url=source_url)
                )
                self.assertEqual(response.status_code, 200)
                self.assertIn(
                    "source_url", response.context["adminform"].form.errors
                )
        self.assertFalse(Opportunity.objects.exists())

    def test_search_and_status_filters_find_the_expected_record(self):
        draft = Opportunity.objects.create(
            title="Research internship",
            organization="Example University",
            type=Opportunity.Type.INTERNSHIP,
        )
        Opportunity.objects.create(
            title="Career fair",
            organization="Another Organization",
            type=Opportunity.Type.EVENT,
            status=Opportunity.Status.PUBLISHED,
        )
        self.client.force_login(self.admin)
        for query in ({"q": "Example University"}, {"status__exact": "draft"}):
            with self.subTest(query=query):
                response = self.client.get(self.list_url, query)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(
                    list(response.context["cl"].result_list), [draft]
                )

    def test_admin_escapes_untrusted_descriptions(self):
        opportunity = Opportunity.objects.create(
            title="Research internship",
            organization="Example University",
            type=Opportunity.Type.INTERNSHIP,
            description="<script>alert(1)</script>",
        )
        self.client.force_login(self.admin)
        response = self.client.get(
            reverse("admin:opportunities_opportunity_change", args=[opportunity.pk])
        )
        self.assertContains(response, "&lt;script&gt;alert(1)&lt;/script&gt;")
        self.assertNotContains(response, "<script>alert(1)</script>")

    def test_admin_writes_require_a_csrf_token(self):
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.admin)
        response = csrf_client.post(self.add_url, self.payload())
        self.assertEqual(response.status_code, 403)
        self.assertFalse(Opportunity.objects.exists())
