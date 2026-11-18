"""Offline channel isolation checks using rendered-message browser fakes."""
from contextlib import contextmanager
from datetime import datetime, timezone
from itertools import count
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from opportunities.services import slack_browser
from opportunities.services.slack_source import get_channel
from opportunities.tests.test_slack_browser import CUTOFF, NEWEST, OLDEST, FakePage, message, view


OLD_ID = "C1111111111"
NEW_ID = "C2222222222"
OLD_URL = f"https://workspace.slack.example.invalid/archives/{OLD_ID}"
NEW_URL = f"https://workspace.slack.example.invalid/archives/{NEW_ID}"


@override_settings(SLACK_CHANNEL_URLS=(OLD_URL, NEW_URL))
class SlackMultichannelBrowserTests(SimpleTestCase):
    NOW = datetime(2026, 11, 12, 12, tzinfo=timezone.utc)

    def collect(self, page, *, channel=NEW_ID):
        with patch.object(slack_browser.time, "monotonic", side_effect=count().__next__):
            return slack_browser.collect_messages(
                page, cutoff=CUTOFF, stop_at=100, channel=channel,
            )

    def test_channel_scope_is_pinned_to_selected_source_even_when_both_are_configured(self):
        for target in (NEW_URL, NEW_ID, get_channel(NEW_ID)):
            with self.subTest(target=target):
                self.assertTrue(slack_browser.channel_is_open(NEW_URL, channel=target))
                self.assertTrue(slack_browser.channel_is_open(
                    f"https://app.slack.com/client/T123456/{NEW_ID}", channel=target,
                ))
                self.assertFalse(slack_browser.channel_is_open(OLD_URL, channel=target))
                self.assertFalse(slack_browser.channel_is_open(
                    f"https://app.slack.com/client/T123456/{OLD_ID}", channel=target,
                ))

    def test_explicit_new_target_navigates_only_to_new_source_and_builds_its_permalink(self):
        page = FakePage(view(
            message(NEWEST, "Vacancy: Junior Analyst\nCompany: Acme"),
            start=True, atTop=True, url=NEW_URL,
        ))

        @contextmanager
        def fake_browser(**browser_options):
            yield page

        with (
            patch.object(slack_browser, "_browser", side_effect=fake_browser) as browser,
            patch.object(slack_browser, "_wait_for_channel") as ready,
            patch.object(slack_browser, "datetime", wraps=datetime) as clock,
            patch.object(slack_browser.time, "monotonic", side_effect=count().__next__),
        ):
            clock.now.return_value = self.NOW
            result = slack_browser.scrape_slack(channel=NEW_URL)

        browser.assert_called_once_with(browser_channel="chrome", headed=False)
        self.assertEqual(page.visits, [NEW_URL])
        self.assertEqual(ready.call_args.kwargs["channel"], get_channel(NEW_ID))
        self.assertTrue(result.complete)
        self.assertEqual(result.discovered, 1)
        self.assertEqual(result.items[0]["external_id"], NEWEST)
        self.assertEqual(result.items[0]["source_url"], f"{NEW_URL}/p{NEWEST.replace('.', '')}")
        self.assertEqual(result.items[0]["organization"], "Acme")

    def test_identical_message_timestamps_in_two_sources_produce_different_source_links(self):
        links = []
        for target in (OLD_URL, NEW_URL):
            page = FakePage(view(
                message(NEWEST, "Vacancy: Junior Analyst\nCompany: Acme"),
                start=True, atTop=True, url=target,
            ))

            @contextmanager
            def fake_browser(**browser_options):
                yield page

            with (
                patch.object(slack_browser, "_browser", fake_browser),
                patch.object(slack_browser, "_wait_for_channel"),
                patch.object(slack_browser, "datetime", wraps=datetime) as clock,
                patch.object(slack_browser.time, "monotonic", side_effect=count().__next__),
            ):
                clock.now.return_value = self.NOW
                result = slack_browser.scrape_slack(channel=target)
            self.assertEqual(result.items[0]["external_id"], NEWEST)
            links.append(result.items[0]["source_url"])
        self.assertEqual(links, [
            f"{OLD_URL}/p{NEWEST.replace('.', '')}",
            f"{NEW_URL}/p{NEWEST.replace('.', '')}",
        ])
        self.assertNotEqual(*links)

    def test_collector_rejects_other_configured_channel_before_reading_any_rows(self):
        page = FakePage(view(message(NEWEST), url=OLD_URL))
        with self.assertRaisesMessage(slack_browser.SlackImportError, "left the configured channel"):
            self.collect(page)
        self.assertEqual(page.directions, [])

    def test_switching_to_other_configured_channel_during_history_scan_aborts(self):
        page = FakePage(
            view(message(NEWEST), url=NEW_URL),
            view(message(OLDEST), url=OLD_URL),
        )
        with self.assertRaisesMessage(slack_browser.SlackImportError, "left the configured channel"):
            self.collect(page)
        self.assertIn("up", page.directions)

    def test_switching_between_configured_client_channels_during_scan_aborts(self):
        page = FakePage(
            view(message(NEWEST), url=f"https://app.slack.com/client/T123456/{NEW_ID}"),
            view(message(OLDEST), url=f"https://app.slack.com/client/T123456/{OLD_ID}"),
        )
        with self.assertRaisesMessage(slack_browser.SlackImportError, "left the configured channel"):
            self.collect(page)

    def test_switch_during_final_dom_read_cannot_return_other_channel_messages(self):
        class SwitchingPage(FakePage):
            reads = 0
            switched = False

            @property
            def url(self):
                return OLD_URL if self.switched else NEW_URL

            def evaluate(self, script, direction=None):
                state = super().evaluate(script, direction)
                if script == slack_browser.MESSAGE_SCRIPT:
                    self.reads += 1
                    if self.reads == 5:
                        self.switched = True
                return state

        page = SwitchingPage(view(message(NEWEST), message(OLDEST)))
        with self.assertRaisesMessage(slack_browser.SlackImportError, "left the configured channel"):
            self.collect(page)

    def test_unconfigured_scrape_target_is_rejected_before_browser_access(self):
        for target in (
            "C0000000000", "https://workspace.slack.example.invalid/archives/C0000000000",
            f"https://other.slack.example.invalid/archives/{NEW_ID}",
        ):
            with self.subTest(target=target), patch.object(slack_browser, "_browser") as browser:
                with self.assertRaises(ValueError):
                    slack_browser.scrape_slack(channel=target)
                browser.assert_not_called()

    def test_unconfigured_login_target_is_rejected_before_browser_access(self):
        with patch.object(slack_browser, "_browser") as browser:
            with self.assertRaises(ValueError):
                slack_browser.login_slack(channel="C0000000000")
            browser.assert_not_called()

    def test_login_uses_one_browser_and_follows_only_selected_channel_browser_link(self):
        class LoginPage:
            url = "https://workspace.slack.example.invalid/"

            def __init__(self):
                self.visits = []

            def goto(self, url, **options):
                self.visits.append(url)
                self.url = (
                    f"https://app.slack.com/client/T123456/{NEW_ID}"
                    if url == f"https://workspace.slack.example.invalid/messages/{NEW_ID}" else url
                )

            def locator(self, selector):
                if selector == slack_browser.PANE_SELECTOR:
                    available = self.url == f"https://app.slack.com/client/T123456/{NEW_ID}"
                else:
                    self.assert_selected_link(selector)
                    available = self.url == NEW_URL
                return SimpleNamespace(count=lambda: int(available))

            @staticmethod
            def assert_selected_link(selector):
                if f"/messages/{NEW_ID}" not in selector or f"/messages/{OLD_ID}" in selector:
                    raise AssertionError("Readiness inspected an unrelated channel link")

            def wait_for_timeout(self, milliseconds):
                raise AssertionError("Login should be ready after the same-channel browser redirect")

        page = LoginPage()

        @contextmanager
        def fake_browser(**browser_options):
            yield page

        with patch.object(slack_browser, "_browser", side_effect=fake_browser) as browser:
            slack_browser.login_slack(channel=NEW_ID)
        browser.assert_called_once_with(browser_channel="chrome", headed=True)
        self.assertEqual(page.visits, [NEW_URL, f"https://workspace.slack.example.invalid/messages/{NEW_ID}"])

    def test_login_readiness_does_not_accept_other_configured_channels_visible_message_pane(self):
        page = FakePage(view(url=OLD_URL))
        page.locator = lambda selector: SimpleNamespace(count=lambda: int(selector == slack_browser.PANE_SELECTOR))
        with patch.object(slack_browser.time, "monotonic", side_effect=count().__next__):
            with self.assertRaisesMessage(slack_browser.SlackImportError, NEW_ID):
                slack_browser._wait_for_channel(page, timeout_seconds=3, channel=NEW_ID)
        self.assertEqual(page.visits, [])
