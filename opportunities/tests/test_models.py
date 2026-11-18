from datetime import date

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from opportunities.models import Opportunity


class OpportunityModelTests(TestCase):
    def opportunity(self, **overrides):
        fields = {
            "title": "Research internship",
            "organization": "Example University",
            "type": Opportunity.Type.INTERNSHIP,
        }
        fields.update(overrides)
        return Opportunity(**fields)

    def test_new_opportunities_are_manual_drafts(self):
        opportunity = self.opportunity()
        opportunity.full_clean()
        opportunity.save()
        opportunity.refresh_from_db()

        self.assertEqual(opportunity.status, Opportunity.Status.DRAFT)
        self.assertEqual(opportunity.source, Opportunity.Source.MANUAL)
        self.assertTrue(timezone.is_aware(opportunity.created_at))

    def test_all_fields_survive_database_round_trip(self):
        opportunity = self.opportunity(
            description="Work with the research team.",
            location="Example City / Remote",
            deadline=date(2026, 11, 30),
            source=Opportunity.Source.NOTION,
            source_url="https://example.com/internship",
            status=Opportunity.Status.PUBLISHED,
        )
        opportunity.full_clean()
        opportunity.save()
        opportunity.refresh_from_db()

        self.assertEqual(opportunity.title, "Research internship")
        self.assertEqual(opportunity.organization, "Example University")
        self.assertEqual(opportunity.type, Opportunity.Type.INTERNSHIP)
        self.assertEqual(opportunity.description, "Work with the research team.")
        self.assertEqual(opportunity.location, "Example City / Remote")
        self.assertEqual(opportunity.deadline, date(2026, 11, 30))
        self.assertEqual(opportunity.source, Opportunity.Source.NOTION)
        self.assertEqual(opportunity.source_url, "https://example.com/internship")
        self.assertEqual(opportunity.status, Opportunity.Status.PUBLISHED)

    def test_optional_fields_can_be_empty(self):
        opportunity = self.opportunity()
        opportunity.full_clean()
        opportunity.save()
        self.assertIsNone(opportunity.deadline)

    def test_required_fields_cannot_be_empty(self):
        for field in ("title", "organization", "type"):
            with self.subTest(field=field):
                opportunity = self.opportunity(**{field: ""})
                with self.assertRaises(ValidationError) as error:
                    opportunity.full_clean()
                self.assertIn(field, error.exception.message_dict)

    def test_invalid_choices_are_rejected(self):
        for field in ("type", "source", "status"):
            with self.subTest(field=field):
                opportunity = self.opportunity(**{field: "invalid"})
                with self.assertRaises(ValidationError) as error:
                    opportunity.full_clean()
                self.assertIn(field, error.exception.message_dict)

    def test_http_and_https_source_urls_are_accepted(self):
        for scheme in ("http", "https"):
            with self.subTest(scheme=scheme):
                self.opportunity(
                    source_url=f"{scheme}://example.com/opportunity"
                ).full_clean()

    def test_unsafe_source_urls_are_rejected(self):
        for source_url in (
            "javascript:alert(1)",
            "data:text/html,example",
            "ftp://example.com/opportunity",
            "ftps://example.com/opportunity",
            "//example.com/opportunity",
        ):
            with self.subTest(source_url=source_url):
                with self.assertRaises(ValidationError) as error:
                    self.opportunity(source_url=source_url).full_clean()
                self.assertIn("source_url", error.exception.message_dict)

    def test_newest_records_come_first_with_stable_ties(self):
        first = self.opportunity(title="First")
        first.save()
        second = self.opportunity(title="Second")
        second.save()
        Opportunity.objects.update(created_at=timezone.now())

        self.assertEqual(list(Opportunity.objects.all()), [second, first])
