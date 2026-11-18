import sys
from datetime import date
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from opportunities.services import notion_public as notion


PAGE_ID = "a1b2c3d4e5f6478899aabbccddeeff00"
PAGE_UUID = "a1b2c3d4-e5f6-4788-99aa-bbccddeeff00"
PAGE_URL = f"https://{notion.PUBLIC_HOST}/{PAGE_ID}"


class PublicNotionValidationTests(SimpleTestCase):
    def test_page_ids_are_canonicalized_without_urls_or_extra_characters(self):
        for value in (PAGE_ID, PAGE_ID.upper(), PAGE_UUID, PAGE_UUID.upper()):
            with self.subTest(value=value):
                self.assertEqual(notion.canonical_page_id(value), PAGE_ID)
        for value in (None, "", "a" * 31, "z" * 32, PAGE_URL, PAGE_ID + " "):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    notion.canonical_page_id(value)

    def test_only_configured_https_page_urls_are_accepted(self):
        self.assertEqual(notion.validate_source_url(PAGE_URL), PAGE_URL)
        self.assertEqual(
            notion.validate_source_url(
                f"https://{notion.PUBLIC_HOST}/Research-internship-{PAGE_ID.upper()}?view=x#top"
            ),
            PAGE_URL,
        )
        self.assertEqual(
            notion.validate_source_url(f"https://{notion.PUBLIC_HOST}/{PAGE_UUID}"),
            PAGE_URL,
        )

    def test_unsafe_source_urls_are_rejected(self):
        for value in (
            f"http://{notion.PUBLIC_HOST}/{PAGE_ID}",
            f"https://{notion.PUBLIC_HOST}:443/{PAGE_ID}",
            f"https://user:password@{notion.PUBLIC_HOST}/{PAGE_ID}",
            f"https://{notion.PUBLIC_HOST}.example.com/{PAGE_ID}",
            f"https://sub.{notion.PUBLIC_HOST}/{PAGE_ID}",
            f"https://127.0.0.1/{PAGE_ID}",
            f"https://[::1]/{PAGE_ID}",
            f"https://localhost/{PAGE_ID}",
            f"https://{notion.PUBLIC_HOST}/%2e%2e/{PAGE_ID}",
            f"https://{notion.PUBLIC_HOST}/%61{PAGE_ID[1:]}",
            f"https://{notion.PUBLIC_HOST}/other/{PAGE_ID}",
            f"https://{notion.PUBLIC_HOST}\\@example.com/{PAGE_ID}",
            f"https://{notion.PUBLIC_HOST}/ {PAGE_ID}",
            "javascript:alert(1)",
            None,
        ):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    notion.validate_source_url(value)

    def test_main_frame_redirects_are_restricted_to_source_pages(self):
        self.assertTrue(
            notion.resource_allowed(PAGE_URL, navigation=True, main_frame=True)
        )
        for url in (
            "https://www.notion.so/login",
            "https://challenges.cloudflare.com/verify",
            "https://example.com/",
            f"https://{notion.PUBLIC_HOST}/login",
        ):
            with self.subTest(url=url):
                self.assertFalse(
                    notion.resource_allowed(url, navigation=True, main_frame=True)
                )

    def test_browser_resources_exclude_arbitrary_hosts_credentials_and_ports(self):
        self.assertTrue(notion.resource_allowed(f"https://{notion.PUBLIC_HOST}/_assets/app.js"))
        self.assertTrue(notion.resource_allowed("https://www.notion.so/_assets/app.js"))
        for url in (
            "https://example.com/script.js",
            "http://www.notion.so/script.js",
            "https://www.notion.so:443/script.js",
            "https://user@www.notion.so/script.js",
            "https://www.notion.so.example.com/script.js",
            "https://127.0.0.1/script.js",
            "file:///restricted/example.txt",
        ):
            with self.subTest(url=url):
                self.assertFalse(notion.resource_allowed(url))

    def test_card_links_cannot_escape_the_source(self):
        card = notion._card_data(
            {"href": f"/{PAGE_ID}", "text": "Research internship\nNovember 30, 2026"}
        )
        self.assertEqual(card["source_url"], PAGE_URL)
        self.assertEqual(card["deadline_text"], "November 30, 2026")
        for href in ("https://example.com/job", "//example.com/job", "javascript:alert(1)"):
            with self.subTest(href=href):
                with self.assertRaises(ValueError):
                    notion._card_data({"href": href, "text": "Research internship"})

    def test_stable_card_id_uses_canonical_source_instead_of_external_href(self):
        card = notion._card_data(
            {"id": PAGE_UUID.upper(), "href": "https://example.com/job", "text": "Job"}
        )
        self.assertEqual(card["source_url"], PAGE_URL)


class PublicNotionNormalizationTests(SimpleTestCase):
    def setUp(self):
        self.card = {
            "source_url": PAGE_URL,
            "title": "Card title",
            "text": "Card title\nNovember 30, 2026",
            "deadline_text": "November 30, 2026",
        }

    def test_explicit_labeled_properties_map_to_candidate_fields(self):
        properties = {
            "Company:": "Example University",
            "Type": "Internship",
            "Location": "Example City / Remote",
            "Application deadline": "30.11.2026",
        }
        item = notion.normalize_listing(
            self.card,
            {
                "title": "Research internship",
                "properties": properties,
                "description": "Join our research team.",
                "text": "Full public source text",
            },
        )
        self.assertEqual(item["external_id"], PAGE_ID)
        self.assertEqual(item["title"], "Research internship")
        self.assertEqual(item["organization"], "Example University")
        self.assertEqual(item["type"], "internship")
        self.assertEqual(item["location"], "Example City / Remote")
        self.assertEqual(item["deadline"], date(2026, 11, 30))
        self.assertEqual(item["description"], "Join our research team.")
        self.assertEqual(item["raw_data"]["properties"], properties)
        self.assertEqual(item["raw_data"]["text"], "Full public source text")

    def test_ukrainian_property_labels_and_types_are_supported(self):
        item = notion.normalize_listing(
            self.card,
            {
                "properties": {
                    "Компанія": "Приклад",
                    "Тип": "Стажування",
                    "Місто": "Тестове місто",
                    "Дедлайн": "2026-11-30",
                }
            },
        )
        self.assertEqual(item["organization"], "Приклад")
        self.assertEqual(item["type"], "internship")
        self.assertEqual(item["location"], "Тестове місто")
        self.assertEqual(item["deadline"], date(2026, 11, 30))

    def test_observed_employment_types_and_format_properties_are_supported(self):
        for employment_type in ("Full-time", "Part-time"):
            with self.subTest(employment_type=employment_type):
                item = notion.normalize_listing(
                    self.card,
                    {"properties": {"Type": employment_type, "Format": "Remote"}},
                )
                self.assertEqual(item["type"], "job")
                self.assertEqual(item["location"], "")
                self.assertEqual(item["work_mode"], "remote")
                self.assertEqual(item["employment_type"], employment_type.lower().replace("-", "_"))

    def test_ambiguous_generic_prose_does_not_invent_employer_or_location(self):
        item = notion.normalize_listing(
            self.card,
            {
                "title": "Research opportunity",
                "description": "Our client is hiring interns for Example City.",
                "text": "Our client is hiring interns for Example City.",
            },
        )
        self.assertEqual(item["organization"], "")
        self.assertEqual(item["type"], "")
        self.assertEqual(item["location"], "")

    def test_rendered_property_label_value_pairs_are_supported(self):
        item = notion.normalize_listing(
            self.card,
            {"text": "Company\nExample University\nType\nJob\nLocation\nExample City"},
        )
        self.assertEqual(item["organization"], "Example University")
        self.assertEqual(item["type"], "job")
        self.assertEqual(item["location"], "Example City")

    def test_unknown_type_and_ambiguous_date_are_preserved_for_review(self):
        properties = {"Type": "Scholarship", "Deadline": "11/12/2026"}
        item = notion.normalize_listing(self.card, {"properties": properties})

        self.assertEqual(item["type"], "")
        self.assertIsNone(item["deadline"])
        self.assertEqual(item["raw_data"]["properties"], properties)
        self.assertIn("Unrecognized deadline: 11/12/2026", item["raw_data"]["warnings"])
        self.assertIn(
            "Opportunity type needs review: Scholarship", item["raw_data"]["warnings"]
        )

    def test_deadlines_require_a_known_format_with_an_explicit_year(self):
        for value in ("2026-11-30", "November 30, 2026", "Nov 30, 2026", "30.11.2026"):
            with self.subTest(value=value):
                self.assertEqual(notion.parse_deadline(value), date(2026, 11, 30))
        for value in ("", "Empty", "November 30", "11/12/2026", "2026-02-30", "tomorrow"):
            with self.subTest(value=value):
                self.assertIsNone(notion.parse_deadline(value))

    def test_explicit_organization_introduction_retains_review_evidence(self):
        description = "Example Labs is an international research company. Join the team."
        item = notion.normalize_listing(
            self.card, {"description": description, "properties": {"Type": "Full-time"}}
        )
        self.assertEqual(item["organization"], "Example Labs")
        self.assertEqual(item["type"], "job")
        self.assertIn("Example Labs is an", item["raw_data"]["organization_evidence"])
        self.assertTrue(item["raw_data"]["warnings"])

    def test_generic_introductions_do_not_invent_an_employer(self):
        for text in (
            "Our client is an international technology company.",
            "This role is an opportunity to join a company.",
            "The company is hiring interns.",
        ):
            with self.subTest(text=text):
                self.assertEqual(notion.explicit_organization_introduction(text), ("", ""))

    def test_closed_and_expired_cards_are_skipped_but_today_is_included(self):
        today = date(2026, 11, 8)
        self.assertFalse(notion._active_card({"text": "Job\nClosed"}, today=today))
        self.assertFalse(notion._active_card(
            {"text": "Job\nOpen", "deadline_text": "November 7, 2026"}, today=today
        ))
        self.assertTrue(notion._active_card(
            {"text": "Job\nOpen", "deadline_text": "November 8, 2026"}, today=today
        ))
        self.assertTrue(notion._active_card({"text": "Job\nOpen"}, today=today))


class PublicNotionCollectionTests(SimpleTestCase):
    def page_with_cards(self, cards):
        page = Mock()
        container = Mock()
        page.locator.return_value = container
        container.count.return_value = 1
        container.first = container
        container.evaluate.side_effect = lambda script: cards if script == notion.CARD_SCRIPT else True
        container.get_by_role.return_value.count.return_value = 0
        container.locator.return_value.all.return_value = []
        return page, container

    def test_empty_collection_is_not_reported_as_success(self):
        page, _ = self.page_with_cards([])
        with patch.object(notion.time, "monotonic", return_value=0):
            with self.assertRaisesMessage(notion.NotionImportError, "No recognizable listing"):
                notion._discover_cards(page, limit=None, stop_at=100)

    def test_missing_known_collection_fails_closed(self):
        page, container = self.page_with_cards([])
        container.count.return_value = 0
        with self.assertRaisesMessage(notion.NotionImportError, "Expected one"):
            notion._discover_cards(page, limit=None, stop_at=100)

    def test_nested_containers_for_the_same_known_database_are_supported(self):
        page, container = self.page_with_cards(
            [{"id": PAGE_ID, "text": "Research internship"}]
        )
        container.count.return_value = 4
        with patch.object(notion.time, "monotonic", return_value=0):
            cards, complete, _ = notion._discover_cards(page, limit=None, stop_at=100)
        self.assertTrue(complete)
        self.assertEqual(len(cards), 1)
        page.locator.assert_called_with(notion.COLLECTION_SELECTOR)

    def test_repeated_cards_are_deduplicated_and_stable_end_is_detected(self):
        raw = {"id": PAGE_UUID, "text": "Research internship"}
        page, _ = self.page_with_cards([raw, raw])
        with patch.object(notion.time, "monotonic", return_value=0):
            cards, complete, warnings = notion._discover_cards(page, limit=None, stop_at=100)
        self.assertTrue(complete)
        self.assertEqual(len(cards), 1)
        self.assertEqual(warnings, [])

    def test_later_loaded_cards_are_collected_before_reporting_completion(self):
        first = {"id": PAGE_ID, "text": "First listing"}
        second = {"id": "b" * 32, "text": "Later listing"}
        page, container = self.page_with_cards([])
        snapshots = iter(([first], [first, second]))

        def evaluate(script):
            if script == notion.CARD_SCRIPT:
                return next(snapshots, [first, second])
            return True

        container.evaluate.side_effect = evaluate
        with patch.object(notion.time, "monotonic", return_value=0):
            cards, complete, warnings = notion._discover_cards(page, limit=None, stop_at=100)
        self.assertTrue(complete)
        self.assertEqual(len(cards), 2)
        self.assertEqual(warnings, [])

    def test_explicit_limit_reports_requested_batch_without_claiming_full_source(self):
        page, _ = self.page_with_cards(
            [{"id": PAGE_ID, "text": "One"}, {"id": "b" * 32, "text": "Two"}]
        )
        with patch.object(notion.time, "monotonic", return_value=0):
            cards, complete, warnings = notion._discover_cards(page, limit=1, stop_at=100)
        self.assertTrue(complete)
        self.assertEqual(len(cards), 1)
        self.assertIn("does not verify the entire database", warnings[0])

    def test_time_budget_exhaustion_returns_incomplete_collection(self):
        page, _ = self.page_with_cards([{ "id": PAGE_ID, "text": "One"}])
        with patch.object(notion.time, "monotonic", side_effect=[0, 101]):
            cards, complete, warnings = notion._discover_cards(page, limit=None, stop_at=100)
        self.assertFalse(complete)
        self.assertEqual(len(cards), 1)
        self.assertIn("time budget", warnings[0])

    def test_card_safety_failure_stops_collection(self):
        page, _ = self.page_with_cards([{ "href": "https://example.com/job", "text": "One"}])
        with patch.object(notion.time, "monotonic", return_value=0):
            with self.assertRaisesMessage(notion.NotionImportError, "invalid source"):
                notion._discover_cards(page, limit=None, stop_at=100)

    def test_security_challenge_is_an_error_with_retry_information(self):
        page = Mock(url=PAGE_URL)
        page.title.return_value = "Just a moment"
        page.locator.return_value.inner_text.return_value = "Verify you are human"
        response = SimpleNamespace(status=403, headers={"retry-after": "60"})
        with self.assertRaisesMessage(notion.NotionImportError, "Retry-After: 60"):
            notion._check_page(page, response)

    def test_successful_verification_does_not_reuse_the_old_challenge_response(self):
        page = Mock(url=PAGE_URL)
        page.title.return_value = "Research internship"
        page.locator.return_value.inner_text.return_value = "The public listing has loaded."
        original_response = SimpleNamespace(status=429, headers={})
        with patch.dict(sys.modules, {"playwright.sync_api": SimpleNamespace(TimeoutError=TimeoutError)}):
            notion._wait_for_page(page, original_response)

    def test_unsafe_redirect_is_rejected_before_reading_page_content(self):
        page = Mock(url="https://example.com/job")
        with self.assertRaises(ValueError):
            notion._check_page(page)
        page.title.assert_not_called()

    def test_missing_browser_dependency_has_actionable_error(self):
        with (
            patch.object(notion, "PUBLIC_HOST", "notion.example.org"),
            patch.object(notion, "SOURCE_URL", "https://notion.example.org/" + PAGE_ID),
            patch.dict(sys.modules, {"playwright.sync_api": None}),
        ):
            with self.assertRaisesMessage(notion.NotionImportError, "requirements-importers.txt"):
                notion.scrape_public_notion()

    def test_placeholder_source_is_blocked_before_browser_import(self):
        with self.assertRaisesMessage(notion.NotionImportError, "placeholder Notion source"):
            notion.scrape_public_notion()

    def test_invalid_options_fail_before_browser_import(self):
        for kwargs in ({"limit": 0}, {"limit": True}, {"limit": 501}, {"browser_channel": "other"}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    notion.scrape_public_notion(**kwargs)
