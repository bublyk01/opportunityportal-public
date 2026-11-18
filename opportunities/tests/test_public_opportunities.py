from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from opportunities.models import Opportunity


@override_settings(SECURE_SSL_REDIRECT=False)
class PublicOpportunityTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        def published(title, kind, **fields):
            return Opportunity.objects.create(
                title=title, type=kind, status=Opportunity.Status.PUBLISHED,
                organization=fields.pop("organization", "Research Center"), **fields,
            )

        cls.internship = published(
            "Research internship", Opportunity.Type.INTERNSHIP, organization="Lab One",
            description="Study causal inference with our team.", source=Opportunity.Source.SLACK,
            deadline=date(2026, 11, 30), location="Example City",
        )
        cls.job = published(
            "Graduate developer", Opportunity.Type.JOB, organization="Studio Two",
            description="Build data tools.", source=Opportunity.Source.NOTION,
        )
        cls.seminar = published(
            "Methods seminar", Opportunity.Type.EVENT, event_type=Opportunity.EventType.SEMINAR,
            source=Opportunity.Source.SLACK, deadline=date(2026, 11, 17),
        )
        cls.lecture = published(
            "History lecture", Opportunity.Type.EVENT, event_type=Opportunity.EventType.LECTURE,
            deadline=date(2026, 11, 25),
        )
        cls.workshop = published(
            "Design workshop", Opportunity.Type.EVENT, event_type=Opportunity.EventType.WORKSHOP,
        )
        cls.unspecified = published("Community gathering", Opportunity.Type.EVENT)
        cls.other = published("Career fair", Opportunity.Type.EVENT, event_type=Opportunity.EventType.OTHER)
        cls.draft = Opportunity.objects.create(
            title="Private draft marker", organization="Private draft organization",
            type=Opportunity.Type.INTERNSHIP, description="Private draft description",
        )
        cls.archived = Opportunity.objects.create(
            title="Private archived marker", organization="Private archived organization",
            type=Opportunity.Type.EVENT, status=Opportunity.Status.ARCHIVED,
            description="Private archived description",
        )
        cls.staff = get_user_model().objects.create_superuser(
            username="public-reviewer", email="reviewer@example.com", password="test-password",
        )

    def listing(self, **filters):
        return self.client.get(reverse("opportunities:list"), filters)

    def ids(self, response):
        return {item.pk for item in response.context["opportunities"]}

    def test_home_and_list_are_public_and_share_the_published_feed(self):
        home = self.client.get(reverse("home"))
        listing = self.listing()

        self.assertEqual(home.status_code, 200)
        self.assertEqual(listing.status_code, 200)
        self.assertTemplateUsed(home, "opportunities/list.html")
        self.assertEqual(self.ids(home), self.ids(listing))
        self.assertEqual(home.context["result_count"], 7)
        self.assertNotContains(home, self.draft.title)
        self.assertNotContains(home, self.archived.title)
        self.assertContains(home, "Staff sign in")

    def test_staff_public_views_cannot_read_drafts_or_archived_details(self):
        self.client.force_login(self.staff)
        response = self.listing()
        self.assertEqual(response.context["result_count"], 7)
        self.assertNotIn(self.draft.pk, self.ids(response))
        self.assertNotIn(self.archived.pk, self.ids(response))
        for item in (self.draft, self.archived):
            with self.subTest(status=item.status):
                self.assertEqual(self.client.get(reverse("opportunities:detail", args=[item.pk])).status_code, 404)
        self.assertContains(response, "Staff admin")

    def test_search_matches_title_organization_and_description_without_exposing_private_rows(self):
        for query in ("RESEARCH INTERNSHIP", "Lab One", "causal inference"):
            with self.subTest(query=query):
                response = self.listing(q=query)
                self.assertEqual(self.ids(response), {self.internship.pk})
                self.assertEqual(response.context["result_count"], 1)
        for query in ("Private draft", "Private archived"):
            with self.subTest(query=query):
                response = self.listing(q=query)
                self.assertEqual(response.context["result_count"], 0)
                self.assertEqual(self.ids(response), set())

    def test_internship_filter_excludes_all_events_and_jobs(self):
        response = self.listing(type="internship")
        self.assertEqual(self.ids(response), {self.internship.pk})
        self.assertEqual(response.context["result_count"], 1)

    def test_stale_event_format_is_ignored_when_switching_to_internships_or_jobs(self):
        for kind, expected in (("internship", self.internship), ("job", self.job)):
            for stale_format in ("seminar", "not-a-real-format"):
                with self.subTest(kind=kind, stale_format=stale_format):
                    response = self.listing(type=kind, event_type=stale_format)
                    self.assertEqual(self.ids(response), {expected.pk})
                    self.assertFalse(response.context["form"].errors)
                    self.assertEqual(response.context["form"]["event_type"].value(), "")

    def test_event_format_works_alone_and_combines_with_type_search_and_source(self):
        for filters in (
            {"event_type": "seminar"},
            {"type": "event", "event_type": "seminar", "source": "slack", "q": "Methods"},
        ):
            with self.subTest(filters=filters):
                self.assertEqual(self.ids(self.listing(**filters)), {self.seminar.pk})

    def test_hide_seminars_keeps_other_events_and_unknown_event_formats(self):
        response = self.listing(hide_seminars="1")
        self.assertEqual(self.ids(response), {
            self.internship.pk, self.job.pk, self.lecture.pk,
            self.workshop.pk, self.unspecified.pk, self.other.pk,
        })
        self.assertEqual(response.context["result_count"], 6)
        response = self.listing(type="event", hide_seminars="1")
        self.assertEqual(self.ids(response), {
            self.lecture.pk, self.workshop.pk, self.unspecified.pk, self.other.pk,
        })
        self.assertEqual(self.ids(self.listing(type="internship", hide_seminars="1")), {self.internship.pk})

    def test_contradictory_seminar_filter_and_hide_option_returns_no_results(self):
        response = self.listing(event_type="seminar", hide_seminars="1")
        self.assertEqual(response.context["result_count"], 0)
        self.assertContains(response, "No opportunities found")

    def test_source_filter_combines_with_opportunity_type(self):
        self.assertEqual(self.ids(self.listing(source="slack")), {self.internship.pk, self.seminar.pk})
        self.assertEqual(self.ids(self.listing(source="slack", type="job")), set())

    def test_invalid_filters_are_explained_and_do_not_expose_unfiltered_results(self):
        for filters, field in (
            ({"type": "draft"}, "type"),
            ({"type": "event", "event_type": "unknown"}, "event_type"),
            ({"source": "unknown"}, "source"),
            ({"sort": "DROP TABLE opportunities"}, "sort"),
            ({"q": "a" * 201}, "q"),
        ):
            with self.subTest(filters=filters):
                response = self.listing(**filters)
                self.assertEqual(response.status_code, 200)
                self.assertIn(field, response.context["form"].errors)
                self.assertEqual(response.context["result_count"], 0)
                self.assertContains(response, "Check the highlighted filters")

    def test_unknown_status_parameters_and_sql_like_search_cannot_bypass_public_scope(self):
        response = self.listing(status="draft")
        self.assertEqual(response.context["result_count"], 7)
        self.assertNotContains(response, self.draft.title)
        response = self.listing(q="' OR 1=1 --")
        self.assertEqual(response.context["result_count"], 0)

    def test_default_order_is_newest_first_and_deadline_order_puts_unknown_dates_last(self):
        Opportunity.objects.filter(pk=self.other.pk).update(created_at=timezone.now() - timedelta(days=10))
        response = self.listing()
        items = list(response.context["opportunities"])
        self.assertEqual(items[-1].pk, self.other.pk)
        response = self.listing(sort="deadline")
        items = list(response.context["opportunities"])
        self.assertEqual([item.pk for item in items[:3]], [self.seminar.pk, self.lecture.pk, self.internship.pk])
        self.assertTrue(all(item.deadline is None for item in items[3:]))

    def test_pagination_preserves_filters_and_counts_only_published_matches(self):
        Opportunity.objects.bulk_create([
            Opportunity(
                title=f"PagingMarker {index}", organization="Paging Organization",
                type=Opportunity.Type.INTERNSHIP, source=Opportunity.Source.SLACK,
                status=Opportunity.Status.PUBLISHED,
            ) for index in range(13)
        ] + [
            Opportunity(
                title=f"PagingMarker private {index}", organization="Paging Organization",
                type=Opportunity.Type.INTERNSHIP, source=Opportunity.Source.SLACK,
            ) for index in range(20)
        ])
        filters = {"q": "PagingMarker", "type": "internship", "source": "slack", "hide_seminars": "1", "sort": "deadline", "page": "2"}
        response = self.listing(**filters)

        self.assertEqual(response.context["result_count"], 13)
        self.assertEqual(response.context["page_obj"].number, 2)
        self.assertEqual(len(response.context["opportunities"]), 1)
        self.assertNotContains(response, "PagingMarker private")
        self.assertContains(response, "q=PagingMarker&amp;type=internship&amp;source=slack&amp;hide_seminars=1&amp;sort=deadline&amp;page=1")
        for value in ("invalid", "-5", "999999999999999999999999"):
            with self.subTest(page=value):
                response = self.listing(**{**filters, "page": value})
                self.assertEqual(response.status_code, 200)
                self.assertIn(response.context["page_obj"].number, (1, 2))
                self.assertEqual(response.context["result_count"], 13)

    def test_public_detail_includes_escaped_plain_text_and_safe_original_link(self):
        self.internship.description = "First paragraph.\n\nSecond line.\n<script>alert('x')</script>"
        self.internship.source_url = "https://workspace.slack.example.invalid/archives/C1111111111/p1794000000000001"
        self.internship.save()
        response = self.client.get(reverse("opportunities:detail", args=[self.internship.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "<p>First paragraph.</p>", html=True)
        self.assertContains(response, "&lt;script&gt;")
        self.assertNotContains(response, "<script>")
        self.assertContains(response, f'href="{self.internship.source_url}"')
        self.assertContains(response, 'rel="noopener noreferrer"')
        self.assertContains(response, "View original posting")

    def test_detail_does_not_render_unsafe_or_credential_bearing_source_urls(self):
        for value in ("javascript:alert(1)", "data:text/html,test", "ftp://example.com/test", "https://secret:password@example.com/test"):
            with self.subTest(value=value):
                Opportunity.objects.filter(pk=self.internship.pk).update(source_url=value)
                response = self.client.get(reverse("opportunities:detail", args=[self.internship.pk]))
                self.assertNotContains(response, "View original posting")
                self.assertNotContains(response, value)
        Opportunity.objects.filter(pk=self.internship.pk).update(source_url="http://example.com/opportunity")
        self.assertContains(self.client.get(reverse("opportunities:detail", args=[self.internship.pk])), 'href="http://example.com/opportunity"')

    def test_list_escapes_title_organization_and_query_text(self):
        self.internship.title = "<script>alert('title')</script>"
        self.internship.organization = '<img src=x onerror="alert(1)">'
        self.internship.save()
        response = self.listing()
        self.assertContains(response, "&lt;script&gt;")
        self.assertContains(response, "&lt;img")
        self.assertNotContains(response, "<script>")
        self.assertNotContains(response, "<img src=x")
        response = self.listing(q='<svg onload="alert(1)">')
        self.assertNotContains(response, "<svg onload=")
        self.assertContains(response, "&lt;svg")

    def test_public_endpoints_reject_post_and_do_not_modify_opportunities(self):
        for url in (reverse("home"), reverse("opportunities:list"), reverse("opportunities:detail", args=[self.internship.pk])):
            with self.subTest(url=url):
                response = self.client.post(url, {"status": "draft", "title": "Unauthorized edit"})
                self.assertEqual(response.status_code, 405)
        self.internship.refresh_from_db()
        self.assertEqual(self.internship.title, "Research internship")
        self.assertEqual(self.internship.status, Opportunity.Status.PUBLISHED)

    def test_missing_or_unpublished_detail_returns_404_to_anonymous_visitor(self):
        for pk in (self.draft.pk, self.archived.pk, 999999):
            with self.subTest(pk=pk):
                self.assertEqual(self.client.get(reverse("opportunities:detail", args=[pk])).status_code, 404)
