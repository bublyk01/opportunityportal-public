from datetime import date
from io import StringIO
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, override_settings

from opportunities.services.gmail_browser import GmailImportError
from opportunities.services.gmail_source import get_account, message_permalink
from scripts import run_gmail_sync


COMMAND_MODULE = "opportunities.management.commands.sync_gmail"
PRIVATE_CONTENT = "Private mailbox body sentinel"


@override_settings(GMAIL_ACCOUNT_EMAIL="owner@example.edu")
class GmailCommandTests(SimpleTestCase):
    def setUp(self):
        self.account = get_account()
        self.item = {
            "external_id": "19a8000000000001",
            "source_url": message_permalink("19a8000000000001"),
            "title": "Research internship", "organization": "Example University",
            "type": "internship", "description": PRIVATE_CONTENT,
            "deadline": date(2026, 11, 30),
            "raw_data": {"text": PRIVATE_CONTENT, "warnings": ["Private warning sentinel"]},
        }
        self.result = SimpleNamespace(
            items=[self.item], complete=True, discovered=4, skipped=3,
            warnings=["Private warning sentinel"],
            window_start="2026-10-14", window_end="2026-11-13",
        )
        for name, function in (("scrape", "scrape_gmail"), ("login", "login_gmail"), ("persist", "import_candidates")):
            patcher = patch(f"{COMMAND_MODULE}.{function}")
            setattr(self, name, patcher.start())
            self.addCleanup(patcher.stop)
        self.scrape.return_value = self.result
        self.summary = {
            "created": 1, "staged": 0, "unchanged": 0, "changed": 0,
            "errors": 0, "messages": ["Private validation sentinel"],
        }
        self.persist.return_value = self.summary

    def command(self, *arguments, **options):
        stdout = StringIO()
        call_command("sync_gmail", *arguments, stdout=stdout, **options)
        return stdout.getvalue()

    def test_default_preview_scans_authorized_inbox_window_without_database_writes(self):
        output = self.command()

        self.scrape.assert_called_once_with(days=30, limit=None, browser_channel="chrome", headed=False)
        self.persist.assert_not_called()
        self.login.assert_not_called()
        self.assertIn("Preview only", output)
        self.assertIn("Research internship", output)
        self.assertIn("requested Inbox window complete", output)
        self.assertNotIn(PRIVATE_CONTENT, output)

    def test_explicit_json_preview_retains_original_evidence_and_serializes_dates(self):
        payload = json.loads(self.command("--json"))

        self.assertEqual(payload["mode"], "preview")
        self.assertEqual(payload["source"], self.account.source_key)
        self.assertEqual(payload["scope"], "inbox")
        self.assertEqual(payload["days"], 30)
        self.assertTrue(payload["complete"])
        self.assertTrue(payload["full_window"])
        self.assertEqual(payload["coverage"], "inbox_window")
        self.assertEqual(payload["discovered"], 4)
        self.assertEqual(payload["skipped"], 3)
        self.assertEqual(payload["collected"], 1)
        self.assertEqual(payload["items"][0]["description"], PRIVATE_CONTENT)
        self.assertEqual(payload["items"][0]["deadline"], "2026-11-30")
        self.assertNotIn("summary", payload)
        self.persist.assert_not_called()

    def test_import_uses_configured_account_provenance_and_reports_outcomes(self):
        payload = json.loads(self.command("--import", "--json"))

        self.persist.assert_called_once_with([self.item], source_key=self.account.source_key, source="gmail")
        self.assertEqual(payload["mode"], "import")
        self.assertEqual(payload["summary"], self.summary)

    def test_normal_import_output_is_aggregate_only(self):
        output = self.command("--import")

        self.assertIn("4 messages read", output)
        self.assertIn("1 drafts", output)
        for value in (self.item["title"], self.item["organization"], self.item["source_url"], PRIVATE_CONTENT, "Private warning sentinel", "Private validation sentinel"):
            with self.subTest(value=value):
                self.assertNotIn(value, output)

    def test_summary_only_json_omits_individual_data_and_raw_validation_messages(self):
        payload = json.loads(self.command("--import", "--json", "--summary-only", verbosity=2))

        self.assertNotIn("items", payload)
        self.assertNotIn("warnings", payload)
        self.assertNotIn("messages", payload["summary"])
        self.assertEqual(payload["warning_count"], 1)
        self.assertEqual(payload["summary"]["created"], 1)
        self.assertNotIn("progress", self.scrape.call_args.kwargs)
        self.assertNotIn(PRIVATE_CONTENT, json.dumps(payload))

    def test_summary_only_preview_does_not_print_titles_or_mail_content(self):
        output = self.command("--summary-only")

        self.assertIn("opportunity candidates", output)
        self.assertNotIn(self.item["title"], output)
        self.assertNotIn(PRIVATE_CONTENT, output)
        self.persist.assert_not_called()

    def test_requested_batch_is_never_reported_as_a_complete_window(self):
        payload = json.loads(self.command("--limit", "1", "--json"))

        self.assertTrue(payload["complete"])
        self.assertFalse(payload["full_window"])
        self.assertEqual(payload["coverage"], "requested_batch")
        self.assertEqual(payload["limit"], 1)
        self.assertEqual(self.scrape.call_args.kwargs["limit"], 1)

    def test_each_run_scans_window_again_without_a_cursor(self):
        self.command("--import")
        self.command("--import")

        self.assertEqual(self.persist.call_count, 2)
        self.assertEqual(self.scrape.call_count, 2)
        self.assertTrue(all(entry.kwargs == {
            "days": 30, "limit": None, "browser_channel": "chrome", "headed": False,
        } for entry in self.scrape.call_args_list))

    def test_incomplete_collection_never_imports_even_for_an_explicit_batch(self):
        self.result.complete = False
        for arguments in (("--import",), ("--import", "--limit", "1")):
            with self.subTest(arguments=arguments):
                with self.assertRaisesMessage(CommandError, "Collection is incomplete"):
                    self.command(*arguments)
        self.persist.assert_not_called()

    def test_incomplete_json_preview_reports_diagnostics_and_fails(self):
        self.result.complete = False
        stdout = StringIO()
        with self.assertRaises(CommandError):
            call_command("sync_gmail", "--json", stdout=stdout)
        payload = json.loads(stdout.getvalue())
        self.assertFalse(payload["complete"])
        self.assertFalse(payload["full_window"])
        self.assertEqual(payload["warnings"], self.result.warnings)
        self.persist.assert_not_called()

    def test_import_errors_return_nonzero_after_structured_report(self):
        self.summary.update(created=0, errors=1)
        stdout = StringIO()
        with self.assertRaises(CommandError):
            call_command("sync_gmail", "--import", "--json", stdout=stdout)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["summary"]["errors"], 1)

    def test_expected_collector_errors_are_clear_and_do_not_import(self):
        for error in (GmailImportError("Sign in to the configured mailbox."), ValueError("Invalid mailbox state.")):
            with self.subTest(error=error):
                self.scrape.side_effect = error
                with self.assertRaisesMessage(CommandError, str(error)):
                    self.command("--import")
        self.persist.assert_not_called()

    def test_summary_only_error_does_not_include_raw_mail_content(self):
        self.scrape.side_effect = GmailImportError(PRIVATE_CONTENT)
        with self.assertRaises(CommandError) as error:
            self.command("--import", "--summary-only")
        self.assertNotIn(PRIVATE_CONTENT, str(error.exception))
        self.persist.assert_not_called()

    def test_unexpected_programming_errors_are_not_hidden_in_normal_command(self):
        self.scrape.side_effect = RuntimeError("Unexpected collector failure")
        with self.assertRaisesMessage(RuntimeError, "Unexpected collector failure"):
            self.command("--import")
        self.persist.assert_not_called()

    def test_scope_options_are_validated_before_browser_access(self):
        for options in (
            {"days": 0}, {"days": 31}, {"days": True},
            {"limit": 0}, {"limit": 1001}, {"limit": True},
            {"browser_channel": "unexpected"},
        ):
            with self.subTest(options=options):
                with self.assertRaises(CommandError):
                    self.command(**options)
        self.scrape.assert_not_called()
        self.login.assert_not_called()
        self.persist.assert_not_called()

    def test_arbitrary_mailbox_folder_or_query_options_are_not_available(self):
        for option in ("--account", "--folder", "--query"):
            with self.subTest(option=option):
                with self.assertRaises(CommandError):
                    self.command(option, "outside-authorized-scope")
        self.scrape.assert_not_called()
        self.login.assert_not_called()

    @override_settings(GMAIL_ACCOUNT_EMAIL="")
    def test_unconfigured_account_fails_before_opening_browser(self):
        with self.assertRaisesMessage(CommandError, "GMAIL_ACCOUNT_EMAIL"):
            self.command()
        self.scrape.assert_not_called()
        self.login.assert_not_called()

    def test_browser_days_limit_and_headed_options_are_forwarded(self):
        self.command("--days", "7", "--limit", "5", "--browser", "msedge", "--headed")
        self.scrape.assert_called_once_with(days=7, limit=5, browser_channel="msedge", headed=True)

    def test_verbose_progress_goes_to_stderr_and_json_stays_valid(self):
        stderr = StringIO()
        payload = json.loads(self.command("--json", verbosity=2, stderr=stderr))
        self.assertTrue(payload["complete"])
        self.scrape.call_args.kwargs["progress"]("Reading Inbox messages...")
        self.assertIn("Reading Inbox messages...", stderr.getvalue())

    def test_login_does_not_collect_or_import_and_reports_authentication(self):
        payload = json.loads(self.command("--login", "--browser", "msedge", "--json"))
        self.login.assert_called_once_with(browser_channel="msedge")
        self.assertEqual(payload, {"mode": "login", "source": self.account.source_key, "authenticated": True})
        self.scrape.assert_not_called()
        self.persist.assert_not_called()

    def test_login_import_combination_and_failed_login_never_import(self):
        with self.assertRaises(CommandError):
            self.command("--login", "--import")
        self.login.assert_not_called()
        self.login.side_effect = GmailImportError("Configured account is unavailable.")
        with self.assertRaisesMessage(CommandError, "Configured account is unavailable."):
            self.command("--login")
        self.scrape.assert_not_called()
        self.persist.assert_not_called()


class GmailScheduledWorkerTests(SimpleTestCase):
    def run_worker(self, *, output=None, error=None):
        payload = output if output is not None else json.dumps({
            "mode": "import", "complete": True, "discovered": 4, "skipped": 3, "collected": 1,
            "summary": {"created": 1, "staged": 0, "unchanged": 0, "changed": 0, "errors": 0},
            # Even an accidental richer command report must not reach the log.
            "items": [{"description": PRIVATE_CONTENT}], "warnings": [PRIVATE_CONTENT],
        })

        def command(*args, **kwargs):
            if payload:
                kwargs["stdout"].write(payload)
            kwargs["stderr"].write(PRIVATE_CONTENT)
            if error is not None:
                raise error

        with TemporaryDirectory() as directory:
            with (
                patch.object(run_gmail_sync, "PROJECT_ROOT", Path(directory)),
                patch.object(run_gmail_sync.sys, "path", list(run_gmail_sync.sys.path)),
                patch.dict(os.environ, {"DJANGO_SETTINGS_MODULE": "config.settings"}),
                patch("django.setup"),
                patch("django.core.management.call_command", side_effect=command) as mock_command,
            ):
                result = run_gmail_sync.main()
            log = (Path(directory) / ".logs" / "gmail-sync.log").read_text(encoding="utf-8")
        self.assertNotIn(PRIVATE_CONTENT, log)
        return result, log, mock_command

    def test_worker_imports_headless_and_logs_only_numeric_aggregates(self):
        result, log, command = self.run_worker()
        self.assertEqual(result, 0)
        self.assertEqual(command.call_args.args, ("sync_gmail",))
        self.assertTrue(command.call_args.kwargs["commit"])
        self.assertTrue(command.call_args.kwargs["summary_only"])
        self.assertTrue(command.call_args.kwargs["as_json"])
        self.assertFalse(command.call_args.kwargs["headed"])
        self.assertIn("Read 4 messages; 1 candidates; 3 skipped", log)
        self.assertIn("1 drafts", log)
        self.assertIn("Gmail import finished successfully.", log)

    def test_handled_and_unexpected_failures_never_log_exception_content(self):
        for error in (CommandError(PRIVATE_CONTENT), RuntimeError(PRIVATE_CONTENT)):
            with self.subTest(error=type(error).__name__):
                result, log, _ = self.run_worker(output="", error=error)
                self.assertEqual(result, 1)
                self.assertIn("Gmail import failed", log)
                self.assertNotIn("finished successfully", log)
                self.assertNotIn("Traceback", log)

    def test_incomplete_collection_report_cannot_be_logged_as_success(self):
        result, log, _ = self.run_worker(output=json.dumps({
            "mode": "import", "complete": False, "discovered": 2, "skipped": 1, "collected": 1,
        }))
        self.assertEqual(result, 1)
        self.assertIn("collection incomplete", log)
        self.assertNotIn("finished successfully", log)

    def test_import_error_counts_fail_worker_even_if_command_returns_normally(self):
        result, log, _ = self.run_worker(output=json.dumps({
            "mode": "import", "complete": True, "discovered": 2, "skipped": 1, "collected": 1,
            "summary": {"created": 0, "staged": 0, "unchanged": 0, "changed": 0, "errors": 1},
        }))
        self.assertEqual(result, 1)
        self.assertIn("1 errors", log)
        self.assertNotIn("finished successfully", log)

    def test_invalid_or_non_numeric_output_is_rejected_without_logging_it(self):
        for output in (
            PRIVATE_CONTENT,
            json.dumps({"mode": "import", "complete": True, "discovered": PRIVATE_CONTENT, "skipped": 0, "collected": 0}),
            json.dumps({"mode": "import", "complete": True, "discovered": 0, "skipped": 0, "collected": 0}),
        ):
            with self.subTest(output=output):
                result, log, _ = self.run_worker(output=output)
                self.assertEqual(result, 1)
                self.assertIn("Gmail import failed", log)
                self.assertNotIn("finished successfully", log)
