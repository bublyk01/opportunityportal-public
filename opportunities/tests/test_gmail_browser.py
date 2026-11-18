from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from opportunities.services import gmail_browser as browser
from opportunities.services.gmail_source import message_permalink


MESSAGE_ID = "19a8000000000001"
SECOND_ID = "19a8000000000002"
THIRD_ID = "19a8000000000003"
QUERY = "in:inbox after:100 before:200"


class MessagePage:
    def __init__(self, *, emails=None, messages=None, content=None, redirect=None, inbox=True, headers=None):
        self.url = browser.search_url(QUERY)
        self.emails = emails if emails is not None else ["owner@example.edu"]
        self.messages = messages if messages is not None else [{
            "external_id": MESSAGE_ID, "received_ts": 150, "received_at": "A displayed date", "body": True,
        }]
        self.content = content if content is not None else [{
            "text": "University internship announcement", "subject": "Research internship",
            "sender": "careers@example.com", "links": [], "attachments": [],
            "clipped": False, "trimmed": False,
        }]
        self.redirect = redirect
        self.inbox = inbox
        self.headers = list(headers or [])
        self.calls = []

    def goto(self, url, **kwargs):
        self.url = self.redirect(url) if self.redirect else url

    def evaluate(self, script, argument=None):
        self.calls.append((script, argument))
        if script == browser.ACCOUNT_SCRIPT:
            return list(self.emails)
        if script == browser.HEADER_SCRIPT:
            if self.headers:
                return deepcopy(self.headers.pop(0))
            return {"ready": True, "inbox": self.inbox, "messages": deepcopy(self.messages)}
        if script == browser.TEXT_SCRIPT:
            value = self.content[0]
            if len(self.content) > 1:
                self.content.pop(0)
            return deepcopy(value)
        if script in (browser.EXPAND_SCRIPT, browser.NEXT_SCRIPT):
            return True
        raise AssertionError("Unexpected browser evaluation")

    def wait_for_timeout(self, milliseconds):
        pass

    def locator(self, selector):
        return SimpleNamespace(click=lambda: self.calls.append(("click", selector)))


@override_settings(GMAIL_ACCOUNT_EMAIL="owner@example.edu")
class GmailBrowserScopeTests(SimpleTestCase):
    def thread(self, **overrides):
        return {"thread_id": MESSAGE_ID, "last_message_id": MESSAGE_ID, "count": 1, **overrides}

    def read(self, page, **thread):
        return browser.read_message(page, QUERY, self.thread(**thread), window_start=100, window_end=200)

    def assert_no_body_read(self, page):
        self.assertFalse(any(script == browser.TEXT_SCRIPT for script, _ in page.calls))

    def test_mail_url_checks_restrict_account_slot_and_host(self):
        self.assertTrue(browser.mail_is_open(browser.MAIL_URL + "#inbox"))
        for url in (
            "http://mail.google.com/mail/u/0/", "https://mail.google.com/mail/u/1/#inbox",
            "https://mail.google.com.evil.example/mail/u/0/", "https://user@mail.google.com/mail/u/0/",
            "https://mail.google.com:443/mail/u/0/", "https://mail.google.com/mail/u/0/?account=other",
        ):
            with self.subTest(url=url):
                self.assertFalse(browser.mail_is_open(url))

    def test_resource_allowlist_excludes_arbitrary_mail_tracking_hosts_and_credentials(self):
        self.assertTrue(browser.resource_allowed("https://mail.google.com/mail/u/0/"))
        self.assertTrue(browser.resource_allowed("https://accounts.google.com/login"))
        for url in (
            "https://tracking.example.com/pixel", "https://mail.google.com.evil.example/",
            "https://user:password@mail.google.com/", "http://mail.google.com/", "file:///C:/secret.txt",
        ):
            with self.subTest(url=url):
                self.assertFalse(browser.resource_allowed(url))

    def test_wrong_or_ambiguous_account_is_rejected_before_message_header_or_body_reads(self):
        for emails in ([], ["different@example.com"], ["owner@example.edu", "different@example.com"]):
            with self.subTest(emails=emails):
                page = MessagePage(emails=emails)
                with self.assertRaises(browser.GmailImportError):
                    self.read(page)
                self.assert_no_body_read(page)
                self.assertFalse(any(script == browser.HEADER_SCRIPT for script, _ in page.calls))

    def test_grouped_result_is_rejected_before_reading_any_message_body(self):
        page = MessagePage()
        with self.assertRaisesMessage(browser.GmailImportError, "grouped conversation"):
            self.read(page, count=2)
        self.assert_no_body_read(page)

    def test_rendered_inbox_membership_is_required_before_body_read(self):
        page = MessagePage(inbox=False)
        with self.assertRaisesMessage(browser.GmailImportError, "Inbox label"):
            self.read(page)
        self.assert_no_body_read(page)

    def test_multiple_rendered_messages_are_rejected_even_if_list_count_was_one(self):
        page = MessagePage(messages=[
            {"external_id": MESSAGE_ID, "received_ts": 150, "body": True},
            {"external_id": SECOND_ID, "received_ts": 150, "body": True},
        ])
        with self.assertRaises(browser.GmailImportError):
            self.read(page)
        self.assert_no_body_read(page)

    def test_result_identity_and_visible_complete_body_are_required_before_read(self):
        for overrides in ({"external_id": SECOND_ID}, {"body": False}):
            with self.subTest(overrides=overrides):
                page = MessagePage(messages=[{"external_id": MESSAGE_ID, "received_ts": 150, "body": True, **overrides}])
                with self.assertRaises(browser.GmailImportError):
                    self.read(page)
                self.assert_no_body_read(page)

    def test_outside_or_unreadable_date_is_rejected_before_reading_body(self):
        for timestamp in (None, 99, 200, 201):
            with self.subTest(timestamp=timestamp):
                page = MessagePage(messages=[{"external_id": MESSAGE_ID, "received_ts": timestamp, "body": True}])
                with self.assertRaises(browser.GmailImportError):
                    self.read(page)
                self.assert_no_body_read(page)

    def test_route_that_leaves_authorized_search_is_rejected_before_body_read(self):
        for route in ("#inbox/" + MESSAGE_ID, "#search/other/" + MESSAGE_ID, "#search/" + QUERY + "/extra/" + MESSAGE_ID):
            with self.subTest(route=route):
                page = MessagePage(redirect=lambda url: browser.MAIL_URL + route)
                with self.assertRaises(browser.GmailImportError):
                    self.read(page)
                self.assert_no_body_read(page)

    def test_valid_single_message_uses_exact_message_permalink_and_preserves_text(self):
        page = MessagePage()
        result = self.read(page)
        self.assertTrue(result["complete"])
        self.assertEqual(result["external_id"], MESSAGE_ID)
        self.assertEqual(result["source_url"], message_permalink(MESSAGE_ID))
        self.assertEqual(result["text"], "University internship announcement")
        self.assertEqual([argument for script, argument in page.calls if script == browser.TEXT_SCRIPT], [MESSAGE_ID])

    def test_stale_rendered_identity_is_waited_out_before_body_read(self):
        page = MessagePage(headers=[{
            "ready": True, "inbox": True,
            "messages": [{"external_id": SECOND_ID, "received_ts": 150, "body": True}],
        }])
        result = self.read(page)
        self.assertEqual(result["external_id"], MESSAGE_ID)
        self.assertEqual(sum(script == browser.HEADER_SCRIPT for script, _ in page.calls), 2)
        self.assertEqual([argument for script, argument in page.calls if script == browser.TEXT_SCRIPT], [MESSAGE_ID])

    def test_matching_header_waits_for_visible_body_before_reading_text(self):
        page = MessagePage(headers=[{
            "ready": True, "inbox": True,
            "messages": [{"external_id": MESSAGE_ID, "received_ts": 150, "body": False}],
        }])
        with patch.object(page, "wait_for_timeout") as wait:
            result = self.read(page)

        self.assertEqual(result["external_id"], MESSAGE_ID)
        self.assertEqual(result["text"], "University internship announcement")
        reads = [script for script, _ in page.calls if script in (browser.HEADER_SCRIPT, browser.TEXT_SCRIPT)]
        self.assertEqual(reads, [browser.HEADER_SCRIPT, browser.HEADER_SCRIPT, browser.TEXT_SCRIPT])
        wait.assert_called_once_with(300)

    def test_navigation_race_after_body_read_is_rejected_instead_of_returning_snapshot(self):
        page = MessagePage()
        original_evaluate = page.evaluate

        def navigate_during_read(script, argument=None):
            value = original_evaluate(script, argument)
            if script == browser.TEXT_SCRIPT:
                page.url = browser.MAIL_URL + "#inbox/" + SECOND_ID
            return value

        with patch.object(page, "evaluate", side_effect=navigate_during_read):
            with self.assertRaisesMessage(browser.GmailImportError, "during collection"):
                self.read(page)

    def test_navigation_race_during_prebody_account_check_prevents_body_read(self):
        page = MessagePage()
        original_evaluate = page.evaluate
        account_reads = 0

        def navigate_before_read(script, argument=None):
            nonlocal account_reads
            value = original_evaluate(script, argument)
            if script == browser.ACCOUNT_SCRIPT:
                account_reads += 1
                if account_reads == 2:
                    page.url = browser.MAIL_URL + "#inbox/" + SECOND_ID
            return value

        with patch.object(page, "evaluate", side_effect=navigate_before_read):
            with self.assertRaisesMessage(browser.GmailImportError, "during collection"):
                self.read(page)
        self.assert_no_body_read(page)

    def test_clipped_or_persistently_trimmed_messages_fail_instead_of_silent_partial_import(self):
        for field in ("clipped", "trimmed"):
            with self.subTest(field=field):
                page = MessagePage()
                page.content[0][field] = True
                with self.assertRaisesMessage(browser.GmailImportError, "clipped or still collapsed"):
                    self.read(page)

    def test_trimmed_content_is_expanded_and_complete_text_is_returned(self):
        page = MessagePage()
        original = page.content[0]
        page.content = [{**original, "trimmed": True, "text": "Partial"}, {**original, "text": "Complete announcement"}]
        result = self.read(page)
        self.assertEqual(result["text"], "Complete announcement")
        self.assertTrue(any(script == browser.EXPAND_SCRIPT for script, _ in page.calls))


@override_settings(GMAIL_ACCOUNT_EMAIL="owner@example.edu")
class GmailBrowserPaginationTests(SimpleTestCase):
    def row(self, message_id):
        return {"thread_id": message_id, "last_message_id": message_id, "count": 1}

    def state(self, *ids, **extra):
        return {"ready": True, "rows": [self.row(value) for value in ids], "empty": False,
                "related": False, "next_present": True, "next": False, **extra}

    def collect(self, states, **options):
        page = MessagePage()
        with (
            patch.object(browser, "_wait_for_results", side_effect=states),
            patch.object(browser, "verify_search"),
        ):
            return browser.collect_threads(page, QUERY, **options)

    def test_pagination_accumulates_unique_ids_and_finishes_only_at_verified_end(self):
        rows = self.collect([
            self.state(MESSAGE_ID, next=True),
            self.state(MESSAGE_ID, SECOND_ID),
        ])
        self.assertEqual([row["thread_id"] for row in rows], [MESSAGE_ID, SECOND_ID])

    def test_related_results_missing_pagination_and_unsupported_ids_fail_closed(self):
        for state in (
            self.state(MESSAGE_ID, related=True), self.state(MESSAGE_ID, next_present=False),
            self.state("unsupported-id"),
        ):
            with self.subTest(state=state):
                with self.assertRaises(browser.GmailImportError):
                    self.collect([state])

    def test_empty_exact_search_is_a_verified_empty_collection(self):
        self.assertEqual(self.collect([self.state(empty=True)]), [])

    def test_explicit_batch_stops_at_requested_count_without_claiming_remaining_pages(self):
        rows = self.collect([self.state(MESSAGE_ID, SECOND_ID, next=True)], limit=1)
        self.assertEqual([row["thread_id"] for row in rows], [MESSAGE_ID])

    def test_final_page_must_not_cross_safety_cap(self):
        with patch.object(browser, "MAX_MESSAGES", 2):
            with self.assertRaises(browser.GmailImportError):
                self.collect([self.state(MESSAGE_ID, next=True), self.state(SECOND_ID, THIRD_ID)])

    def test_final_page_may_end_at_exact_safety_cap(self):
        with patch.object(browser, "MAX_MESSAGES", 2):
            rows = self.collect([self.state(MESSAGE_ID, next=True), self.state(SECOND_ID)])
        self.assertEqual(len(rows), 2)


@override_settings(GMAIL_ACCOUNT_EMAIL="owner@example.edu")
class GmailBrowserScanTests(SimpleTestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 13, 12, tzinfo=timezone.utc)
        self.page = MessagePage()
        self.rows = [{"thread_id": value, "last_message_id": value, "count": 1} for value in (MESSAGE_ID, SECOND_ID)]
        self.messages = [{"external_id": value, "text": "Rendered announcement", "complete": True} for value in (MESSAGE_ID, SECOND_ID)]
        for attribute, target in (
            ("open_browser", "_browser"), ("collect", "collect_threads"),
            ("read", "read_message"), ("clock", "datetime"),
        ):
            patcher = patch.object(browser, target)
            setattr(self, attribute, patcher.start())
            self.addCleanup(patcher.stop)
        parser_patch = patch("opportunities.services.gmail_parsing.parse_email")
        self.parse = parser_patch.start()
        self.addCleanup(parser_patch.stop)
        time_patch = patch.object(browser.time, "monotonic", return_value=0)
        time_patch.start()
        self.addCleanup(time_patch.stop)
        self.clock.now.return_value = self.now
        self.open_browser.return_value.__enter__.return_value = self.page
        self.collect.return_value = self.rows
        self.read.side_effect = self.messages
        self.candidate = {"external_id": SECOND_ID, "title": "Research internship"}
        self.parse.side_effect = [None, self.candidate]

    def test_scan_uses_only_fixed_inbox_date_query_and_accounts_for_classified_messages(self):
        result = browser.scrape_gmail(days=7, limit=2, browser_channel="msedge", headed=True)

        self.open_browser.assert_called_once_with(browser_channel="msedge", headed=True)
        start = datetime(2026, 11, 6, 12, tzinfo=timezone.utc)
        query = f"in:inbox after:{int(start.timestamp())} before:{int(self.now.timestamp())}"
        self.assertEqual(self.page.url, browser.search_url(query))
        self.assertTrue(result.complete)
        self.assertEqual(result.discovered, 2)
        self.assertEqual(result.skipped, 1)
        self.assertEqual(result.items, [self.candidate])
        self.assertEqual(result.window_start, start.isoformat())
        self.assertEqual(result.window_end, self.now.isoformat())
        self.assertTrue(any("not a full Inbox window" in warning for warning in result.warnings))
        self.assertTrue(all(entry.kwargs["window_start"] == start.timestamp() for entry in self.read.call_args_list))

    def test_empty_search_returns_complete_empty_result_without_opening_message_bodies(self):
        self.collect.return_value = []
        result = browser.scrape_gmail()

        self.assertTrue(result.complete)
        self.assertEqual(result.discovered, 0)
        self.assertEqual(result.items, [])
        self.read.assert_not_called()
        self.parse.assert_not_called()

    def test_duplicate_message_identity_and_unreadable_candidate_abort_collection(self):
        self.read.side_effect = [self.messages[0], self.messages[0]]
        with self.assertRaisesMessage(browser.GmailImportError, "duplicate message identities"):
            browser.scrape_gmail()
        self.assertEqual(self.parse.call_count, 1)

        self.read.side_effect = self.messages
        self.parse.side_effect = ValueError("Private truncated email text")
        with self.assertRaisesMessage(browser.GmailImportError, "could not be read completely") as error:
            browser.scrape_gmail()
        self.assertNotIn("Private truncated email text", str(error.exception))

    def test_collection_error_closes_browser_and_never_reads_or_parses_bodies(self):
        self.collect.side_effect = browser.GmailImportError("Search did not complete")
        with self.assertRaisesMessage(browser.GmailImportError, "Search did not complete"):
            browser.scrape_gmail()
        self.read.assert_not_called()
        self.parse.assert_not_called()
        self.open_browser.return_value.__exit__.assert_called_once()
