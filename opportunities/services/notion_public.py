"""Read the configured public Notion source in a signed-out browser.

Browser support is optional; importing this module does not launch a browser
or require Playwright. No credentials or an existing Chrome profile are used.
"""
from dataclasses import dataclass, field
from datetime import date, datetime
import re
import time
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo


# Reserved placeholders support offline tests without identifying a real source.
# Configure an approved host, page, and collection before using browser import.
PUBLIC_HOST = "notion.example.invalid"
SOURCE_URL = (
    f"https://{PUBLIC_HOST}/"
    "Jobs-for-Students-11111111111141118111111111111111"
)
SOURCE_KEY = "example-opportunities"
MAX_ITEMS = 500
MAX_RUN_SECONDS = 300
COLLECTION_ID = "22222222-2222-4222-8222-222222222222"
COLLECTION_SELECTOR = f'.notion-collection_view-block[data-block-id="{COLLECTION_ID}"]'
CARD_SELECTOR = '.notion-collection-card, a.notion-collection-item, a[href*="pvs=25"]'
RESOURCE_HOSTS = {
    PUBLIC_HOST, "notion.so", "www.notion.so", "app.notion.com", "www.notion.com",
    "challenges.cloudflare.com",
}


class NotionImportError(RuntimeError):
    """The public source could not be read reliably."""


@dataclass
class ScrapeResult:
    items: list[dict] = field(default_factory=list)
    complete: bool = False
    discovered: int = 0
    warnings: list[str] = field(default_factory=list)


def canonical_page_id(value):
    """Normalize a UUID without accepting URLs or arbitrary identifiers."""
    if not isinstance(value, str) or not re.fullmatch(
        r"(?:[0-9a-fA-F]{32}|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-"
        r"[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})",
        value,
    ):
        raise ValueError("Expected a Notion page ID (32 hex digits or a UUID).")
    return value.replace("-", "").lower()


def validate_source_url(value):
    """Allow only page URLs on the one configured HTTPS public source."""
    if not isinstance(value, str) or any(char.isspace() for char in value):
        raise ValueError("Invalid public Notion source URL.")
    parts = urlsplit(value)
    if (
        parts.scheme != "https"
        or parts.netloc.lower() != PUBLIC_HOST
        or parts.username is not None
        or parts.password is not None
        or "\\" in value
    ):
        raise ValueError(f"Source URLs must use https://{PUBLIC_HOST}/.")
    path = parts.path.strip("/")
    if "/" in path or "%" in path:
        raise ValueError("Expected a public Notion page URL.")
    match = re.search(r"([0-9a-fA-F]{32})$", path)
    if match:
        page_id = canonical_page_id(match.group(1))
    else:
        page_id = canonical_page_id(path)
    return f"https://{PUBLIC_HOST}/{page_id}"


def resource_allowed(url, *, navigation=False, main_frame=False):
    """Constrain browser traffic, including redirects and embedded content."""
    parts = urlsplit(url)
    if (
        parts.scheme != "https"
        or parts.username is not None
        or parts.password is not None
        or ":" in parts.netloc
        or "\\" in url
    ):
        return False
    host = parts.hostname or ""
    if navigation and main_frame:
        try:
            validate_source_url(url)
        except ValueError:
            return False
        return True
    return host in RESOURCE_HOSTS or host.endswith(".challenges.cloudflare.com")


def normalize_label(value):
    return re.sub(r"[\s:]+", " ", str(value)).strip().casefold()


PROPERTY_NAMES = {
    "organization": (
        "organization", "organisation", "company", "employer",
        "компанія", "організація", "роботодавець", "назва компанії",
    ),
    "type": ("opportunity type", "kind", "type", "тип можливості", "тип пропозиції", "тип"),
    "format": ("format", "формат"),
    "employment_type": ("employment type", "working hours", "schedule", "зайнятість"),
    "work_mode": ("work mode", "work arrangement", "режим роботи"),
    "location": ("location", "city", "локація", "місто", "місце роботи"),
    "deadline": (
        "deadline", "application deadline", "дедлайн", "кінцевий термін",
        "дата завершення", "термін подачі", "дедлайн подачі",
    ),
}
TYPE_VALUES = {
    "job": "job", "vacancy": "job", "вакансія": "job", "робота": "job",
    "internship": "internship", "intership": "internship", "paid internship": "internship",
    "unpaid internship": "internship", "стажування": "internship", "практика": "internship",
    "event": "event", "conference": "event", "workshop": "event",
    "подія": "event", "захід": "event", "конференція": "event",
}
EMPLOYMENT_VALUES = {
    "full-time": "full_time", "full time": "full_time",
    "part-time": "part_time", "part time": "part_time",
}
WORK_MODE_VALUES = {
    "remote": "remote", "online": "remote", "дистанційно": "remote", "віддалено": "remote",
    "hybrid": "hybrid", "гібрид": "hybrid",
    "offline": "on_site", "on-site": "on_site", "onsite": "on_site", "in person": "on_site",
    "in-person": "on_site", "офлайн": "on_site",
}
SOURCE_LABELS = {
    name for aliases in PROPERTY_NAMES.values() for name in aliases
} | {"source coordinator", "position title", "program", "send your application to", "status"}


def parse_deadline(value):
    """Parse explicit dates; ambiguous or missing years are left for review."""
    value = str(value).strip()
    if not value or value.casefold() in {"empty", "порожньо", "не вказано"}:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        pass
    for pattern in ("%B %d, %Y", "%b %d, %Y", "%d.%m.%Y", "%d %B %Y"):
        try:
            return datetime.strptime(value, pattern).date()
        except ValueError:
            continue
    return None


def explicit_organization_introduction(description):
    """Extract only an explicit named-organization introduction, with evidence."""
    first_paragraph = description.strip().split("\n", 1)[0]
    match = re.match(
        r"(?P<name>\w[^:\n]{1,159}?)\s+(?P<verb>is|are|officially invites|invites|invites you)\s+"
        r"(?P<introduction>.+)", first_paragraph,
    )
    if not match:
        return "", ""
    name, verb, introduction = match.group("name", "verb", "introduction")
    generic_names = {
        "this", "this role", "the role", "this position", "the position",
        "this opportunity", "our client", "our company", "the company", "it",
        "our team", "the team", "the organizers", "we", "they", "you",
    }
    organization_words = (
        r"\b(compan(?:y|ies)|organizations?|organisations?|firms?|startups?|banks?|universit(?:y|ies)|"
        r"nonprofit|non-profit|charity|agency|institute|academy|conference|association)\b"
    )
    describes_organization = re.match(r"(?:a |an |the |one of )", introduction, re.I) and re.search(
        organization_words, introduction[:200], re.I
    )
    offers_role = re.match(r"(?:offering|hiring|seeking|looking for|recruiting)\b", introduction, re.I)
    invitation = "invites" in verb
    if (name.casefold() in generic_names or name.casefold().startswith(
        ("this ", "that ", "our ", "your ", "their ", "a ", "an ")
    ) or not (describes_organization or offers_role or invitation)):
        return "", ""
    if name.startswith("The "):
        name = name[4:]
    evidence = re.split(r"(?<=[.!?])\s", first_paragraph, maxsplit=1)[0]
    return name.strip(), evidence


def source_properties(detail):
    """Read complete property rows, stopping before the description prose."""
    supplied = detail.get("properties", {})
    properties = {normalize_label(key): str(value).strip() for key, value in supplied.items()}
    text = detail.get("text", "")
    description = detail.get("description", "").strip()
    if description:
        boundary = text.find(description.splitlines()[0].strip())
        if boundary >= 0:
            text = text[:boundary]
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    current = None
    values = []
    for line in lines + ["__end__"]:
        label = normalize_label(line)
        if label in SOURCE_LABELS or line == "__end__":
            if current and current not in properties:
                properties[current] = "\n".join(values)
            current = label if label in SOURCE_LABELS else None
            values = []
        elif current:
            values.append(line)
    return properties


def geographic_location(description):
    """Recognize job/venue or applicant-location statements, not headquarters."""
    place = r"([A-ZА-ЯІЇЄҐ][\w’'-]*(?:\s+[A-ZА-ЯІЇЄҐ][\w’'-]*){0,2})"
    patterns = (
        (r"(?i:\b(?:on[- ]site|offline|in[- ]person)\s+in\s+)" + place, "on_site", False),
        (r"(?i:\b(?:at|in)\s+(?:the|our)\s+)" + place + r"(?i:\s+office\b)", "", False),
        (r"(?i:\b(?:office|workplace|role|position)\s+(?:is\s+)?(?:based|located)\s+in\s+)" + place, "", False),
        (r"(?im:^[ \t]*(?:[-•]\s*)?currently located in\s+)" + place, "", True),
        (r"(?i:\b(?:applicants|candidates)\s+must\s+be\s+(?:based|located)\s+in\s+)" + place, "", True),
        (r"(?i:\byou\s+(?:(?:must|should)\s+be|are)\s+(?:currently\s+)?(?:based|located)\s+in\s+)" + place, "", True),
    )
    for pattern, mode, eligibility in patterns:
        match = re.search(pattern, description)
        if match:
            return match.group(1), match.group(0), mode, eligibility
    return "", "", "", False


def normalize_listing(card, detail):
    """Map labeled public properties, preserving the source text for review.

    The source mixes opportunity categories and work arrangements between
    Format and Type. Classify their values instead of copying label positions.
    """
    source_url = validate_source_url(card["source_url"])
    external_id = canonical_page_id(source_url.rsplit("/", 1)[-1])
    properties = source_properties(detail)
    values = {}
    for field_name, aliases in PROPERTY_NAMES.items():
        value = next((properties[name] for name in aliases if properties.get(name)), "")
        if value.casefold() in {"empty", "порожньо", "не вказано"}:
            value = ""
        values[field_name] = value

    warnings = []
    title = (detail.get("title") or card.get("title") or "").strip()
    description = detail.get("description", "").strip()
    if description and not detail.get("description_complete", False):
        warnings.append("Saved description completeness has not been verified; check the original posting before publication.")
    organization_evidence = ""
    if not values["organization"]:
        values["organization"], organization_evidence = explicit_organization_introduction(
            description
        )
        if organization_evidence:
            warnings.append("Employer extracted from the source introduction; verify before publication.")
    deadline_text = values["deadline"] or card.get("deadline_text", "")
    deadline = parse_deadline(deadline_text)
    if deadline_text and deadline is None:
        warnings.append(f"Unrecognized deadline: {deadline_text}")
    raw_type = normalize_label(values["type"])
    raw_format = normalize_label(values["format"])
    categories = [TYPE_VALUES.get(raw_format), TYPE_VALUES.get(raw_type)]
    specific = next((kind for kind in categories if kind in {"internship", "event"}), "")
    opportunity_type = specific or next((kind for kind in categories if kind), "")
    employment_type = next((EMPLOYMENT_VALUES[value] for value in (
        normalize_label(values["employment_type"]), raw_type, raw_format,
    ) if value in EMPLOYMENT_VALUES), "")
    if not specific and re.search(r"\bintern(?:ship)?\b|стажуван", title, re.I):
        opportunity_type = "internship"
    elif not opportunity_type and not raw_type and re.search(r"\bconference\b|\bworkshop\b", title, re.I):
        opportunity_type = "event"
    elif not opportunity_type and employment_type:
        opportunity_type = "job"
    if values["type"] and not opportunity_type:
        warnings.append(f"Opportunity type needs review: {values['type']}")

    work_mode = next((WORK_MODE_VALUES[value] for value in (
        normalize_label(values["work_mode"]), raw_format, raw_type,
    ) if value in WORK_MODE_VALUES), "")
    location = values["location"]
    location_evidence = ""
    eligibility = False
    if normalize_label(location) in WORK_MODE_VALUES:
        work_mode = work_mode or WORK_MODE_VALUES[normalize_label(location)]
        location = ""
    elif normalize_label(location) in TYPE_VALUES or normalize_label(location) in EMPLOYMENT_VALUES:
        warnings.append("Source location contained an opportunity category or working hours and was not used.")
        location = ""
    if not location:
        location, location_evidence, described_mode, eligibility = geographic_location(description)
        work_mode = work_mode or described_mode
    if eligibility:
        warnings.append("Location is an explicit applicant location requirement; no office address is specified.")

    return {
        "external_id": external_id,
        "source_url": source_url,
        "title": title,
        "organization": values["organization"],
        "type": opportunity_type,
        "employment_type": employment_type,
        "work_mode": work_mode,
        "description": description,
        "location": location,
        "deadline": deadline,
        "raw_data": {
            "properties": dict(detail.get("properties", {})),
            "parsed_properties": properties,
            "text": detail.get("text", ""),
            "card_text": card.get("text", ""),
            "warnings": warnings,
            "organization_evidence": organization_evidence,
            "field_evidence": {
                "organization": organization_evidence or values["organization"],
                "type": f"Format: {values['format']}; Type: {values['type']}; Title: {title}",
                "location": location_evidence or values["location"],
                "work_mode": values["work_mode"] or values["format"],
                "employment_type": values["employment_type"] or values["type"],
            },
            "parser_version": 2,
            "description_complete": bool(detail.get("description_complete", False)),
        },
    }


def candidate_from_snapshot(payload):
    """Re-run mapping against the original saved public evidence, offline."""
    raw = payload.get("raw_data", {})
    if not raw.get("text") and not raw.get("properties"):
        raise ValueError("This import has no original source properties or text to reprocess.")
    card = {
        "source_url": payload["source_url"], "title": payload.get("title", ""),
        "text": raw.get("card_text", ""), "deadline_text": payload.get("deadline") or "",
    }
    detail = {
        "title": payload.get("title", ""), "description": payload.get("description", ""),
        "properties": raw.get("properties", {}), "text": raw.get("text", ""),
        "description_complete": raw.get("description_complete", False),
    }
    return normalize_listing(card, detail)


CARD_SCRIPT = r"""(container) => {
    const cards = container.querySelectorAll('.notion-collection-card, a.notion-collection-item, a[href*="pvs=25"]');
    return Array.from(cards).map(card => {
        const title = card.querySelector('.notion-property-title, [data-testid="property-value-title"]');
        const anchor = card.matches('a[href]') ? card : card.querySelector('a[href]');
        return {
            id: card.getAttribute('data-block-id'),
            href: anchor ? anchor.getAttribute('href') : null,
            title: title ? title.innerText.trim() : '',
            text: card.innerText.trim()
        };
    });
}"""

DETAIL_SCRIPT = r"""() => {
    const title = document.querySelector('.notion-page-block .notranslate, [data-testid="page-title"], main h1, h1');
    const content = document.querySelector('.notion-page-content');
    const blocks = document.querySelectorAll('.notion-text-block, .notion-bulleted_list-block, .notion-numbered_list-block, .notion-header-block, .notion-sub_header-block, .notion-sub_sub_header-block, .notion-callout-block');
    const blockData = Array.from(blocks).map(el => {
        const leaf = el.querySelector('[data-content-editable-leaf]');
        return {id:el.getAttribute('data-block-id'), text:(leaf ? leaf.innerText : el.innerText).trim()};
    }).filter(block => block.text);
    const properties = {};
    for (const row of document.querySelectorAll('.notion-collection-page-properties > div, [data-testid="property-row"]')) {
        const name = row.querySelector('.notion-property-name, [data-testid="property-name"]');
        if (name) {
            const text = row.innerText.trim();
            properties[name.innerText.trim()] = text.slice(name.innerText.trim().length).trim();
        }
    }
    return {
        title: title ? title.innerText.trim() : '',
        properties,
        description: content ? content.innerText.trim() : Array.from(blocks).map(el => el.innerText.trim()).filter(Boolean).join('\n'),
        text: document.body.innerText.trim(),
        blocks: blockData
    };
}"""


DETAIL_SCROLL_SCRIPT = r"""el => {
    let scroller = el.parentElement;
    while (scroller && !(scroller.scrollHeight > scroller.clientHeight + 2 &&
        /auto|scroll/.test(getComputedStyle(scroller).overflowY))) scroller = scroller.parentElement;
    scroller = scroller || document.scrollingElement;
    scroller.scrollTop += Math.max(1, scroller.clientHeight * 0.75);
    return scroller.scrollTop + scroller.clientHeight >= scroller.scrollHeight - 2;
}"""


def _read_detail(page, *, stop_at):
    """Collect virtualized description blocks while traversing the whole page."""
    blocks = {}
    first = None
    stable_bottom = 0
    complete = False
    while time.monotonic() < stop_at:
        data = page.evaluate(DETAIL_SCRIPT)
        if first is None:
            first = dict(data)
        else:
            title = data.get("title", "").strip()
            if title and title.casefold() not in {"notion", "new page", "untitled"}:
                first["title"] = title
            first["properties"] = {
                **first.get("properties", {}),
                **{key: value for key, value in data.get("properties", {}).items() if str(value).strip()},
            }
        previous_count = len(blocks)
        for block in data.get("blocks", []):
            if not block.get("id"):
                raise NotionImportError("A description block had no stable ID; the source layout may have changed.")
            blocks[block["id"]] = block["text"]
        if blocks:
            first_line = next(iter(blocks.values())).splitlines()[0]
            boundary = data.get("text", "").find(first_line)
            current_boundary = first.get("text", "").find(first_line)
            if boundary >= 0 and boundary > current_boundary:
                first["text"] = data["text"]
        anchor = page.locator(
            ".notion-text-block, .notion-bulleted_list-block, .notion-numbered_list-block"
        ).first
        if not anchor.count():
            break
        at_bottom = anchor.evaluate(DETAIL_SCROLL_SCRIPT)
        page.wait_for_timeout(800)
        loading = page.locator('[role="progressbar"], .notion-spinner')
        is_loading = any(element.is_visible() for element in loading.all())
        if at_bottom and len(blocks) == previous_count and not is_loading:
            stable_bottom += 1
        else:
            stable_bottom = 0
        if stable_bottom >= 3:
            complete = bool(blocks)
            break
    detail = dict(first or {})
    if blocks:
        description = "\n".join(blocks.values())
        initial_text = detail.get("text", "")
        first_line = next(iter(blocks.values())).splitlines()[0]
        boundary = initial_text.find(first_line)
        header = initial_text[:boundary] if boundary >= 0 else initial_text + "\n"
        detail.update(description=description, text=header + description)
    detail["description_complete"] = complete
    return detail, complete


def _check_page(page, response=None):
    validate_source_url(page.url)
    title = page.title().casefold()
    text = page.locator("body").inner_text(timeout=5000)
    if (response is not None and response.status in {403, 429}) or any(
        marker in title or marker in text.casefold()
        for marker in (
            "just a moment", "verify you are human", "security verification",
            "incompatible browser extension",
        )
    ):
        retry = response.headers.get("retry-after") if response is not None else None
        suffix = f" Retry-After: {retry}." if retry else ""
        raise NotionImportError(
            "The public source requires browser/security verification or is rate limited. "
            "No data was imported. Try later in a normal browser; the importer does not "
            "bypass access checks." + suffix
        )
    if any(marker in text.casefold() for marker in (
        "this page couldn’t be found", "this page couldn't be found",
        "you do not have access", "log in to continue",
    )):
        raise NotionImportError(
            "The public Notion page is unavailable or requires login. "
            "Check its published URL; no data was imported."
        )
    if response is not None and response.status >= 400:
        raise NotionImportError(f"The public source returned HTTP {response.status}.")


def _card_data(raw):
    page_id = raw.get("id")
    if page_id:
        page_id = canonical_page_id(page_id)
        source_url = f"https://{PUBLIC_HOST}/{page_id}"
    else:
        href = raw.get("href") or ""
        source_url = validate_source_url(
            f"https://{PUBLIC_HOST}{href}" if href.startswith("/") else href
        )
    # Preserve the published link for navigation. IDs still provide canonical
    # identity, while the public site can treat slug URLs differently.
    href = raw.get("href") or ""
    navigation_url = source_url
    if href:
        absolute_url = f"https://{PUBLIC_HOST}{href}" if href.startswith("/") else href
        try:
            normalized_href = validate_source_url(absolute_url)
        except ValueError:
            pass
        else:
            if normalized_href == source_url:
                navigation_url = absolute_url
    text = raw.get("text", "")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    title = raw.get("title") or (lines[0] if lines else "")
    deadline_text = next((line for line in lines[1:] if parse_deadline(line)), "")
    return {
        "source_url": source_url, "navigation_url": navigation_url, "title": title,
        "text": text, "deadline_text": deadline_text,
    }


def _discover_cards(page, *, limit, stop_at):
    container = page.locator(COLLECTION_SELECTOR)
    if not container.count():
        raise NotionImportError(
            "Expected one public Notion database. Its page layout may have changed."
        )
    container = container.first
    found = {}
    stable_bottom = 0
    while time.monotonic() < stop_at:
        for raw in container.evaluate(CARD_SCRIPT):
            try:
                card = _card_data(raw)
            except ValueError as exc:
                raise NotionImportError("A listing had an invalid source ID or URL.") from exc
            found[card["source_url"]] = card
        if limit is not None and len(found) >= limit:
            return list(found.values())[:limit], True, [
                f"Requested batch of {limit} listings; this does not verify the entire database."
            ]
        if len(found) >= MAX_ITEMS:
            return list(found.values()), False, [
                f"Reached the {MAX_ITEMS}-listing safety limit before proving the end."
            ]
        load_more = container.get_by_role(
            "button", name=re.compile(r"^(Load more|Show more|Завантажити ще|Показати ще)$", re.I)
        )
        if load_more.count() and load_more.first.is_visible():
            load_more.first.click(timeout=5000)
            stable_bottom = 0
            page.wait_for_timeout(1200)
            continue
        before_count = len(found)
        at_bottom = container.evaluate(r"""el => {
            let scroller = el.parentElement;
            while (scroller && !(scroller.scrollHeight > scroller.clientHeight + 2 &&
                /auto|scroll/.test(getComputedStyle(scroller).overflowY))) scroller = scroller.parentElement;
            scroller = scroller || document.scrollingElement;
            scroller.scrollTop += Math.max(400, scroller.clientHeight * 0.8);
            return scroller.scrollTop + scroller.clientHeight >= scroller.scrollHeight - 2;
        }""")
        page.wait_for_timeout(1200)
        for raw in container.evaluate(CARD_SCRIPT):
            card = _card_data(raw)
            found[card["source_url"]] = card
        loading = container.locator('[role="progressbar"], .notion-spinner')
        is_loading = any(locator.is_visible() for locator in loading.all())
        if at_bottom and len(found) == before_count and not is_loading:
            stable_bottom += 1
        else:
            stable_bottom = 0
        if stable_bottom >= 3:
            if not found:
                raise NotionImportError(
                    "No recognizable listing cards were found. The source may be empty "
                    "or its layout may have changed; no data was imported."
                )
            return list(found.values()), True, []
    return list(found.values()), False, ["Collection exceeded its five-minute time budget."]


def _active_card(card, *, today):
    lines = [line.strip().casefold() for line in card.get("text", "").splitlines() if line.strip()]
    if lines and lines[-1] in {"closed", "закрито", "закрита"}:
        return False
    deadline = parse_deadline(card.get("deadline_text", ""))
    return deadline is None or deadline >= today


def _wait_for_page(page, response, *, detail=False, verification_timeout=45000):
    from playwright.sync_api import TimeoutError as PlaywrightTimeout

    selector = (
        ".notion-page-content, .notion-text-block, .notion-bulleted_list-block"
        if detail else COLLECTION_SELECTOR + ' a[href*="pvs=25"]'
    )
    try:
        page.wait_for_selector(selector, timeout=verification_timeout if not detail else 30000)
    except PlaywrightTimeout as exc:
        _check_page(page, response)
        raise NotionImportError(
            "Expected listing content did not load. The public page layout may have "
            f"changed or the source may be temporarily unavailable. Page: {page.url}; "
            f"title: {page.title()!r}. Nothing was imported."
        ) from exc
    # The initial response may have been a verification challenge followed by
    # a normal reload. Expected content proves readiness; inspect current state.
    _check_page(page)


def scrape_public_notion(*, limit=None, browser_channel="chrome", headed=False, progress=None):
    """Collect public records only. This function never writes to the database."""
    if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_ITEMS):
        raise ValueError(f"limit must be an integer between 1 and {MAX_ITEMS}.")
    if browser_channel not in {"chrome", "msedge", "chromium"}:
        raise ValueError("Unsupported browser. Use chrome, msedge, or chromium.")
    if not isinstance(headed, bool):
        raise ValueError("headed must be a boolean.")
    if PUBLIC_HOST == "invalid" or PUBLIC_HOST.endswith(".invalid"):
        raise NotionImportError(
            "This public copy uses a placeholder Notion source. Configure an approved "
            "host, page, and collection before browser import. See README.md."
        )
    validate_source_url(SOURCE_URL)
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise NotionImportError(
            "Browser support is missing. Install requirements-importers.txt in the virtual environment."
        ) from exc

    stop_at = time.monotonic() + MAX_RUN_SECONDS
    try:
        with sync_playwright() as playwright:
            launch_options = {"headless": not headed}
            if browser_channel != "chromium":
                launch_options["channel"] = browser_channel
            # A separate, ignored profile keeps this source's anonymous site
            # state between runs; it never opens the user's Chrome profile.
            profile = Path(__file__).resolve().parents[2] / ".notion-browser"
            context = playwright.chromium.launch_persistent_context(
                str(profile), locale="en-US", accept_downloads=False,
                service_workers="block", **launch_options,
            )
            try:
                def route_request(route):
                    request = route.request
                    host = urlsplit(request.url).hostname or ""
                    security_resource = host == "challenges.cloudflare.com" or host.endswith(".challenges.cloudflare.com")
                    if (request.resource_type in {"image", "font", "media"} and not security_resource) or not resource_allowed(
                        request.url,
                        navigation=request.is_navigation_request(),
                        main_frame=request.frame == page.main_frame,
                    ):
                        route.abort()
                    else:
                        route.continue_()

                context.route("**/*", route_request)
                page = context.new_page()
                page.set_default_timeout(20000)
                if progress:
                    progress("Loading the public Notion gallery...")
                response = page.goto(SOURCE_URL, wait_until="domcontentloaded", timeout=45000)
                _wait_for_page(page, response, verification_timeout=120000 if headed else 45000)
                cards, complete, warnings = _discover_cards(page, limit=limit, stop_at=stop_at)
                result = ScrapeResult(complete=complete, discovered=len(cards), warnings=warnings)
                if progress:
                    progress(f"Discovered {len(cards)} listings; collection complete: {complete}.")
                today = datetime.now(ZoneInfo("UTC")).date()
                active_cards = [card for card in cards if _active_card(card, today=today)]
                if len(active_cards) != len(cards):
                    result.warnings.append(
                        f"Skipped {len(cards) - len(active_cards)} explicitly closed or expired listings."
                    )
                for index, card in enumerate(active_cards, start=1):
                    if time.monotonic() >= stop_at:
                        result.complete = False
                        result.warnings.append("Time budget expired while reading detail pages.")
                        break
                    if progress:
                        progress(f"Reading {index}/{len(active_cards)}: {card['title']}")
                    response = page.goto(card["navigation_url"], wait_until="domcontentloaded", timeout=30000)
                    _wait_for_page(page, response, detail=True)
                    if validate_source_url(page.url) != card["source_url"]:
                        raise NotionImportError(
                            "A listing redirected to a different public page. Nothing was imported."
                        )
                    detail, detail_complete = _read_detail(page, stop_at=stop_at)
                    if validate_source_url(page.url) != card["source_url"]:
                        raise NotionImportError("A listing changed pages while being read. Nothing was imported.")
                    if not detail_complete or not detail.get("title") or not detail.get("description"):
                        result.complete = False
                        result.warnings.append(f"Detail content did not finish loading: {card['source_url']}")
                        continue
                    result.items.append(normalize_listing(card, detail))
                    page.wait_for_timeout(1200)
                return result
            finally:
                context.close()
    except PlaywrightError as exc:
        raise NotionImportError(
            "The public source could not finish loading, or the selected browser is unavailable. "
            "Check the URL in a signed-out browser, retry later, or select --browser msedge. "
            "For bundled Chromium, run python -m playwright install chromium."
        ) from exc
