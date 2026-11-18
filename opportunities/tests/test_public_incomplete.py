from django.test import TestCase, override_settings
from django.urls import reverse

from opportunities.models import Opportunity


@override_settings(SECURE_SSL_REDIRECT=False)
class PublicIncompleteOpportunityTests(TestCase):
    def opportunity(self, **fields):
        values = {
            "title": "Student opportunity", "organization": "", "type": "",
            "status": Opportunity.Status.PUBLISHED, "allow_missing_details": True,
            "source": Opportunity.Source.SLACK, "source_url": "https://example.org/posting",
        }
        values.update(fields)
        opportunity = Opportunity(**values)
        opportunity.full_clean()
        opportunity.save()
        return opportunity

    def listing(self, **filters):
        return self.client.get(reverse("opportunities:list"), filters)

    def detail(self, opportunity):
        return self.client.get(reverse("opportunities:detail", args=[opportunity.pk]))

    def ids(self, response):
        return {opportunity.pk for opportunity in response.context["opportunities"]}

    def test_published_incomplete_card_has_explicit_placeholders_and_marker(self):
        opportunity = self.opportunity()
        response = self.listing()
        self.assertEqual(self.ids(response), {opportunity.pk})
        self.assertContains(response, "Details incomplete", count=1)
        self.assertContains(response, '<p class="organization">Organization not specified</p>', html=True)
        self.assertContains(response, '<span class="type-badge type-unspecified">Type not specified</span>', html=True)
        self.assertContains(response, reverse("opportunities:detail", args=[opportunity.pk]))

    def test_detail_names_the_missing_information_and_keeps_original_link_available(self):
        opportunity = self.opportunity()
        response = self.detail(opportunity)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Details incomplete", count=1)
        self.assertContains(response, "Organization not specified", count=2)
        self.assertContains(response, "Type not specified", count=2)
        self.assertContains(response, "Some details have not been provided. Check the original posting for the organization and opportunity type.")
        self.assertContains(response, 'href="https://example.org/posting"')
        self.assertContains(response, 'target="_blank" rel="noopener noreferrer"')

    def test_unknown_type_is_visible_in_all_but_not_internship_filter(self):
        unknown = self.opportunity(title="Unknown opportunity category")
        internship = self.opportunity(title="Known internship", type=Opportunity.Type.INTERNSHIP)
        job = self.opportunity(title="Known job", type=Opportunity.Type.JOB)
        self.assertEqual(self.ids(self.listing()), {unknown.pk, internship.pk, job.pk})
        response = self.listing(type="internship")
        self.assertEqual(self.ids(response), {internship.pk})
        self.assertNotContains(response, unknown.title)
        self.assertNotContains(response, job.title)
        self.assertNotContains(response, "Type not specified")

    def test_missing_organization_does_not_remove_known_type_or_its_filter(self):
        internship = self.opportunity(type=Opportunity.Type.INTERNSHIP)
        response = self.listing(type="internship")
        self.assertEqual(self.ids(response), {internship.pk})
        self.assertContains(response, '<span class="type-badge type-internship">Internship</span>', html=True)
        self.assertContains(response, "Organization not specified")
        self.assertContains(response, "Details incomplete")
        detail = self.detail(internship)
        self.assertContains(detail, "Check the original posting for the organization.")
        self.assertNotContains(detail, "Type not specified")

    def test_known_organization_is_retained_when_only_type_is_missing(self):
        opportunity = self.opportunity(organization="Example Institute")
        for response in (self.listing(), self.detail(opportunity)):
            with self.subTest(template=response.templates[0].name):
                self.assertContains(response, "Example Institute")
                self.assertNotContains(response, "Organization not specified")
                self.assertContains(response, "Type not specified")
        self.assertContains(self.detail(opportunity), "Check the original posting for the opportunity type.")

    def test_whitespace_only_organization_uses_missing_details_placeholders(self):
        opportunity = self.opportunity(organization="   ", type=Opportunity.Type.JOB)
        for response in (self.listing(), self.detail(opportunity)):
            with self.subTest(template=response.templates[0].name):
                self.assertContains(response, "Organization not specified")
                self.assertContains(response, "Details incomplete")
                self.assertNotContains(response, "Type not specified")
        self.assertContains(self.detail(opportunity), "Check the original posting for the organization.")

    def test_draft_and_archived_incomplete_records_remain_private(self):
        for status in (Opportunity.Status.DRAFT, Opportunity.Status.ARCHIVED):
            opportunity = self.opportunity(title=f"Private {status} opportunity", status=status)
            with self.subTest(status=status):
                self.assertNotContains(self.listing(), opportunity.title)
                self.assertEqual(self.detail(opportunity).status_code, 404)

    def test_correcting_details_removes_marker_even_when_permission_flag_stays_enabled(self):
        opportunity = self.opportunity()
        self.assertContains(self.detail(opportunity), "Details incomplete")
        opportunity.organization = "Example Institute"
        opportunity.type = Opportunity.Type.JOB
        opportunity.full_clean()
        opportunity.save()
        for response in (self.listing(), self.detail(opportunity)):
            with self.subTest(template=response.templates[0].name):
                self.assertContains(response, "Example Institute")
                self.assertNotContains(response, "Details incomplete")
                self.assertNotContains(response, "Some details have not been provided")
                self.assertNotContains(response, "Organization not specified")
                self.assertNotContains(response, "Type not specified")
        opportunity.refresh_from_db()
        self.assertTrue(opportunity.allow_missing_details)

    def test_incomplete_record_metadata_and_description_remain_escaped(self):
        opportunity = self.opportunity(
            title='<script>alert("title")</script>',
            organization='<b onclick="alert(1)">Example</b>',
            description='<img src=x onerror="alert(1)">Read the original posting.',
        )
        for response in (self.listing(), self.detail(opportunity)):
            with self.subTest(template=response.templates[0].name):
                self.assertContains(response, "&lt;script&gt;")
                self.assertContains(response, "&lt;b onclick=")
                self.assertContains(response, "&lt;img src=x")
                self.assertNotContains(response, '<script>alert("title")</script>')
                self.assertNotContains(response, '<b onclick="alert(1)">')
                self.assertNotContains(response, '<img src=x onerror=')

    def test_unsafe_legacy_original_url_is_not_exposed_for_incomplete_record(self):
        opportunity = self.opportunity()
        Opportunity.objects.filter(pk=opportunity.pk).update(source_url="javascript:alert(1)")
        response = self.detail(opportunity)
        self.assertContains(response, "Details incomplete")
        self.assertNotContains(response, "javascript:")
        self.assertNotContains(response, "View original posting")
