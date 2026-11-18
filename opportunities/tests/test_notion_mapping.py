from datetime import date
from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from opportunities.services import notion_public as notion


SOURCE_URL = f"https://{notion.PUBLIC_HOST}/33333333333343338333333333333333"


class SourceMappingRegressionTests(SimpleTestCase):
    def listing(self, *, title="Research role", properties=None, description="", text=""):
        return notion.normalize_listing(
            {"source_url": SOURCE_URL, "title": title, "text": title},
            {"title": title, "properties": properties or {}, "description": description, "text": text},
        )

    def test_synthetic_listing_maps_kind_hours_country_and_employer_separately(self):
        description = (
            "Example Training Academy is offering a fictional Software Testing Internship.\n"
            "Currently located in Example Country and available for supervised practice."
        )
        text = (
            "Software Testing Internship\nDeadline\nNovember 16, 2026\n"
            "Format\nIntership\nSource Coordinator\nExample Coordinator\nPosition title\n"
            "Software Testing Intern\nProgram\nExample Program\nStatus\nOpen\n"
            "Type\nPart-time\n" + description
        )
        item = self.listing(title="Software Testing Internship", description=description, text=text)
        self.assertEqual(item["organization"], "Example Training Academy")
        self.assertEqual(item["type"], "internship")
        self.assertEqual(item["employment_type"], "part_time")
        self.assertEqual(item["work_mode"], "")
        self.assertEqual(item["location"], "Example Country")
        self.assertEqual(item["deadline"], date(2026, 11, 16))
        self.assertIn("Currently located in Example Country", item["raw_data"]["field_evidence"]["location"])
        self.assertTrue(any("applicant location requirement" in warning for warning in item["raw_data"]["warnings"]))

    def test_lowercase_brand_and_cyrillic_legal_prefix_are_named_organizations(self):
        for description, expected in (
            ("examplelabs is a fictional company that builds sample tools.", "examplelabs"),
            ("exampleworks is a small research company with a fictional team.", "exampleworks"),
            ("АТ EXAMPLEBANK is a fictional bank used in parser examples.", "АТ EXAMPLEBANK"),
            ("Example Training Academy is offering a sample internship.", "Example Training Academy"),
        ):
            with self.subTest(description=description):
                self.assertEqual(self.listing(description=description)["organization"], expected)

    def test_conference_and_inviting_organizer_are_extracted(self):
        item = self.listing(
            title="Example Technology Conference 2026",
            properties={"Format": "online", "Type": "Conference"},
            description=(
                "The Example Learning Association officially invites "
                "new participants to a fictional Example Technology Conference 2026."
            ),
        )
        self.assertEqual(item["organization"], "Example Learning Association")
        self.assertEqual(item["type"], "event")
        self.assertEqual(item["work_mode"], "remote")
        self.assertEqual(item["location"], "")

    def test_paid_internship_is_not_treated_as_an_unknown_type(self):
        item = self.listing(
            title="Sample Research Intern",
            properties={"Type": "Paid internship", "Format": "Offline"},
            description=(
                "Example Bank is a fictional bank used for development examples.\n"
                "You will practice at the Example City office with a sample team."
            ),
        )
        self.assertEqual(item["type"], "internship")
        self.assertEqual(item["work_mode"], "on_site")
        self.assertEqual(item["location"], "Example City")

    def test_intern_title_has_priority_over_hours_or_generic_vacancy(self):
        for source_type in ("Part-time", "Full-time", "Vacancy"):
            with self.subTest(source_type=source_type):
                item = self.listing(title="Engineer Intern", properties={"Type": source_type})
                self.assertEqual(item["type"], "internship")

    def test_on_site_city_and_remote_arrangement_do_not_pollute_location(self):
        onsite = self.listing(
            properties={"Format": "Offline", "Type": "Vacancy"},
            description="You will work on-site in Example City within the engineering team.",
        )
        remote = self.listing(properties={"Format": "Remote", "Type": "Full-time"})
        self.assertEqual((onsite["location"], onsite["work_mode"]), ("Example City", "on_site"))
        self.assertEqual((remote["location"], remote["work_mode"]), ("", "remote"))

    def test_headquarters_or_coverage_country_is_not_a_job_location(self):
        item = self.listing(
            description=(
                "Example Bank is a fictional bank operating in Another Country.\n"
                "The assignment concerns data from Example Country."
            )
        )
        self.assertEqual(item["location"], "")

    def test_company_current_location_is_not_applicant_eligibility(self):
        for description in (
            "Our headquarters are currently located in Another City. This role is remote.",
            "The company is currently located in Other City; applicants can work worldwide.",
        ):
            with self.subTest(description=description):
                self.assertEqual(self.listing(description=description)["location"], "")

    def test_event_venue_can_supply_location_without_reusing_flexible_format(self):
        item = self.listing(
            properties={"Format": "Flexible", "Type": "Event"},
            description="The conference will take place offline in Example City at the venue.",
        )
        self.assertEqual(item["type"], "event")
        self.assertEqual(item["location"], "Example City")
        self.assertEqual(item["work_mode"], "on_site")

    def test_empty_format_does_not_consume_the_coordinator_field(self):
        item = self.listing(text="Format\nSource Coordinator\nExample Person\nType\nVacancy")
        self.assertEqual(item["location"], "")
        self.assertEqual(item["work_mode"], "")
        self.assertEqual(item["type"], "job")

    def test_property_labels_in_description_do_not_override_the_header(self):
        description = "Join our team.\nCompany\nA company mentioned in an example.\nLocation\nSomewhere"
        item = self.listing(description=description, text="Type\nVacancy\n" + description)
        self.assertEqual(item["organization"], "")
        self.assertEqual(item["location"], "")


class FullDescriptionCollectionTests(SimpleTestCase):
    def page(self, snapshots):
        page = Mock()
        iterator = iter(snapshots)
        last = snapshots[-1]
        page.evaluate.side_effect = lambda script: next(iterator, last)
        page.locator.return_value.first.count.return_value = 1
        page.locator.return_value.first.evaluate.return_value = True
        page.locator.return_value.all.return_value = []
        return page

    def test_virtualized_tail_is_collected_without_losing_the_first_paragraph(self):
        top = {
            "title": "Engineer Internship", "text": "Type\nPart-time\nFirst paragraph.",
            "properties": {}, "blocks": [{"id": "first", "text": "First paragraph."}],
        }
        bottom = {
            "title": "Engineer Internship", "text": "Final selection process.",
            "blocks": [{"id": "last", "text": "Final selection process."}],
        }
        with patch.object(notion.time, "monotonic", return_value=0):
            detail, complete = notion._read_detail(self.page([top, bottom]), stop_at=100)
        self.assertTrue(complete)
        self.assertEqual(detail["description"], "First paragraph.\nFinal selection process.")
        self.assertTrue(detail["text"].startswith("Type\nPart-time\n"))
        self.assertTrue(detail["description_complete"])

    def test_partial_traversal_does_not_claim_complete_description(self):
        top = {"title": "Internship", "text": "First.", "blocks": [{"id": "first", "text": "First."}]}
        with patch.object(notion.time, "monotonic", side_effect=[0, 101]):
            detail, complete = notion._read_detail(self.page([top]), stop_at=100)
        self.assertFalse(complete)
        self.assertFalse(detail["description_complete"])

    def test_later_rendered_title_and_properties_replace_initial_placeholders(self):
        first = {
            "title": "Notion", "text": "First.", "properties": {"Type": ""},
            "blocks": [{"id": "first", "text": "First."}],
        }
        later = {
            "title": "Engineer Internship", "text": "Type\nPart-time\nFirst.",
            "properties": {"Type": "Part-time"},
            "blocks": [{"id": "last", "text": "Last."}],
        }
        with patch.object(notion.time, "monotonic", return_value=0):
            detail, complete = notion._read_detail(self.page([first, later]), stop_at=100)
        self.assertTrue(complete)
        self.assertEqual(detail["title"], "Engineer Internship")
        self.assertEqual(detail["properties"]["Type"], "Part-time")
