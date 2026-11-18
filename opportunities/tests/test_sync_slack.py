import json
from datetime import date
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import call, patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, override_settings

from opportunities.services.slack_browser import SlackImportError
from opportunities.services.slack_source import CHANNEL_URL, SOURCE_KEY, get_channel, get_channels, message_permalink


COMMAND_MODULE = "opportunities.management.commands.sync_slack"
SECOND_CHANNEL_URL = "https://workspace.slack.example.invalid/archives/C2222222222"


@override_settings(SLACK_CHANNEL_URLS=(CHANNEL_URL,))
class SyncSlackCommandTests(SimpleTestCase):
    def setUp(self):
        self.channel = get_channel()
        self.item = {
            "external_id": "1794000000.000001",
            "source_url": message_permalink("1794000000.000001"),
            "title": "Research internship",
            "organization": "Example University",
            "type": "internship",
            "description": "Join the research team.",
            "location": "Example City",
            "deadline": date(2026, 11, 30),
            "raw_data": {"text": "Original announcement", "warnings": ["Review extracted details."]},
        }
        self.result = SimpleNamespace(
            items=[self.item], complete=True, discovered=3, skipped=2,
            warnings=["Thread replies are not scanned."],
            latest_ts="1794000000.000001", window_start="1791408000",
        )
        for attribute, function in (
            ("scrape", "scrape_slack"), ("persist", "import_candidates"),
            ("login", "login_slack"), ("read_checkpoint", "read_checkpoint"),
            ("write_checkpoint", "write_checkpoint"),
        ):
            patcher = patch(f"{COMMAND_MODULE}.{function}")
            setattr(self, attribute, patcher.start())
            self.addCleanup(patcher.stop)
        self.scrape.return_value = self.result
        self.read_checkpoint.return_value = "1793900000.000001"
        self.summary = {
            "created": 1, "staged": 0, "unchanged": 0, "changed": 0,
            "errors": 0, "messages": [],
        }
        self.persist.return_value = self.summary

    def command(self, *args, **options):
        stdout = StringIO()
        call_command("sync_slack", *args, stdout=stdout, **options)
        return stdout.getvalue()

    def test_default_preview_does_not_read_or_advance_checkpoint_or_import(self):
        output = self.command()

        self.scrape.assert_called_once_with(
            days=30, limit=None, browser_channel="chrome", headed=False, since=None, channel=self.channel,
        )
        self.persist.assert_not_called()
        self.read_checkpoint.assert_not_called()
        self.write_checkpoint.assert_not_called()
        self.login.assert_not_called()
        self.assertIn("Research internship", output)
        self.assertIn("Example University", output)
        self.assertIn("Preview", output)

    def test_json_preview_reports_messages_candidates_skips_and_original_links(self):
        payload = json.loads(self.command("--json"))

        self.assertEqual(payload["mode"], "preview")
        self.assertTrue(payload["complete"])
        self.assertEqual(payload["discovered"], 3)
        self.assertEqual(payload["skipped"], 2)
        self.assertEqual(payload["collected"], 1)
        self.assertEqual(payload["warnings"], self.result.warnings)
        self.assertEqual(payload["items"][0]["deadline"], "2026-11-30")
        self.assertEqual(payload["items"][0]["source_url"], self.item["source_url"])
        self.assertNotIn("summary", payload)
        self.persist.assert_not_called()
        self.write_checkpoint.assert_not_called()

    def test_complete_import_uses_slack_provenance_and_advances_checkpoint(self):
        payload = json.loads(self.command("--import", "--json"))

        self.read_checkpoint.assert_called_once_with(channel=self.channel)
        self.scrape.assert_called_once_with(
            days=30, limit=None, browser_channel="chrome", headed=False,
            since="1793900000.000001", channel=self.channel,
        )
        self.persist.assert_called_once_with(
            [self.item], source_key=SOURCE_KEY, source="slack",
        )
        self.write_checkpoint.assert_called_once_with(self.result.latest_ts, channel=self.channel)
        self.assertEqual(payload["mode"], "import")
        self.assertEqual(payload["summary"], self.summary)

    def test_rescan_import_bypasses_checkpoint_read(self):
        self.command("--import", "--rescan", "--days", "7")

        self.read_checkpoint.assert_not_called()
        self.scrape.assert_called_once_with(
            days=7, limit=None, browser_channel="chrome", headed=False, since=None, channel=self.channel,
        )
        self.write_checkpoint.assert_called_once_with(self.result.latest_ts, channel=self.channel)

    def test_explicit_batch_never_advances_checkpoint(self):
        self.command("--import", "--limit", "10")

        self.persist.assert_called_once()
        self.write_checkpoint.assert_not_called()
        self.assertEqual(self.scrape.call_args.kwargs["limit"], 10)

    def test_empty_completed_scan_does_not_replace_existing_checkpoint(self):
        self.result.items = []
        self.result.discovered = self.result.skipped = 0
        self.result.latest_ts = None
        self.command("--import")

        self.persist.assert_called_once_with([], source_key=SOURCE_KEY, source="slack")
        self.write_checkpoint.assert_not_called()

    def test_completed_scan_with_only_unrelated_messages_can_advance_checkpoint(self):
        self.result.items = []
        self.result.skipped = self.result.discovered
        self.command("--import")

        self.persist.assert_called_once_with([], source_key=SOURCE_KEY, source="slack")
        self.write_checkpoint.assert_called_once_with(self.result.latest_ts, channel=self.channel)

    def test_incomplete_scan_never_imports_or_advances_even_for_requested_batch(self):
        self.result.complete = False
        self.result.warnings = ["Older history did not reach the requested boundary."]
        for arguments in (("--import",), ("--import", "--limit", "10")):
            with self.subTest(arguments=arguments):
                with self.assertRaises(CommandError):
                    self.command(*arguments)

        self.persist.assert_not_called()
        self.write_checkpoint.assert_not_called()

    def test_incomplete_json_preview_retains_diagnostics_and_fails(self):
        self.result.complete = False
        self.result.warnings = ["Slack limited visible history."]
        stdout = StringIO()
        with self.assertRaises(CommandError):
            call_command("sync_slack", "--json", stdout=stdout)

        payload = json.loads(stdout.getvalue())
        self.assertFalse(payload["complete"])
        self.assertEqual(payload["warnings"], self.result.warnings)
        self.persist.assert_not_called()
        self.write_checkpoint.assert_not_called()

    def test_import_errors_are_reported_without_advancing_checkpoint(self):
        self.summary.update(created=0, errors=1, messages=["Invalid source identity."])
        stdout = StringIO()
        with self.assertRaises(CommandError):
            call_command("sync_slack", "--import", "--json", stdout=stdout)

        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["summary"]["errors"], 1)
        self.assertEqual(payload["summary"]["messages"], ["Invalid source identity."])
        self.write_checkpoint.assert_not_called()

    def test_expected_browser_errors_do_not_call_persistence_or_checkpoint_writer(self):
        for error in (SlackImportError("Sign in first."), ValueError("Invalid scan boundary.")):
            with self.subTest(error=error):
                self.scrape.side_effect = error
                with self.assertRaisesMessage(CommandError, str(error)):
                    self.command("--import")

        self.persist.assert_not_called()
        self.write_checkpoint.assert_not_called()

    def test_unexpected_collector_errors_are_not_hidden(self):
        self.scrape.side_effect = RuntimeError("Unexpected programming failure.")
        with self.assertRaisesMessage(RuntimeError, "Unexpected programming failure."):
            self.command("--import")

        self.persist.assert_not_called()
        self.write_checkpoint.assert_not_called()

    def test_browser_days_headed_and_limit_are_explicitly_forwarded(self):
        self.command("--days", "7", "--limit", "15", "--browser", "msedge", "--headed")

        self.scrape.assert_called_once_with(
            days=7, limit=15, browser_channel="msedge", headed=True, since=None, channel=self.channel,
        )

    def test_verbose_progress_uses_stderr_without_corrupting_json_stdout(self):
        stderr = StringIO()
        payload = json.loads(self.command("--json", verbosity=2, stderr=stderr))

        self.assertTrue(payload["complete"])
        progress = self.scrape.call_args.kwargs["progress"]
        progress("Reading channel messages...")
        self.assertIn("Reading channel messages...", stderr.getvalue())

    def test_invalid_ranges_and_browser_are_rejected_before_browser_access(self):
        for options in (
            {"days": 0}, {"days": 366}, {"days": True},
            {"limit": 0}, {"limit": 1001}, {"limit": True},
            {"browser_channel": "unexpected"},
        ):
            with self.subTest(options=options):
                with self.assertRaises(CommandError):
                    self.command(**options)

        self.scrape.assert_not_called()
        self.login.assert_not_called()
        self.read_checkpoint.assert_not_called()
        self.persist.assert_not_called()
        self.write_checkpoint.assert_not_called()

    def test_login_only_opens_interactive_browser_and_reports_success(self):
        output = self.command("--login", "--browser", "msedge")

        self.login.assert_called_once_with(browser_channel="msedge", channel=self.channel)
        self.assertIn("Sign-in complete", output)
        self.scrape.assert_not_called()
        self.persist.assert_not_called()
        self.read_checkpoint.assert_not_called()
        self.write_checkpoint.assert_not_called()

    def test_login_json_reports_authentication_without_message_content(self):
        payload = json.loads(self.command("--login", "--json"))

        self.assertEqual(payload["mode"], "login")
        self.assertTrue(payload["authenticated"])
        self.assertNotIn("items", payload)
        self.persist.assert_not_called()

    def test_login_and_import_cannot_be_combined(self):
        with self.assertRaises(CommandError):
            self.command("--login", "--import")

        self.login.assert_not_called()
        self.scrape.assert_not_called()
        self.persist.assert_not_called()
        self.read_checkpoint.assert_not_called()

    def test_failed_login_does_not_claim_authentication_or_import(self):
        self.login.side_effect = SlackImportError("The channel is not accessible.")
        stdout = StringIO()
        with self.assertRaisesMessage(CommandError, "The channel is not accessible."):
            call_command("sync_slack", "--login", stdout=stdout)

        self.assertNotIn("Sign-in complete", stdout.getvalue())
        self.scrape.assert_not_called()
        self.persist.assert_not_called()
        self.write_checkpoint.assert_not_called()


@override_settings(SLACK_CHANNEL_URLS=(CHANNEL_URL, SECOND_CHANNEL_URL))
class MultiChannelSyncSlackCommandTests(SimpleTestCase):
    command = SyncSlackCommandTests.command

    def setUp(self):
        SyncSlackCommandTests.setUp(self)
        self.first, self.second = get_channels()
        self.second_item = {
            **self.item,
            "source_url": message_permalink(self.item["external_id"], channel=self.second),
        }
        self.second_result = SimpleNamespace(**{**vars(self.result), "items": [self.second_item]})
        self.scrape.side_effect = lambda **kwargs: (
            self.result if kwargs["channel"] == self.first else self.second_result
        )
        self.read_checkpoint.side_effect = lambda *, channel: (
            "1793900000.000001" if channel == self.first else None
        )

    def test_default_preview_scans_configured_channels_in_order_without_state(self):
        payload = json.loads(self.command("--json"))

        self.assertTrue(payload["complete"])
        self.assertEqual(payload["mode"], "preview")
        self.assertEqual([report["source"] for report in payload["channels"]], [
            self.first.source_key, self.second.source_key,
        ])
        self.assertEqual([entry.kwargs["channel"] for entry in self.scrape.call_args_list], [
            self.first, self.second,
        ])
        self.assertTrue(all(entry.kwargs["since"] is None for entry in self.scrape.call_args_list))
        self.read_checkpoint.assert_not_called()
        self.write_checkpoint.assert_not_called()
        self.persist.assert_not_called()

    def test_same_timestamp_imports_independently_and_uses_each_checkpoint(self):
        payload = json.loads(self.command("--import", "--json"))

        self.assertTrue(payload["complete"])
        self.assertEqual(self.persist.call_args_list, [
            call([self.item], source_key=self.first.source_key, source="slack"),
            call([self.second_item], source_key=self.second.source_key, source="slack"),
        ])
        self.assertEqual(self.read_checkpoint.call_args_list, [
            call(channel=self.first), call(channel=self.second),
        ])
        self.assertEqual([entry.kwargs["since"] for entry in self.scrape.call_args_list], [
            "1793900000.000001", None,
        ])
        self.assertEqual(self.write_checkpoint.call_args_list, [
            call(self.result.latest_ts, channel=self.first),
            call(self.second_result.latest_ts, channel=self.second),
        ])

    def test_selection_accepts_id_url_and_key_and_deduplicates_repeated_channel(self):
        payload = json.loads(self.command(
            "--json", "--channel", self.second.channel_id,
            "--channel", self.second.url, "--channel", self.second.source_key,
        ))

        self.assertEqual(payload["source"], self.second.source_key)
        self.assertNotIn("channels", payload)
        self.assertEqual(self.scrape.call_count, 1)
        self.assertEqual(self.scrape.call_args.kwargs["channel"], self.second)

    def test_invalid_later_selection_is_rejected_before_any_browser_or_state_access(self):
        with self.assertRaises(CommandError):
            self.command("--import", "--channel", self.first.channel_id, "--channel", "CUNKNOWN999")

        self.scrape.assert_not_called()
        self.login.assert_not_called()
        self.read_checkpoint.assert_not_called()
        self.persist.assert_not_called()
        self.write_checkpoint.assert_not_called()

    def test_browser_failure_in_first_channel_does_not_block_second_import(self):
        self.scrape.side_effect = [SlackImportError("Channel access is unavailable."), self.second_result]
        stdout = StringIO()
        with self.assertRaisesMessage(CommandError, "Channel access is unavailable."):
            call_command("sync_slack", "--import", "--json", stdout=stdout)

        payload = json.loads(stdout.getvalue())
        self.assertFalse(payload["complete"])
        self.assertIn("error", payload["channels"][0])
        self.assertEqual(payload["channels"][1]["summary"]["created"], 1)
        self.persist.assert_called_once_with([self.second_item], source_key=self.second.source_key, source="slack")
        self.write_checkpoint.assert_called_once_with(self.second_result.latest_ts, channel=self.second)

    def test_incomplete_second_channel_preserves_its_checkpoint_but_keeps_first_success(self):
        self.second_result.complete = False
        stdout = StringIO()
        with self.assertRaises(CommandError):
            call_command("sync_slack", "--import", "--json", stdout=stdout)

        payload = json.loads(stdout.getvalue())
        self.assertFalse(payload["complete"])
        self.assertTrue(payload["channels"][0]["complete"])
        self.assertFalse(payload["channels"][1]["complete"])
        self.persist.assert_called_once_with([self.item], source_key=self.first.source_key, source="slack")
        self.write_checkpoint.assert_called_once_with(self.result.latest_ts, channel=self.first)

    def test_import_errors_in_one_channel_do_not_block_other_channel_checkpoint(self):
        self.persist.side_effect = [
            {**self.summary, "created": 0, "errors": 1, "messages": ["Invalid listing."]},
            self.summary,
        ]
        stdout = StringIO()
        with self.assertRaises(CommandError):
            call_command("sync_slack", "--import", "--json", stdout=stdout)

        payload = json.loads(stdout.getvalue())
        self.assertFalse(payload["complete"])
        self.assertEqual(self.persist.call_count, 2)
        self.write_checkpoint.assert_called_once_with(self.second_result.latest_ts, channel=self.second)

    def test_checkpoint_write_failure_keeps_saved_report_and_allows_next_channel(self):
        self.write_checkpoint.side_effect = [CommandError("Checkpoint storage unavailable."), None]
        stdout = StringIO()
        with self.assertRaisesMessage(CommandError, "Checkpoint storage unavailable."):
            call_command("sync_slack", "--import", "--json", stdout=stdout)

        payload = json.loads(stdout.getvalue())
        self.assertFalse(payload["complete"])
        self.assertEqual(self.persist.call_count, 2)
        self.assertEqual(payload["channels"][0]["summary"]["created"], 1)
        self.assertIn("error", payload["channels"][0])
        self.assertNotIn("error", payload["channels"][1])

    def test_bad_checkpoint_entry_does_not_block_other_channel_full_window(self):
        self.read_checkpoint.side_effect = [CommandError("Use --rescan for this checkpoint."), None]
        with self.assertRaisesMessage(CommandError, "Use --rescan"):
            self.command("--import")

        self.scrape.assert_called_once()
        self.assertEqual(self.scrape.call_args.kwargs["channel"], self.second)
        self.assertIsNone(self.scrape.call_args.kwargs["since"])
        self.write_checkpoint.assert_called_once_with(self.second_result.latest_ts, channel=self.second)

    def test_unexpected_failure_is_reraised_after_other_channel_finishes(self):
        self.scrape.side_effect = [RuntimeError("Collector programming failure."), self.second_result]
        with self.assertRaisesMessage(RuntimeError, "Collector programming failure."):
            self.command("--import")

        self.persist.assert_called_once_with([self.second_item], source_key=self.second.source_key, source="slack")
        self.write_checkpoint.assert_called_once_with(self.second_result.latest_ts, channel=self.second)

    def test_limit_applies_to_each_channel_without_any_checkpoint_advance(self):
        self.command("--import", "--limit", "5")

        self.assertEqual(self.scrape.call_count, 2)
        self.assertTrue(all(entry.kwargs["limit"] == 5 for entry in self.scrape.call_args_list))
        self.assertEqual(self.persist.call_count, 2)
        self.write_checkpoint.assert_not_called()

    def test_login_defaults_to_one_window_for_first_channel(self):
        self.command("--login")

        self.login.assert_called_once_with(browser_channel="chrome", channel=self.first)
        self.scrape.assert_not_called()
        self.read_checkpoint.assert_not_called()

    def test_login_can_target_second_channel_and_rejects_multiple_targets(self):
        self.command("--login", "--channel", self.second.channel_id)
        self.login.assert_called_once_with(browser_channel="chrome", channel=self.second)
        self.login.reset_mock()
        with self.assertRaisesMessage(CommandError, "one --channel"):
            self.command("--login", "--channel", self.first.channel_id, "--channel", self.second.url)
        self.login.assert_not_called()


@override_settings(SLACK_CHANNEL_URLS=(CHANNEL_URL,))
class SlackCheckpointTests(SimpleTestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        settings_override = override_settings(BASE_DIR=Path(self.directory.name))
        settings_override.enable()
        self.addCleanup(settings_override.disable)
        self.path = Path(self.directory.name) / ".slack-sync-state.json"

    def test_first_run_has_no_checkpoint(self):
        from opportunities.management.commands.sync_slack import read_checkpoint

        self.assertIsNone(read_checkpoint())
        self.assertFalse(self.path.exists())

    def test_saved_checkpoint_retains_exact_timestamp(self):
        from opportunities.management.commands.sync_slack import read_checkpoint, write_checkpoint

        write_checkpoint("1794000000.000001")

        self.assertEqual(read_checkpoint(), "1794000000.000001")
        self.assertEqual(
            json.loads(self.path.read_text(encoding="utf-8")),
            {"version": 2, "channels": {SOURCE_KEY: "1794000000.000001"}},
        )

    def test_older_result_cannot_regress_successful_checkpoint(self):
        from opportunities.management.commands.sync_slack import read_checkpoint, write_checkpoint

        write_checkpoint("1794000000.000002")
        write_checkpoint("1794000000.000001")
        self.assertEqual(read_checkpoint(), "1794000000.000002")
        write_checkpoint("1794000000.000003")
        self.assertEqual(read_checkpoint(), "1794000000.000003")

    def test_invalid_or_cross_source_state_requires_explicit_rescan(self):
        from opportunities.management.commands.sync_slack import read_checkpoint

        for content in (
            "invalid-json", "[]", "null", "{}",
            json.dumps({"source": "another-channel", "latest_ts": "1794000000.000001"}),
            json.dumps({"source": SOURCE_KEY, "latest_ts": 1794000000.000001}),
            json.dumps({"source": SOURCE_KEY, "latest_ts": "1794000000.1"}),
        ):
            with self.subTest(content=content):
                self.path.write_text(content, encoding="utf-8")
                with self.assertRaisesMessage(CommandError, "--rescan"):
                    read_checkpoint()

    def test_invalid_checkpoint_cannot_be_written(self):
        from opportunities.management.commands.sync_slack import write_checkpoint

        for timestamp in (None, "1794000000", "1794000000.1", "1794000000.000001\n"):
            with self.subTest(timestamp=timestamp):
                with self.assertRaises(CommandError):
                    write_checkpoint(timestamp)
        self.assertFalse(self.path.exists())

    def test_successful_rescan_can_repair_corrupt_state(self):
        from opportunities.management.commands.sync_slack import read_checkpoint, write_checkpoint

        self.path.write_text("invalid-json", encoding="utf-8")
        write_checkpoint("1794000000.000001")

        self.assertEqual(read_checkpoint(), "1794000000.000001")
        self.assertEqual(list(Path(self.directory.name).iterdir()), [self.path])

    def test_successful_rescan_can_replace_future_checkpoint(self):
        from opportunities.management.commands.sync_slack import read_checkpoint, write_checkpoint

        self.path.write_text(
            json.dumps({"source": SOURCE_KEY, "latest_ts": "9999999999.000000"}),
            encoding="utf-8",
        )
        write_checkpoint("1794000000.000001")

        self.assertEqual(read_checkpoint(), "1794000000.000001")

    @override_settings(SLACK_CHANNEL_URLS=(CHANNEL_URL, SECOND_CHANNEL_URL))
    def test_legacy_checkpoint_is_only_for_original_channel_and_converts_without_loss(self):
        from opportunities.management.commands.sync_slack import read_checkpoint, write_checkpoint

        first, second = get_channels()
        self.path.write_text(json.dumps({"source": SOURCE_KEY, "latest_ts": "1794000000.000001"}), encoding="utf-8")
        self.assertEqual(read_checkpoint(first), "1794000000.000001")
        self.assertIsNone(read_checkpoint(second))

        write_checkpoint("1794000000.000002", second)
        self.assertEqual(read_checkpoint(first), "1794000000.000001")
        self.assertEqual(read_checkpoint(second), "1794000000.000002")
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), {
            "version": 2, "channels": {
                first.source_key: "1794000000.000001", second.source_key: "1794000000.000002",
            },
        })

    @override_settings(SLACK_CHANNEL_URLS=(SECOND_CHANNEL_URL, CHANNEL_URL))
    def test_reordering_configuration_cannot_assign_old_checkpoint_to_new_channel(self):
        from opportunities.management.commands.sync_slack import read_checkpoint

        self.path.write_text(json.dumps({"source": SOURCE_KEY, "latest_ts": "1794000000.000001"}), encoding="utf-8")
        self.assertIsNone(read_checkpoint())
        self.assertEqual(read_checkpoint(CHANNEL_URL), "1794000000.000001")

    @override_settings(SLACK_CHANNEL_URLS=(SECOND_CHANNEL_URL,))
    def test_legacy_original_entry_survives_when_only_new_channel_is_configured(self):
        from opportunities.management.commands.sync_slack import read_checkpoint, write_checkpoint

        self.path.write_text(json.dumps({"source": SOURCE_KEY, "latest_ts": "1794000000.000001"}), encoding="utf-8")
        self.assertIsNone(read_checkpoint())
        write_checkpoint("1794000000.000002")
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["channels"], {
            SOURCE_KEY: "1794000000.000001", get_channel().source_key: "1794000000.000002",
        })

    @override_settings(SLACK_CHANNEL_URLS=(CHANNEL_URL, SECOND_CHANNEL_URL))
    def test_channel_entries_advance_independently_even_for_same_message_timestamp(self):
        from opportunities.management.commands.sync_slack import read_checkpoint, write_checkpoint

        first, second = get_channels()
        write_checkpoint("1794000000.000001", first)
        write_checkpoint("1794000000.000001", second)
        write_checkpoint("1794000000.000002", second)
        write_checkpoint("1793900000.000001", first)

        self.assertEqual(read_checkpoint(first), "1794000000.000001")
        self.assertEqual(read_checkpoint(second), "1794000000.000002")

    @override_settings(SLACK_CHANNEL_URLS=(CHANNEL_URL, SECOND_CHANNEL_URL))
    def test_malformed_channel_entry_is_isolated_and_other_entry_is_preserved(self):
        from opportunities.management.commands.sync_slack import read_checkpoint, write_checkpoint

        first, second = get_channels()
        self.path.write_text(json.dumps({"version": 2, "channels": {
            first.source_key: "invalid", second.source_key: "1794000000.000001",
        }}), encoding="utf-8")
        with self.assertRaisesMessage(CommandError, "--rescan"):
            read_checkpoint(first)
        self.assertEqual(read_checkpoint(second), "1794000000.000001")
        write_checkpoint("1794000000.000002", second)
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["channels"][first.source_key], "invalid")

        write_checkpoint("1794000000.000001", first)
        self.assertEqual(read_checkpoint(first), "1794000000.000001")
        self.assertEqual(read_checkpoint(second), "1794000000.000002")

    @override_settings(SLACK_CHANNEL_URLS=(CHANNEL_URL, SECOND_CHANNEL_URL))
    def test_future_entry_repair_does_not_regress_another_channel(self):
        from opportunities.management.commands.sync_slack import read_checkpoint, write_checkpoint

        first, second = get_channels()
        self.path.write_text(json.dumps({"version": 2, "channels": {
            first.source_key: "9999999999.000000", second.source_key: "1794000000.000003",
        }}), encoding="utf-8")
        write_checkpoint("1794000000.000001", first)
        self.assertEqual(read_checkpoint(first), "1794000000.000001")
        self.assertEqual(read_checkpoint(second), "1794000000.000003")
