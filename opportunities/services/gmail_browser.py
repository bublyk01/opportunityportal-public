"""Read the configured Gmail Inbox through a separate interactive browser profile.

Only rendered mail content is inspected. Authentication remains in the local
profile; no cookies, tokens, browser storage, or network responses are extracted.
"""
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
import time
from urllib.parse import quote, urlsplit, unquote_plus

from .gmail_source import get_account, message_permalink


PROFILE_DIR = Path(__file__).resolve().parents[2] / ".gmail-browser"
MAIL_URL = "https://mail.google.com/mail/u/0/"
LOGIN_SECONDS = 600
MAX_RUN_SECONDS = 600
MAX_MESSAGES = 1000


class GmailImportError(RuntimeError):
    def __init__(self, message, *, code="collection"):
        super().__init__(message)
        self.code = code


@dataclass
class GmailScanResult:
    items: list = field(default_factory=list)
    complete: bool = False
    discovered: int = 0
    skipped: int = 0
    warnings: list = field(default_factory=list)
    window_start: str = ""
    window_end: str = ""


ACCOUNT_SCRIPT = r"""() => {
    const nodes = [...document.querySelectorAll(
        'header a[href*="SignOutOptions"], a[aria-label*="Google Account"], '
        + 'a[href^="https://accounts.google.com/SignOutOptions"]'
    )].filter(el => el.getClientRects().length);
    const emails = nodes.flatMap(el =>
        ((el.getAttribute('aria-label') || '') + '\n' + (el.getAttribute('title') || ''))
        .match(/[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Z0-9.-]+\.[A-Z]{2,}/ig) || []);
    return [...new Set(emails.map(value => value.toLowerCase()))];
}"""


def mail_is_open(url):
    try:
        parsed = urlsplit(url)
        return (
            parsed.scheme == "https" and parsed.netloc == "mail.google.com"
            and parsed.username is None and parsed.password is None
            and parsed.port is None and parsed.path == "/mail/u/0/"
            and not parsed.query
        )
    except (ValueError, TypeError):
        return False


def resource_allowed(url):
    try:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port:
            return False
        return parsed.hostname in {
            "mail.google.com", "accounts.google.com", "accounts.youtube.com",
            "ssl.gstatic.com", "www.gstatic.com", "fonts.gstatic.com", "fonts.googleapis.com",
            "apis.google.com", "ogs.google.com", "clients1.google.com", "clients6.google.com",
            "content.googleapis.com", "people-pa.googleapis.com", "play.google.com",
            "login.microsoftonline.com", "aadcdn.msftauth.net", "aadcdn.msauth.net",
            "logincdn.msauth.net",
        }
    except (ValueError, TypeError):
        return False


@contextmanager
def _browser(*, browser_channel, headed):
    if browser_channel not in {"chrome", "msedge", "chromium"}:
        raise ValueError("Unsupported browser. Use chrome, msedge, or chromium.")
    try:
        from playwright.sync_api import Error as PlaywrightError, sync_playwright
    except ImportError as exc:
        raise GmailImportError("Install requirements-importers.txt for browser support.") from exc
    try:
        with sync_playwright() as playwright:
            launch = {"headless": not headed, "viewport": {"width": 1440, "height": 1200}}
            if browser_channel != "chromium":
                launch["channel"] = browser_channel
            context = playwright.chromium.launch_persistent_context(
                str(PROFILE_DIR), locale="en-US", timezone_id="UTC", accept_downloads=False,
                service_workers="block", **launch,
            )
            try:
                def route_request(route):
                    if route.request.resource_type in {"image", "media"} or not resource_allowed(route.request.url):
                        route.abort()
                    else:
                        route.continue_()

                context.route("**/*", route_request)
                page = context.new_page()
                page.set_default_timeout(15000)
                yield page
            finally:
                context.close()
    except PlaywrightError as exc:
        raise GmailImportError(
            "Gmail browser could not complete the scan. Close the importer browser if it is open, "
            "then use sync_gmail --login to check sign-in.", code="browser",
        ) from None


def verify_account(page):
    """Verify the selected account using only the rendered Google account control."""
    expected = get_account().email
    if not mail_is_open(page.url):
        raise GmailImportError("The configured Gmail mailbox is not open; sign in with sync_gmail --login.")
    emails = page.evaluate(ACCOUNT_SCRIPT)
    if emails != [expected]:
        raise GmailImportError("The selected Gmail account could not be verified against GMAIL_ACCOUNT_EMAIL.", code="account")
    return expected


def login_gmail(*, browser_channel="chrome"):
    account = get_account()
    with _browser(browser_channel=browser_channel, headed=True) as page:
        page.goto(MAIL_URL, wait_until="domcontentloaded")
        deadline = time.monotonic() + LOGIN_SECONDS
        while time.monotonic() < deadline:
            if mail_is_open(page.url):
                emails = page.evaluate(ACCOUNT_SCRIPT)
                if emails == [account.email]:
                    return
                if emails:
                    raise GmailImportError("The browser is signed in to a different Gmail account.")
            page.wait_for_timeout(1000)
        raise GmailImportError("Gmail sign-in did not finish. Run sync_gmail --login and complete sign-in in its browser.")


LIST_SCRIPT = r"""() => {
    const visible = el => !!el.getClientRects().length;
    const main = [...document.querySelectorAll('[role="main"]')].find(visible);
    if (!main) return {ready: false};
    const rows = [...main.querySelectorAll('tr.zA')].filter(visible).map(row => {
        const identity = row.querySelector('[data-legacy-thread-id]');
        const countText = row.querySelector('.yW')?.innerText || '';
        const count = countText.match(/\(([0-9]+)\)/);
        // In Conversation view off, replies can share a thread ID. Navigate and
        // deduplicate by the row's actual non-draft message ID instead.
        return {thread_id: identity?.getAttribute('data-legacy-last-non-draft-message-id') || '',
            last_message_id: identity?.getAttribute('data-legacy-last-non-draft-message-id') || '',
            subject: row.querySelector('.bog')?.innerText || '',
            snippet: row.querySelector('.y2')?.innerText || '',
            count: count ? Number(count[1]) : 1};
    });
    const next = [...main.querySelectorAll('[role="button"]')].find(el =>
        visible(el) && /^(Next results|Older)$/i.test(el.getAttribute('aria-label') || ''));
    const text = main.innerText;
    const empty = rows.length === 0 && /No (conversations|messages) found|No results found|Your search did not match any emails/i.test(text);
    const related = /Related results|Showing results for|No (conversations|messages).*match/i.test(text);
    return {ready: rows.length > 0 || empty, rows, empty, related,
        next: !!next && next.getAttribute('aria-disabled') !== 'true',
        next_present: !!next};
}"""

NEXT_SCRIPT = r"""() => {
    const main = [...document.querySelectorAll('[role="main"]')].find(el => el.getClientRects().length);
    const next = [...(main?.querySelectorAll('[role="button"]') || [])].find(el =>
        el.getClientRects().length && /^(Next results|Older)$/i.test(el.getAttribute('aria-label') || '')
        && el.getAttribute('aria-disabled') !== 'true');
    if (!next) return false;
    next.click(); return true;
}"""


def search_url(query):
    return MAIL_URL + "#search/" + quote(query, safe="")


def verify_search(page, query, *, thread_id=None, page_number=None):
    verify_account(page)
    fragment = unquote_plus(urlsplit(page.url).fragment)
    expected = "search/" + query
    if thread_id is not None:
        expected += "/" + thread_id
    elif page_number is not None and page_number > 1:
        expected += "/p" + str(page_number)
    if fragment != expected:
        raise GmailImportError("Gmail left the authorized Inbox search. Nothing was imported.")


def _wait_for_results(page, query, *, page_number, previous=None):
    for _ in range(30):
        # Gmail's router may update shortly after the next-page button click.
        if mail_is_open(page.url):
            fragment = unquote_plus(urlsplit(page.url).fragment)
            expected = "search/" + query + (f"/p{page_number}" if page_number > 1 else "")
            if fragment == expected:
                # The account control loads after Gmail's application shell.
                # Wait for it before inspecting even the result list.
                emails = page.evaluate(ACCOUNT_SCRIPT)
                if emails:
                    verify_search(page, query, page_number=page_number)
                    state = page.evaluate(LIST_SCRIPT)
                    verify_search(page, query, page_number=page_number)
                    ids = tuple(row["thread_id"] for row in state.get("rows", []))
                    if state.get("ready") and (previous is None or ids != previous):
                        return state
        page.wait_for_timeout(500)
    raise GmailImportError("Gmail search results did not finish loading. Nothing was imported.", code="search-loading")


def collect_threads(page, query, *, limit=None, stop_at=None):
    """Collect only identities and list previews from the exact Inbox search."""
    stop_at = stop_at if stop_at is not None else time.monotonic() + MAX_RUN_SECONDS
    threads = {}
    page_number, previous = 1, None
    while time.monotonic() < stop_at:
        state = _wait_for_results(page, query, page_number=page_number, previous=previous)
        if state.get("related"):
            raise GmailImportError("Gmail showed related or broadened search results. Nothing was imported.")
        if state.get("empty"):
            return list(threads.values())
        for row in state["rows"]:
            if not re.fullmatch(r"[0-9a-f]{16}", row.get("thread_id", "")):
                raise GmailImportError("A Gmail result had no supported message identity. Nothing was imported.")
            threads.setdefault(row["thread_id"], row)
            if len(threads) > MAX_MESSAGES:
                raise GmailImportError("The Inbox search exceeded the 1,000-message cap. Use a smaller --days window.")
            if limit is not None and len(threads) >= limit:
                return list(threads.values())
        if not state.get("next_present"):
            raise GmailImportError("Gmail result pagination could not be verified. Nothing was imported.")
        if not state.get("next"):
            return list(threads.values())
        if len(threads) >= MAX_MESSAGES:
            raise GmailImportError("The Inbox search exceeded the 1,000-message cap. Use a smaller --days window.")
        previous = tuple(row["thread_id"] for row in state["rows"])
        verify_search(page, query, page_number=page_number)
        page.locator('[role="main"]:visible [role="button"][aria-label="Next results"]:visible, '
                     '[role="main"]:visible [role="button"][aria-label="Older"]:visible').click()
        page_number += 1
    raise GmailImportError("The Gmail collection time budget expired. Nothing was imported.")


HEADER_SCRIPT = r"""() => {
    const main = [...document.querySelectorAll('[role="main"]')].find(el => el.getClientRects().length);
    if (!main) return {ready: false};
    const messages = [...main.querySelectorAll('.adn[data-message-id]')];
    const inbox = [...main.querySelectorAll('[aria-label="Search for all messages with label Inbox"]')]
        .some(el => el.getClientRects().length && !el.closest('.adn'));
    return {ready: messages.length > 0, inbox, messages: messages.map(message => {
        const date = message.querySelector('.g3[title]')?.getAttribute('title') || '';
        const stamp = Date.parse(date.replace(/\u202f/g, ' '));
        return {external_id: message.getAttribute('data-legacy-message-id') || '',
            received_ts: Number.isFinite(stamp) ? stamp / 1000 : null,
            received_at: date,
            body: !!message.querySelector('.a3s')?.getClientRects().length};
    })};
}"""

TEXT_SCRIPT = r"""id => {
    const main = [...document.querySelectorAll('[role="main"]')].find(el => el.getClientRects().length);
    const message = [...(main?.querySelectorAll('.adn[data-message-id]') || [])].find(el =>
        el.getAttribute('data-legacy-message-id') === id);
    const body = message?.querySelector('.a3s');
    if (!body || !body.getClientRects().length) return null;
    const visible = el => !!el.getClientRects().length;
    const clipped = [...message.querySelectorAll('.adm, .adp')].some(el =>
        visible(el) && /message clipped|view entire message/i.test(el.innerText));
    const trimmed = [...message.querySelectorAll('.ajR[role="button"], .yj6qo [role="button"]')]
        .filter(el => visible(el) && /show trimmed content/i.test(el.getAttribute('aria-label') || el.getAttribute('data-tooltip') || ''));
    return {text: body.innerText || '',
        links: [...body.querySelectorAll('a[href]')].map(a => a.href).filter(url => /^https?:\/\//i.test(url)),
        sender: message.querySelector('.gD[email]')?.getAttribute('email') || '',
        subject: main.querySelector('h2.hP')?.innerText || '',
        attachments: [...message.querySelectorAll('.aV3')].map(el => el.innerText).filter(Boolean),
        clipped, trimmed: trimmed.length > 0};
}"""

EXPAND_SCRIPT = r"""id => {
    const main = [...document.querySelectorAll('[role="main"]')].find(el => el.getClientRects().length);
    const message = [...(main?.querySelectorAll('.adn[data-message-id]') || [])].find(el =>
        el.getAttribute('data-legacy-message-id') === id);
    if (!message) return false;
    const controls = [...message.querySelectorAll('.ajR[role="button"], .yj6qo [role="button"]')]
        .filter(el => el.getClientRects().length
            && /show trimmed content/i.test(el.getAttribute('aria-label') || el.getAttribute('data-tooltip') || ''));
    for (const control of controls) control.click();
    return controls.length > 0;
}"""


def read_message(page, query, thread, *, window_start, window_end):
    """Refuse grouped conversations: thread-level Inbox labels are ambiguous.

    With one message, the exact search result supplies Inbox membership. A date
    header is checked separately and each record uses the actual message ID.
    """
    thread_id = thread["thread_id"]
    page.goto(search_url(query) + "/" + thread_id, wait_until="domcontentloaded")
    header = None
    for _ in range(30):
        # Gmail can replace the hexadecimal route with its modern ID. Keep the
        # chosen result pinned by actual message identity after routing settles.
        verify_account(page)
        fragment = unquote_plus(urlsplit(page.url).fragment)
        prefix = "search/" + query + "/"
        if not fragment.startswith(prefix) or "/" in fragment[len(prefix):]:
            raise GmailImportError("Gmail left the selected Inbox search result.")
        header = page.evaluate(HEADER_SCRIPT)
        if header.get("ready") and header.get("inbox") and any(
            message.get("external_id") == thread.get("last_message_id") and message.get("body")
            for message in header.get("messages", [])
        ):
            break
        page.wait_for_timeout(300)
    if not header or not header.get("ready"):
        raise GmailImportError("A Gmail message did not finish loading. Nothing was imported.", code="body-loading")
    if not header.get("inbox"):
        raise GmailImportError("The selected message has no verified Inbox label. Nothing was imported.", code="inbox-label")
    if len(header["messages"]) != 1 or thread.get("count", 1) != 1:
        raise GmailImportError(
            "Gmail returned a grouped conversation. Turn Conversation view off in Gmail Settings "
            "to keep this scan limited to individual Inbox messages, then retry. Nothing was imported.", code="grouped",
        )
    identity = header["messages"][0]
    message_id = identity["external_id"]
    if message_id != thread.get("last_message_id") or not identity["body"]:
        raise GmailImportError("The selected Gmail result could not be matched to its complete message.", code="message-identity")
    received_ts = identity.get("received_ts")
    if received_ts is None or not window_start <= received_ts < window_end:
        raise GmailImportError("A Gmail result was outside the authorized date window or had an unreadable date.", code="message-date")
    selected_route = unquote_plus(urlsplit(page.url).fragment)

    def verify_selected():
        verify_account(page)
        if unquote_plus(urlsplit(page.url).fragment) != selected_route:
            raise GmailImportError("Gmail left the selected Inbox message during collection.")

    for _ in range(3):
        verify_selected()
        content = page.evaluate(TEXT_SCRIPT, message_id)
        verify_selected()
        if not content:
            raise GmailImportError("A Gmail message body was unavailable.")
        if not content["trimmed"]:
            break
        page.evaluate(EXPAND_SCRIPT, message_id)
        page.wait_for_timeout(300)
    verify_selected()
    if content["clipped"] or content["trimmed"]:
        raise GmailImportError("A Gmail message is clipped or still collapsed. Nothing was imported.", code="clipped")
    return {
        **content, "external_id": message_id, "source_url": message_permalink(message_id),
        # Gmail's displayed sender date is not a verified delivery timestamp.
        # The server-side Inbox search supplies the received-date window.
        "message_date": datetime.fromtimestamp(received_ts, timezone.utc).isoformat(),
        "displayed_date": identity.get("received_at", ""), "complete": True,
    }


def scrape_gmail(*, days=30, limit=None, browser_channel="chrome", headed=False, progress=None):
    """Read every result body; retain only classified opportunities in memory."""
    from .gmail_parsing import parse_email

    get_account()
    if type(days) is not int or not 1 <= days <= 30:
        raise ValueError("Gmail lookback must be an integer from 1 to 30 days.")
    if limit is not None and (type(limit) is not int or not 1 <= limit <= MAX_MESSAGES):
        raise ValueError("Gmail batch limit must be an integer from 1 to 1,000.")
    now = datetime.now(timezone.utc).replace(microsecond=0)
    start = now - timedelta(days=days)
    result = GmailScanResult(window_start=start.isoformat(), window_end=now.isoformat())
    query = f"in:inbox after:{int(start.timestamp())} before:{int(now.timestamp())}"
    stop_at = time.monotonic() + MAX_RUN_SECONDS
    with _browser(browser_channel=browser_channel, headed=headed) as page:
        page.goto(search_url(query), wait_until="domcontentloaded")
        threads = collect_threads(page, query, limit=limit, stop_at=stop_at)
        if progress:
            progress(f"Gmail: {len(threads)} Inbox search results discovered.")
        seen = set()
        for thread in threads:
            if time.monotonic() >= stop_at:
                raise GmailImportError("Gmail's ten-minute scan budget expired. Nothing was imported.", code="time-budget")
            message = read_message(page, query, thread, window_start=start.timestamp(), window_end=now.timestamp())
            if message["external_id"] in seen:
                raise GmailImportError("Gmail returned duplicate message identities. Nothing was imported.")
            seen.add(message["external_id"])
            result.discovered += 1
            try:
                candidate = parse_email(message)
            except ValueError:
                raise GmailImportError("An opportunity email could not be read completely. Nothing was imported.") from None
            if candidate is None:
                result.skipped += 1
            else:
                result.items.append(candidate)
            if progress and result.discovered % 25 == 0:
                progress(f"Gmail: {result.discovered} messages read; {len(result.items)} candidates found.")
        result.complete = True
        if limit is not None:
            result.warnings.append("Only the explicitly requested result batch was scanned; this is not a full Inbox window scan.")
    return result
