"""Offline tests of Slack traversal; no session, network, or personal data is used."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from itertools import count
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from opportunities.services import slack_browser
from opportunities.services.slack_source import CHANNEL_ID, CHANNEL_URL, message_permalink


NEWEST = "1794200400.123456"
MIDDLE = "1794200300.123456"
OLDEST = "1794200100.123456"
CUTOFF = Decimal("1794200200.000000")


def message(ts, text="Rendered message", **overrides):
    return {"ts": ts, "text": text, "links": [], "author": "Example", **overrides}


def view(*messages, **overrides):
    return {
        "pane": True, "messages": list(messages), "start": False,
        "historyLimited": False, "loading": False, "scrollable": True,
        "atTop": False, "atBottom": True, **overrides,
    }


class FakePage:
    """A virtualized channel where scrolling up replaces rendered message rows."""

    def __init__(self, *views):
        self.views = views
        self.index = 0
        self.directions = []
        self.visits = []
        self.waits = []

    @property
    def url(self):
        return self.views[self.index].get("url", CHANNEL_URL)

    def goto(self, url, **options):
        self.visits.append(url)

    def evaluate(self, script, direction=None):
        if script == slack_browser.SCROLL_SCRIPT:
            self.directions.append(direction)
            if direction == "up":
                self.index = min(self.index + 1, len(self.views) - 1)
            return True
        if script == slack_browser.MESSAGE_SCRIPT:
            return self.views[self.index]
        if script == getattr(slack_browser, "EXPAND_SCRIPT", None):
            return 0
        raise AssertionError("Unexpected browser operation")

    def wait_for_timeout(self, milliseconds):
        self.waits.append(milliseconds)


@override_settings(SLACK_CHANNEL_URLS=(CHANNEL_URL,))
class SlackBrowserScopeTests(SimpleTestCase):
    def test_placeholder_source_is_blocked_before_browser_import(self):
        with self.assertRaisesMessage(slack_browser.SlackImportError, "placeholder Slack source"):
            with slack_browser._browser(browser_channel="chrome", headed=False):
                self.fail("A placeholder source must never launch a browser.")

    def test_channel_scope_rejects_unrelated_navigation(self):
        self.assertTrue(slack_browser.channel_is_open(CHANNEL_URL))
        for url in (
            CHANNEL_URL.replace(CHANNEL_ID, "C0000000000"),
            CHANNEL_URL.replace("workspace.slack.example.invalid", "anotherworkspace.slack.example.invalid"),
            CHANNEL_URL.replace("https:", "http:"),
            CHANNEL_URL.replace("https://", "https://user:password@"),
            CHANNEL_URL.replace("slack.example.invalid", "slack.example.invalid:443"),
            CHANNEL_URL.replace("slack.example.invalid", "slack.example.invalid.attacker.test"),
            "https://app.slack.com/client/T123456/C0000000000",
            "https://app.slack.com/client/T123456/" + CHANNEL_ID + "/thread/123",
            "https://app.slack.com/client/T123456/search",
            CHANNEL_URL + "?thread_ts=" + NEWEST,
            CHANNEL_URL + "#thread",
            None,
        ):
            with self.subTest(url=url):
                self.assertFalse(slack_browser.channel_is_open(url))

    def test_resource_allowlist_allows_slack_assets_and_normal_sso_only(self):
        for url in (
            CHANNEL_URL, "https://app.slack.com/client/", "https://a.slack-edge.com/script.js",
            "https://files.slack.com/example", "https://accounts.google.com/ServiceLogin",
            "https://login.microsoftonline.com/common/oauth2/authorize",
        ):
            with self.subTest(url=url):
                self.assertTrue(slack_browser.resource_allowed(url))
        for url in (
            "https://example.com/application", "http://app.slack.com/client/",
            "https://slack.com.attacker.test/script.js", "https://evilslack.example.invalid/",
            "https://slack-edge.com.attacker.test/", "https://user:password@app.slack.com/",
            "https://app.slack.com:443/", "file:///C:/private", "data:text/html,example", None,
        ):
            with self.subTest(url=url):
                self.assertFalse(slack_browser.resource_allowed(url))


@override_settings(SLACK_CHANNEL_URLS=(CHANNEL_URL,))
class SlackHistoryTraversalTests(SimpleTestCase):
    def collect(self, page, **options):
        with patch.object(slack_browser.time, "monotonic", side_effect=count().__next__):
            return slack_browser.collect_messages(page, cutoff=CUTOFF, stop_at=100, **options)

    def test_scrolling_retains_virtualized_rows_and_longest_overlapping_text(self):
        page = FakePage(
            view(message(NEWEST, "Newest"), message(MIDDLE, "Partial")),
            view(message(MIDDLE, "Complete overlapping message"), message(OLDEST, "Before window")),
        )
        rows, complete, warnings = self.collect(page)
        self.assertTrue(complete)
        self.assertEqual([row["ts"] for row in rows], [NEWEST, MIDDLE])
        self.assertEqual(rows[1]["text"], "Complete overlapping message")
        self.assertEqual(warnings, [])
        self.assertEqual(page.directions[:4], ["bottom"] * 4)
        self.assertIn("up", page.directions)

    def test_cutoff_is_inclusive(self):
        at_cutoff = "1794200200.000000"
        page = FakePage(view(message(NEWEST), message(at_cutoff)))
        rows, complete, _ = self.collect(page)
        self.assertTrue(complete)
        self.assertEqual([row["ts"] for row in rows], [NEWEST, at_cutoff])

    def test_explicit_batch_selects_newest_messages_regardless_of_dom_order(self):
        page = FakePage(view(message(MIDDLE), message(NEWEST), message(OLDEST)))
        rows, complete, warnings = self.collect(page, limit=1)
        self.assertTrue(complete)
        self.assertEqual([row["ts"] for row in rows], [NEWEST])
        self.assertNotIn("up", page.directions)
        self.assertIn("checkpoint is preserved", " ".join(warnings))

    def test_stable_visible_top_without_proven_start_is_incomplete(self):
        page = FakePage(view(message(NEWEST), atTop=True))
        rows, complete, warnings = self.collect(page)
        self.assertFalse(complete)
        self.assertEqual(len(rows), 1)
        self.assertIn("stopped loading", " ".join(warnings))

    def test_scrolling_through_a_tall_message_does_not_mistake_progress_for_a_stall(self):
        page = FakePage(
            *(view(message(NEWEST), atTop=False) for _ in range(10)),
            view(message(MIDDLE), message(OLDEST), atTop=False),
        )
        rows, complete, _ = self.collect(page)
        self.assertTrue(complete)
        self.assertEqual([row["ts"] for row in rows], [NEWEST, MIDDLE])

    def test_visible_channel_beginning_completes_short_history(self):
        page = FakePage(view(message(NEWEST), start=True, atTop=True))
        rows, complete, warnings = self.collect(page)
        self.assertTrue(complete)
        self.assertEqual([row["ts"] for row in rows], [NEWEST])
        self.assertEqual(warnings, [])

    def test_channel_intro_without_reaching_top_is_not_proof_of_complete_history(self):
        page = FakePage(view(message(NEWEST), start=True, atTop=False))
        _, complete, _ = self.collect(page)
        self.assertFalse(complete)

    def test_retention_boundary_fails_instead_of_treating_it_as_channel_start(self):
        page = FakePage(view(message(NEWEST), historyLimited=True, atTop=True))
        _, complete, warnings = self.collect(page)
        self.assertFalse(complete)
        self.assertIn("limits visible history", " ".join(warnings))

    def test_collapsed_message_aborts_without_partial_candidates(self):
        page = FakePage(view(message(NEWEST, truncated=True), message(OLDEST)))
        rows, complete, warnings = self.collect(page)
        self.assertFalse(complete)
        self.assertEqual(rows, [])
        self.assertIn("collapsed", " ".join(warnings))

    def test_changing_channels_during_traversal_aborts(self):
        page = FakePage(
            view(message(NEWEST)),
            view(message(OLDEST), url=CHANNEL_URL.replace(CHANNEL_ID, "C0000000000")),
        )
        with self.assertRaisesMessage(slack_browser.SlackImportError, "left the configured channel"):
            self.collect(page)

    def test_navigation_during_final_dom_read_cannot_return_wrong_source_rows(self):
        class NavigatingPage(FakePage):
            reads = 0
            navigated = False

            @property
            def url(self):
                return CHANNEL_URL.replace(CHANNEL_ID, "C0000000000") if self.navigated else CHANNEL_URL

            def evaluate(self, script, direction=None):
                state = super().evaluate(script, direction)
                if script == slack_browser.MESSAGE_SCRIPT:
                    self.reads += 1
                    # Four bottom reads establish latest, then the final history read changes location.
                    if self.reads == 5:
                        self.navigated = True
                return state

        page = NavigatingPage(view(message(NEWEST), message(OLDEST)))
        with self.assertRaisesMessage(slack_browser.SlackImportError, "left the configured channel"):
            self.collect(page)

    def test_disappearing_message_pane_aborts(self):
        page = FakePage(view(message(NEWEST)), view(pane=False))
        with self.assertRaisesMessage(slack_browser.SlackImportError, "layout changed"):
            self.collect(page)

    def test_switching_client_workspace_midscan_aborts_even_for_same_channel_id(self):
        page = FakePage(
            view(message(NEWEST), url=f"https://app.slack.com/client/T123456/{CHANNEL_ID}"),
            view(message(OLDEST), url=f"https://app.slack.com/client/T654321/{CHANNEL_ID}"),
        )
        with self.assertRaisesMessage(slack_browser.SlackImportError, "left the configured channel"):
            self.collect(page)

    def test_safety_cap_requires_shorter_window_or_explicit_batch(self):
        page = FakePage(view(message(NEWEST), message(MIDDLE)))
        with patch.object(slack_browser, "MAX_MESSAGES", 2):
            rows, complete, warnings = self.collect(page)
        self.assertFalse(complete)
        self.assertEqual(len(rows), 2)
        self.assertIn("safety limit", " ".join(warnings))

    def test_unestablished_latest_position_times_out_without_partial_results(self):
        page = FakePage(view(message(NEWEST), atBottom=False))
        with patch.object(slack_browser.time, "monotonic", side_effect=count().__next__):
            rows, complete, warnings = slack_browser.collect_messages(page, cutoff=CUTOFF, stop_at=5)
        self.assertFalse(complete)
        self.assertEqual(rows, [])
        self.assertIn("latest channel messages", " ".join(warnings))

    def test_loading_state_cannot_establish_current_latest_position(self):
        page = FakePage(view(message(NEWEST), loading=True))
        with patch.object(slack_browser.time, "monotonic", side_effect=count().__next__):
            _, complete, warnings = slack_browser.collect_messages(page, cutoff=CUTOFF, stop_at=5)
        self.assertFalse(complete)
        self.assertIn("latest channel messages", " ".join(warnings))


@override_settings(SLACK_CHANNEL_URLS=(CHANNEL_URL,))
class SlackScrapeWindowTests(SimpleTestCase):
    NOW = datetime(2026, 11, 9, 12, tzinfo=timezone.utc)

    def scrape_with_fakes(self, *, rows=None, **options):
        page = FakePage(view())

        @contextmanager
        def fake_browser(**browser_options):
            yield page

        parser = SimpleNamespace(parse_message=lambda row: {"external_id": row["ts"], "source_url": row["source_url"]})
        with (
            patch.object(slack_browser, "datetime", wraps=datetime) as clock,
            patch.object(slack_browser, "_browser", fake_browser),
            patch.object(slack_browser, "_wait_for_channel"),
            patch.object(slack_browser, "collect_messages", return_value=(rows or [], True, [])) as collect,
            patch.dict("sys.modules", {"opportunities.services.slack_parsing": parser}),
        ):
            clock.now.return_value = self.NOW
            result = slack_browser.scrape_slack(**options)
        self.assertEqual(page.visits, [CHANNEL_URL])
        return result, collect.call_args.kwargs

    def test_default_window_is_thirty_days(self):
        result, options = self.scrape_with_fakes()
        expected = Decimal(str((self.NOW - timedelta(days=30)).timestamp()))
        self.assertEqual(options["cutoff"], expected)
        self.assertEqual(result.window_start, str(expected))

    def test_checkpoint_uses_one_day_overlap_without_extending_requested_window(self):
        recent = f"{int((self.NOW - timedelta(days=2)).timestamp())}.123456"
        _, options = self.scrape_with_fakes(since=recent)
        self.assertEqual(options["cutoff"], Decimal(recent) - 86400)
        old = f"{int((self.NOW - timedelta(days=60)).timestamp())}.123456"
        _, options = self.scrape_with_fakes(since=old)
        self.assertEqual(options["cutoff"], Decimal(str((self.NOW - timedelta(days=30)).timestamp())))

    def test_future_checkpoint_and_invalid_settings_fail_before_browser_access(self):
        future = f"{int((self.NOW + timedelta(days=1)).timestamp())}.000000"
        for options in (
            {"since": future}, {"since": "not-a-timestamp"},
            {"since": "１７９４２００４００.１２３４５６"}, {"days": True},
            {"days": 0}, {"days": 366}, {"limit": True}, {"limit": 0},
            {"limit": slack_browser.MAX_MESSAGES + 1},
        ):
            with self.subTest(options=options), patch.object(
                slack_browser, "_browser", side_effect=AssertionError("Browser opened for invalid settings"),
            ) as browser:
                with patch.object(slack_browser, "datetime", wraps=datetime) as clock:
                    clock.now.return_value = self.NOW
                    with self.assertRaises(ValueError):
                        slack_browser.scrape_slack(**options)
                browser.assert_not_called()

    def test_result_uses_canonical_message_links_and_reports_unscanned_threads(self):
        result, _ = self.scrape_with_fakes(rows=[message(NEWEST)])
        self.assertTrue(result.complete)
        self.assertEqual(result.latest_ts, NEWEST)
        self.assertEqual(result.discovered, 1)
        self.assertEqual(result.items[0]["source_url"], message_permalink(NEWEST))
        self.assertIn("thread replies and attached files are not scanned", " ".join(result.warnings))
