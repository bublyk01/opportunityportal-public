import json
from datetime import date
from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase

from opportunities.services.notion_public import NotionImportError


COMMAND_MODULE = "opportunities.management.commands.sync_notion"


class SyncNotionCommandTests(SimpleTestCase):
    def setUp(self):
        self.item = {
            "external_id": "a" * 32,
            "source_url": "https://notion.example.invalid/" + "a" * 32,
            "title": "Research internship",
            "organization": "Example University",
            "type": "internship",
            "description": "Work with a research team.",
            "location": "Example City",
            "deadline": date(2026, 11, 30),
            "raw_data": {"text": "Research internship"},
        }
        self.result = SimpleNamespace(
            items=[self.item], complete=True, discovered=1, warnings=[]
        )
        self.scrape_patch = patch(f"{COMMAND_MODULE}.scrape_public_notion")
        self.import_patch = patch(f"{COMMAND_MODULE}.import_candidates")
        self.scrape = self.scrape_patch.start()
        self.persist = self.import_patch.start()
        self.addCleanup(self.scrape_patch.stop)
        self.addCleanup(self.import_patch.stop)
        self.scrape.return_value = self.result
        self.summary = {
            "created": 1,
            "staged": 0,
            "unchanged": 0,
            "changed": 0,
            "errors": 0,
            "messages": [],
        }
        self.persist.return_value = self.summary

    def command(self, *args, **options):
        stdout = StringIO()
        call_command("sync_notion", *args, stdout=stdout, **options)
        return stdout.getvalue()

    def test_default_preview_never_calls_persistence(self):
        output = self.command()

        self.persist.assert_not_called()
        self.scrape.assert_called_once_with(limit=None, browser_channel="chrome")
        self.assertIn("Preview only: no database records were changed.", output)
        self.assertIn("Research internship", output)
        self.assertIn("Example University", output)
        self.assertIn("internship", output)
        self.assertIn("2026-11-30", output)
        self.assertIn(self.item["source_url"], output)

    def test_json_preview_is_valid_and_serializes_dates(self):
        payload = json.loads(self.command("--json"))

        self.persist.assert_not_called()
        self.assertEqual(payload["mode"], "preview")
        self.assertTrue(payload["complete"])
        self.assertEqual(payload["collected"], 1)
        self.assertEqual(payload["items"][0]["deadline"], "2026-11-30")
        self.assertNotIn("summary", payload)

    def test_explicit_import_calls_persistence_once(self):
        payload = json.loads(self.command("--import", "--json"))

        self.persist.assert_called_once_with(
            [self.item], source_key="example-opportunities"
        )
        self.assertEqual(payload["mode"], "import")
        self.assertEqual(payload["summary"], self.summary)

    def test_human_import_report_distinguishes_staged_and_changed_items(self):
        self.summary.update(created=0, staged=1, changed=2)
        output = self.command("--import")
        self.assertIn("0 drafts created", output)
        self.assertIn("1 staged for review", output)
        self.assertIn("2 source changes flagged", output)

    def test_incomplete_import_never_calls_persistence_even_for_limited_batch(self):
        self.result.complete = False
        self.result.warnings = ["Detail page did not finish loading."]
        for args in (("--import",), ("--import", "--limit", "1")):
            with self.subTest(args=args):
                with self.assertRaisesMessage(CommandError, "Collection is incomplete"):
                    self.command(*args)
        self.persist.assert_not_called()

    def test_incomplete_preview_reports_failure_with_json_details(self):
        self.result.complete = False
        self.result.warnings = ["Pagination did not reach the end."]
        stdout = StringIO()
        with self.assertRaisesMessage(CommandError, "Collection is incomplete"):
            call_command("sync_notion", "--json", stdout=stdout)

        payload = json.loads(stdout.getvalue())
        self.assertFalse(payload["complete"])
        self.assertEqual(payload["warnings"], self.result.warnings)
        self.persist.assert_not_called()

    def test_expected_scraper_errors_become_command_errors_without_writes(self):
        for error in (
            NotionImportError("Browser is unavailable."),
            ValueError("Unsafe source URL."),
        ):
            with self.subTest(error=error):
                self.scrape.side_effect = error
                with self.assertRaisesMessage(CommandError, str(error)):
                    self.command("--import")
        self.persist.assert_not_called()

    def test_unexpected_scraper_errors_are_not_hidden(self):
        self.scrape.side_effect = RuntimeError("Programming failure")
        with self.assertRaisesMessage(RuntimeError, "Programming failure"):
            self.command("--import")
        self.persist.assert_not_called()

    def test_limit_and_browser_are_forwarded(self):
        self.result.warnings = ["Requested batch of 2 listings; source has more."]
        payload = json.loads(
            self.command("--limit", "2", "--browser", "msedge", "--json")
        )
        self.scrape.assert_called_once_with(limit=2, browser_channel="msedge")
        self.assertEqual(payload["limit"], 2)
        self.assertEqual(payload["warnings"], self.result.warnings)

    def test_headed_option_is_explicitly_forwarded(self):
        self.command("--headed")
        self.scrape.assert_called_once_with(limit=None, browser_channel="chrome", headed=True)

    def test_invalid_limits_are_rejected_before_collection(self):
        for limit in (-1, 0, 501):
            with self.subTest(limit=limit):
                with self.assertRaisesMessage(CommandError, "--limit must"):
                    self.command(limit=limit)
        self.scrape.assert_not_called()
        self.persist.assert_not_called()

    def test_invalid_browser_is_rejected_before_collection(self):
        with self.assertRaisesMessage(CommandError, "--browser must"):
            self.command(browser_channel="unexpected")
        self.scrape.assert_not_called()

    def test_import_validation_errors_produce_nonzero_exit_after_report(self):
        self.summary.update(created=0, errors=1, messages=["Invalid deadline."])
        stdout = StringIO()
        with self.assertRaisesMessage(CommandError, "Some listings could not"):
            call_command("sync_notion", "--import", "--json", stdout=stdout)

        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["summary"]["errors"], 1)
        self.assertEqual(payload["summary"]["messages"], ["Invalid deadline."])
