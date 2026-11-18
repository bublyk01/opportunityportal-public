"""Read rendered messages from explicitly configured Slack channels using a local session.

No cookies, tokens, network response bodies, or the user's normal browser profile
are read. Login is interactive; collection only navigates and scrolls the channel.
"""
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import re
import time
from urllib.parse import urlsplit

from .slack_source import WORKSPACE_HOST, get_channel, message_permalink


MAX_MESSAGES = 1000
MAX_RUN_SECONDS = 300
LOGIN_SECONDS = 600
OVERLAP_SECONDS = 86400
PROFILE_DIR = Path(__file__).resolve().parents[2] / ".slack-browser"
PANE_SELECTOR = '[data-qa="message_pane"], .p-message_pane'


class SlackImportError(RuntimeError):
    pass


@dataclass
class SlackScanResult:
    items: list = field(default_factory=list)
    complete: bool = False
    discovered: int = 0
    skipped: int = 0
    warnings: list = field(default_factory=list)
    latest_ts: str | None = None
    window_start: str = ""


# Scope to the main channel pane so thread/sidebar/search messages cannot leak in.
MESSAGE_SCRIPT = r"""() => {
    const pane = document.querySelector('[data-qa="message_pane"], .p-message_pane');
    if (!pane) return {messages: [], pane: false};
    const visible = el => !!(el.getClientRects().length);
    const rows = [...pane.querySelectorAll(
        '.c-virtual_list__item[data-item-key], [data-qa="virtual-list-item"], '
        + '[data-qa="message_container"], .c-message_kit__message[data-ts]'
    )].filter(row => visible(row) && !row.closest('.p-threads_view, [data-qa="thread_view"]'));
    const messages = [];
    for (const row of rows) {
        let ts = row.getAttribute('data-item-key') || row.getAttribute('data-ts');
        if (!/^\d{10}\.\d{6}$/.test(ts || '')) {
            ts = row.querySelector('[data-ts]')?.getAttribute('data-ts');
        }
        if (!/^\d{10}\.\d{6}$/.test(ts || '')) {
            const href = row.querySelector('a[href*="/archives/"][href*="/p"]')?.getAttribute('href');
            const match = (href || '').match(/\/p(\d{10})(\d{6})(?:[?#]|$)/);
            ts = match ? match[1] + '.' + match[2] : null;
        }
        if (!ts || !/^\d{10}\.\d{6}$/.test(ts)) continue;
        const blocks = [...row.querySelectorAll(
            '[data-qa="message-text"], .c-message_kit__text, .c-message__body, '
            + '.c-message_attachment__text, .c-message_attachment__title'
        )];
        const leaves = blocks.filter(el => !blocks.some(other => other !== el && other.contains(el)));
        const text = leaves.map(el => el.innerText || '').filter(Boolean).join('\n').trim();
        const truncated = [...row.querySelectorAll('button')].some(button =>
            visible(button) && /^(see more|show more|read more|показати більше)$/i.test(button.innerText.trim()));
        const links = [...row.querySelectorAll('a[href]')].map(a => a.href).filter(href => /^https?:\/\//i.test(href));
        messages.push({ts, text, links, truncated,
            author: row.querySelector('[data-qa="message_sender_name"], .c-message__sender')?.innerText || ''});
    }
    const scroll = [...pane.querySelectorAll('.c-virtual_list__scroll_container, [data-qa="slack_kit_scrollbar"]')]
        .find(el => visible(el) && el.scrollHeight > el.clientHeight + 2);
    const intro = pane.querySelector('[data-qa="channel_intro"], .p-channel_intro, .c-channel_intro');
    const start = !!intro && visible(intro) && /beginning|very first|created this channel|початок|створили канал/i.test(intro.innerText || '');
    const historyLimited = /older messages.*(hidden|unavailable)|messages.*last 90 days|upgrade.*(message history|older messages)/i.test(pane.innerText || '');
    const loading = [...pane.querySelectorAll('[role="progressbar"], [data-qa="message_pane_loading"]')].some(visible);
    return {pane: true, messages, start, historyLimited, loading,
        scrollable: !!scroll,
        atTop: !scroll || scroll.scrollTop <= 2,
        atBottom: !scroll || scroll.scrollTop + scroll.clientHeight >= scroll.scrollHeight - 3};
}"""

SCROLL_SCRIPT = r"""direction => {
    const pane = document.querySelector('[data-qa="message_pane"], .p-message_pane');
    if (!pane) return false;
    const scroll = [...pane.querySelectorAll('.c-virtual_list__scroll_container, [data-qa="slack_kit_scrollbar"]')]
        .find(el => el.getClientRects().length && el.scrollHeight > el.clientHeight + 2);
    if (!scroll) return false;
    if (direction === 'bottom') scroll.scrollTop = scroll.scrollHeight;
    else scroll.scrollTop = Math.max(0, scroll.scrollTop - Math.max(100, scroll.clientHeight * 0.7));
    return true;
}"""


EXPAND_SCRIPT = r"""ts => {
    const pane = document.querySelector('[data-qa="message_pane"], .p-message_pane');
    if (!pane || !/^[0-9]{10}\.[0-9]{6}$/.test(ts)) return 0;
    const row = [...pane.querySelectorAll('[data-item-key], [data-ts]')].find(el =>
        (el.getAttribute('data-item-key') === ts || el.getAttribute('data-ts') === ts)
        && el.getClientRects().length
        && !el.closest('.p-threads_view, [data-qa="thread_view"]'));
    if (!row) return 0;
    const controls = [...row.querySelectorAll('button')].filter(button =>
        button.getClientRects().length && !button.disabled
        && /^(see more|show more|read more|показати більше)$/i.test(button.innerText.trim()));
    for (const button of controls) button.click();
    return controls.length;
}"""


def _timestamp(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{10}\.[0-9]{6}", value):
        raise ValueError("Expected a Slack message timestamp with six decimal places.")
    return Decimal(value)


def channel_is_open(url, *, channel=None):
    """Accept only the configured channel, including Slack's client redirect."""
    try:
        channel = get_channel(channel)
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port or parsed.query or parsed.fragment:
            return False
        if parsed.hostname == WORKSPACE_HOST:
            return parsed.path.rstrip("/") == f"/archives/{channel.channel_id}"
        return parsed.hostname == "app.slack.com" and bool(re.fullmatch(
            rf"/client/T[A-Z0-9]+/{channel.channel_id}/?", parsed.path,
        ))
    except (ValueError, TypeError):
        return False


def resource_allowed(url):
    """Permit Slack assets and normal Google/Microsoft sign-in, not posting links."""
    try:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port:
            return False
        host = parsed.hostname or ""
        suffixes = ("slack.com", "slack-edge.com", "slack-imgs.com", "slack-files.com")
        return host == WORKSPACE_HOST or any(host == suffix or host.endswith("." + suffix) for suffix in suffixes) or host in {
            "accounts.google.com", "accounts.youtube.com", "ssl.gstatic.com", "www.gstatic.com",
            "fonts.gstatic.com", "login.microsoftonline.com", "aadcdn.msftauth.net",
            "aadcdn.msauth.net", "logincdn.msauth.net",
        }
    except (ValueError, TypeError):
        return False


@contextmanager
def _browser(*, browser_channel, headed):
    if browser_channel not in {"chrome", "msedge", "chromium"}:
        raise ValueError("Unsupported browser. Use chrome, msedge, or chromium.")
    if WORKSPACE_HOST == "invalid" or WORKSPACE_HOST.endswith(".invalid"):
        raise SlackImportError(
            "This public copy uses a placeholder Slack source. Configure an approved "
            "workspace and channel allowlist before browser import. See README.md."
        )
    try:
        from playwright.sync_api import Error as PlaywrightError, sync_playwright
    except ImportError as exc:
        raise SlackImportError("Install requirements-importers.txt for browser support.") from exc
    try:
        with sync_playwright() as playwright:
            launch = {"headless": not headed}
            if not headed:
                # A taller viewport reduces scrolling through long announcements
                # while every step still overlaps the preceding visible region.
                launch["viewport"] = {"width": 1440, "height": 1440}
            if browser_channel != "chromium":
                launch["channel"] = browser_channel
            context = playwright.chromium.launch_persistent_context(
                str(PROFILE_DIR), locale="en-US", accept_downloads=False,
                service_workers="block", **launch,
            )
            try:
                def route_request(route):
                    request = route.request
                    if request.resource_type in {"image", "media"} or not resource_allowed(request.url):
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
        # Never print browser errors containing sign-in redirects or session state.
        raise SlackImportError(
            "Slack browser could not complete the operation. Close another importer window "
            "using .slack-browser, or retry with --login / --browser msedge."
        ) from exc


def _wait_for_channel(page, *, timeout_seconds, channel=None):
    channel = get_channel(channel)
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if channel_is_open(page.url, channel=channel) and page.locator(PANE_SELECTOR).count():
            return
        # The canonical archive URL first offers the desktop app, including in
        # an authenticated session. Follow only its exact same-channel browser
        # link; never click arbitrary links found in messages or sign-in forms.
        if urlsplit(page.url).hostname == WORKSPACE_HOST:
            browser_link = page.locator(
                f'a[href="/messages/{channel.channel_id}"], '
                f'a[href="https://{WORKSPACE_HOST}/messages/{channel.channel_id}"]'
            )
            if browser_link.count():
                page.goto(
                    f"https://{WORKSPACE_HOST}/messages/{channel.channel_id}",
                    wait_until="domcontentloaded", timeout=45000,
                )
                continue
        page.wait_for_timeout(1000)
    raise SlackImportError(
        "The configured Slack channel is not open. Run sync_slack --login and sign in "
        f"to the configured workspace. Your account must have access to channel {channel.channel_id}."
    )


def login_slack(*, browser_channel="chrome", channel=None):
    """Open normal login; return after the requested channel is available."""
    channel = get_channel(channel)
    with _browser(browser_channel=browser_channel, headed=True) as page:
        page.goto(channel.url, wait_until="domcontentloaded", timeout=45000)
        _wait_for_channel(page, timeout_seconds=LOGIN_SECONDS, channel=channel)


def collect_messages(page, *, cutoff, limit=None, stop_at=None, progress=None, channel=None):
    """Traverse virtualized history; a stable top alone never proves completeness."""
    channel = get_channel(channel)
    stop_at = stop_at if stop_at is not None else time.monotonic() + MAX_RUN_SECONDS
    cutoff = Decimal(str(cutoff))
    messages = {}
    warnings = []
    newest = None
    stable = 0
    bottom_ready = False
    initial_location = urlsplit(page.url)._replace(query="", fragment="").geturl()

    def verify_channel():
        current = urlsplit(page.url)._replace(query="", fragment="").geturl()
        if not channel_is_open(page.url, channel=channel) or current != initial_location:
            raise SlackImportError("Slack left the configured channel. Nothing was imported.")

    def read_state():
        # Slack's ordinary expand controls reveal text inside the same message.
        # They are the only message buttons the collector clicks.
        for _ in range(3):
            verify_channel()
            state = page.evaluate(MESSAGE_SCRIPT)
            verify_channel()
            collapsed = [row for row in state.get("messages", []) if row.get("truncated")]
            if not collapsed:
                return state
            clicked = 0
            for row in collapsed:
                _timestamp(row.get("ts"))
                verify_channel()
                clicked += page.evaluate(EXPAND_SCRIPT, row["ts"])
                verify_channel()
            if not clicked:
                return state
            page.wait_for_timeout(500)
        verify_channel()
        state = page.evaluate(MESSAGE_SCRIPT)
        verify_channel()
        return state

    # Slack may open at the last read position; establish the current bottom first.
    while time.monotonic() < stop_at:
        verify_channel()
        page.evaluate(SCROLL_SCRIPT, "bottom")
        page.wait_for_timeout(800)
        state = read_state()
        if not state.get("pane"):
            raise SlackImportError("Slack's message layout is unavailable. Nothing was imported.")
        current = max((row["ts"] for row in state["messages"]), default=None)
        stable = stable + 1 if current == newest and state.get("atBottom") and not state.get("loading") else 0
        newest = current
        if stable >= 3 and current:
            bottom_ready = True
            break
    if not bottom_ready:
        return [], False, ["Could not establish the latest channel messages before the time limit."]

    stable = 0
    previous_oldest = None
    selected = []
    reported_count = -1
    while time.monotonic() < stop_at:
        verify_channel()
        state = read_state()
        if not state.get("pane"):
            raise SlackImportError("Slack's message layout changed. Nothing was imported.")
        for row in state["messages"]:
            ts = row.get("ts")
            _timestamp(ts)
            if row.get("truncated"):
                return [], False, ["A message is collapsed. Expand it in the importer browser and retry."]
            # Preserve the best rendered text when consecutive virtualized views overlap.
            if ts not in messages or len(row.get("text", "")) >= len(messages[ts].get("text", "")):
                messages[ts] = row
        selected = sorted(
            (row for ts, row in messages.items() if _timestamp(ts) >= cutoff),
            key=lambda row: row["ts"], reverse=True,
        )
        if progress and len(selected) != reported_count:
            progress(f"Read {len(selected)} messages in the requested window...")
            reported_count = len(selected)
        oldest = min(messages, default=None)
        if limit is not None and len(selected) >= limit:
            return selected[:limit], True, [f"Explicit batch of the newest {limit} messages; checkpoint is preserved."]
        if oldest and _timestamp(oldest) <= cutoff:
            return selected, True, warnings
        if state.get("historyLimited"):
            return selected, False, ["Slack limits visible history before the requested scan boundary."]
        if state.get("start") and state.get("atTop") and not state.get("loading"):
            return selected, True, warnings
        if len(messages) >= MAX_MESSAGES:
            return selected, False, ["Message safety limit reached before the requested scan boundary; use --limit or a shorter --days window."]
        stable = stable + 1 if (
            oldest == previous_oldest and state.get("atTop") and not state.get("loading")
        ) else 0
        previous_oldest = oldest
        if stable >= 8:
            return selected, False, ["Older history stopped loading before the requested boundary; no import was performed."]
        page.evaluate(SCROLL_SCRIPT, "up")
        page.wait_for_timeout(1200 if state.get("atTop") or state.get("loading") else 400)
    return selected, False, ["Time budget expired before the requested scan boundary."]


def scrape_slack(*, days=30, limit=None, browser_channel="chrome", headed=False,
                 since=None, progress=None, channel=None):
    """Collect channel messages then parse candidates; never write database records."""
    channel = get_channel(channel)
    if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 365:
        raise ValueError("--days must be an integer between 1 and 365.")
    if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_MESSAGES):
        raise ValueError(f"--limit must be an integer between 1 and {MAX_MESSAGES}.")
    cutoff = Decimal(str((datetime.now(timezone.utc) - timedelta(days=days)).timestamp()))
    if since is not None:
        checkpoint = _timestamp(since)
        if checkpoint > Decimal(str(datetime.now(timezone.utc).timestamp())):
            raise ValueError("Slack checkpoint is in the future; run with --rescan.")
        cutoff = max(cutoff, checkpoint - OVERLAP_SECONDS)
    result = SlackScanResult(window_start=str(cutoff))
    if progress:
        progress(f"Opening Slack channel {channel.channel_id} using its separate local profile...")
    with _browser(browser_channel=browser_channel, headed=headed) as page:
        page.goto(channel.url, wait_until="domcontentloaded", timeout=45000)
        _wait_for_channel(page, timeout_seconds=120 if headed else 25, channel=channel)
        rows, result.complete, result.warnings = collect_messages(
            page, cutoff=cutoff, limit=limit, progress=progress, channel=channel,
        )
    from .slack_parsing import parse_message

    result.discovered = len(rows)
    for row in rows:
        row["source_url"] = message_permalink(row["ts"], channel=channel)
        candidate = parse_message(row)
        if candidate is None:
            result.skipped += 1
        else:
            result.items.append(candidate)
    result.latest_ts = max((row["ts"] for row in rows), default=None)
    result.warnings.append("Top-level channel messages only; thread replies and attached files are not scanned.")
    return result
