"""Offline tests of scoped expansion of collapsed Slack message bodies."""
from itertools import count
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from opportunities.services import slack_browser
from opportunities.services.slack_source import CHANNEL_URL
from opportunities.tests.test_slack_browser import CUTOFF, MIDDLE, NEWEST, OLDEST, FakePage, message, view


NEW_CHANNEL_URL = "https://workspace.slack.example.invalid/archives/C2222222222"


class ExpandingPage(FakePage):
    def __init__(self, *views, expanded_text=None, responsive=True, button_available=True):
        super().__init__(*views)
        self.expanded_text = expanded_text or {}
        self.responsive = responsive
        self.button_available = button_available
        self.expanded = set()
        self.expansion_requests = []

    def evaluate(self, script, direction=None):
        if script == slack_browser.EXPAND_SCRIPT:
            self.expansion_requests.append(direction)
            if not self.button_available:
                return 0
            if self.responsive:
                self.expanded.add(direction)
            return 1
        state = super().evaluate(script, direction)
        if script == slack_browser.MESSAGE_SCRIPT:
            rows = []
            for original in state["messages"]:
                row = dict(original)
                if row["ts"] in self.expanded:
                    row["text"] = self.expanded_text.get(row["ts"], row["text"])
                    row["truncated"] = False
                rows.append(row)
            return {**state, "messages": rows}
        return state


@override_settings(SLACK_CHANNEL_URLS=(CHANNEL_URL, NEW_CHANNEL_URL))
class SlackMessageExpansionTests(SimpleTestCase):
    def collect(self, page, *, channel=CHANNEL_URL):
        with patch.object(slack_browser.time, "monotonic", side_effect=count().__next__):
            return slack_browser.collect_messages(
                page, cutoff=CUTOFF, stop_at=100, channel=channel,
            )

    def test_latest_collapsed_body_is_expanded_before_collecting_complete_text(self):
        full_text = "Vacancy: Analyst\nCompany: Acme\nThe full role description includes every requirement."
        page = ExpandingPage(
            view(message(NEWEST, "Vacancy: Analyst", truncated=True), start=True, atTop=True),
            expanded_text={NEWEST: full_text},
        )
        rows, complete, warnings = self.collect(page)
        self.assertTrue(complete)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["text"], full_text)
        self.assertFalse(rows[0]["truncated"])
        self.assertEqual(page.expansion_requests, [NEWEST])
        self.assertIn(500, page.waits)
        self.assertEqual(warnings, [])

    def test_collapsed_older_message_is_expanded_without_losing_newer_virtualized_rows(self):
        full_text = "Full older announcement, including application instructions at its end."
        page = ExpandingPage(
            view(message(NEWEST, "Newest complete message")),
            view(message(MIDDLE, "Older excerpt", truncated=True), message(OLDEST, "Before cutoff")),
            expanded_text={MIDDLE: full_text},
        )
        rows, complete, warnings = self.collect(page)
        self.assertTrue(complete)
        self.assertEqual([row["ts"] for row in rows], [NEWEST, MIDDLE])
        self.assertEqual(rows[0]["text"], "Newest complete message")
        self.assertEqual(rows[1]["text"], full_text)
        self.assertEqual(page.expansion_requests, [MIDDLE])
        self.assertEqual(warnings, [])

    def test_only_collapsed_rows_are_targeted_when_other_messages_share_the_pane(self):
        page = ExpandingPage(
            view(message(NEWEST, "Complete"), message(MIDDLE, "Excerpt", truncated=True), message(OLDEST)),
            expanded_text={MIDDLE: "Expanded announcement"},
        )
        rows, complete, _ = self.collect(page)
        self.assertTrue(complete)
        self.assertEqual(page.expansion_requests, [MIDDLE])
        self.assertEqual([row["ts"] for row in rows], [NEWEST, MIDDLE])

    def test_two_collapsed_messages_are_each_expanded_by_their_exact_timestamp(self):
        page = ExpandingPage(
            view(message(NEWEST, "Excerpt A", truncated=True), message(MIDDLE, "Excerpt B", truncated=True), message(OLDEST)),
            expanded_text={NEWEST: "Full announcement A", MIDDLE: "Full announcement B"},
        )
        rows, complete, _ = self.collect(page)
        self.assertTrue(complete)
        self.assertEqual(set(page.expansion_requests), {NEWEST, MIDDLE})
        self.assertEqual([row["text"] for row in rows], ["Full announcement A", "Full announcement B"])

    def test_missing_expansion_control_fails_without_returning_partial_messages(self):
        page = ExpandingPage(
            view(message(NEWEST, "Complete"), message(MIDDLE, "Excerpt", truncated=True), message(OLDEST)),
            button_available=False,
        )
        rows, complete, warnings = self.collect(page)
        self.assertFalse(complete)
        self.assertEqual(rows, [])
        self.assertIn("collapsed", " ".join(warnings))
        self.assertTrue(page.expansion_requests)

    def test_unresponsive_expansion_control_fails_without_accepting_its_short_excerpt(self):
        page = ExpandingPage(
            view(message(NEWEST, "Excerpt", truncated=True), message(OLDEST)),
            responsive=False,
        )
        rows, complete, warnings = self.collect(page)
        self.assertFalse(complete)
        self.assertEqual(rows, [])
        self.assertIn("collapsed", " ".join(warnings))
        self.assertGreater(len(page.expansion_requests), 0)
        self.assertLessEqual(len(page.expansion_requests), 15)

    def test_source_switch_during_expansion_aborts_even_if_new_body_is_available(self):
        class SwitchingPage(ExpandingPage):
            switched = False

            @property
            def url(self):
                return CHANNEL_URL if self.switched else NEW_CHANNEL_URL

            def evaluate(self, script, direction=None):
                state = super().evaluate(script, direction)
                if script == slack_browser.EXPAND_SCRIPT:
                    self.switched = True
                return state

        page = SwitchingPage(
            view(message(NEWEST, "Excerpt", truncated=True), start=True, atTop=True),
            expanded_text={NEWEST: "Complete text"},
        )
        with self.assertRaisesMessage(slack_browser.SlackImportError, "left the configured channel"):
            self.collect(page, channel=NEW_CHANNEL_URL)
        self.assertEqual(page.expansion_requests, [NEWEST])

    def test_source_switch_after_detecting_collapse_prevents_any_expansion_click(self):
        class SwitchingPage(ExpandingPage):
            switched = False

            @property
            def url(self):
                return CHANNEL_URL if self.switched else NEW_CHANNEL_URL

            def evaluate(self, script, direction=None):
                state = super().evaluate(script, direction)
                if script == slack_browser.MESSAGE_SCRIPT:
                    self.switched = True
                return state

        page = SwitchingPage(view(message(NEWEST, "Excerpt", truncated=True), start=True, atTop=True))
        with self.assertRaisesMessage(slack_browser.SlackImportError, "left the configured channel"):
            self.collect(page, channel=NEW_CHANNEL_URL)
        self.assertEqual(page.expansion_requests, [])
