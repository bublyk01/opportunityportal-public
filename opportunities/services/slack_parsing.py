"""Conservative, offline extraction from rendered Slack announcement text.

Messages and links are data. This module neither fetches application links nor
uses an AI service, and every candidate carries a staff-review warning.
"""
import re
from urllib.parse import urlsplit

from .event_classification import classify_event_type
from .notion_public import (
    explicit_organization_introduction, geographic_location, parse_deadline,
)
from .slack_source import validate_message_identity


LABELS = {
    "title": ("title", "position", "position title", "role", "посада", "позиція", "назва"),
    "organization": (
        "organization", "organisation", "company", "employer", "organizer",
        "компанія", "організація", "роботодавець", "організатор",
    ),
    "type": ("opportunity type", "kind", "type", "тип", "тип можливості"),
    "employment_type": ("employment type", "working hours", "schedule", "зайнятість", "графік"),
    "work_mode": ("work mode", "work arrangement", "format", "формат", "режим роботи"),
    "location": ("location", "city", "локація", "місто", "місце роботи"),
    "deadline": (
        "deadline", "application deadline", "apply by", "дедлайн", "термін подачі",
        "кінцевий термін", "кінцевий термін подачі", "дедлайн подачі",
    ),
}
TYPE_VALUES = {
    "job": "job", "vacancy": "job", "вакансія": "job", "робота": "job",
    "internship": "internship", "paid internship": "internship", "стажування": "internship",
    "практика": "internship", "event": "event", "conference": "event", "workshop": "event",
    "webinar": "event", "подія": "event", "захід": "event", "конференція": "event",
    "воркшоп": "event", "вебінар": "event",
}
INTERNSHIP = re.compile(r"\bintern(?:ship)?\b|стажуван\w*|стажер\w*", re.I)
EVENT = re.compile(r"\b(?:conference|workshop|webinar|hackathon)\b|конференц\w*|воркшоп\w*|вебінар\w*|хакатон\w*", re.I)
HIRING = re.compile(
    r"\b(?:we(?:'re| are) hiring|is hiring|are hiring|job opening|open position|vacancy)\b"
    r"|\b(?:part[- ]time|full[- ]time|paid|entry[- ]level).{0,70}\b(?:position|role)\b"
    r"|шукаю\s+(?:нову?\s+)?колегу|шукаємо\s+(?:нову?\s+)?колегу"
    r"|пошук\s+(?:асистент\w*|аналітик\w*|менеджер\w*|фахів\w*)"
    r"|вакансі\w*|(?:шукаємо|шукає)\s+.{0,70}(?:аналітик|розробник|менеджер|інженер|фахів|дизайнер|analyst|developer|engineer|manager|specialist|designer)",
    re.I,
)
UNSUPPORTED_OPPORTUNITY = re.compile(
    r"\b(?:grant|scholarship|fellowship|training|course|summer school)\b"
    r"|грант\w*|стипенді\w*|навчальн\w*\s+програм\w*|освітн\w*\s+програм\w*|навчанн\w*|курс\w*|інтенсив\w*|\bintensive\b|літн\w*\s+школ\w*",
    re.I,
)
APPLICATION = re.compile(
    r"\b(?:apply|applications?|registration|register|enrol(?:l)?|sign up|deadline)\b"
    r"|подати\s+(?:заявку|документи)|пода(?:ча|вайте|йте)|заявк\w*|реєстрац\w*|реєструй\w*|зареєстр\w*|дедлайн|термін подачі",
    re.I,
)
NONANNOUNCEMENT = re.compile(
    r"\b(?:anyone know|has anyone|looking for a job|thanks for sharing|got the job)\b"
    r"|\b(?:I|we)\s+(?:attended|completed|finished|started|did|had|participated)\b"
    r"|\b(?:internship|conference|webinar|training|course)\s+(?:was|has ended|is over)\b"
    r"|хто знає|(?:в|у) когось|хтось знає|дякую за|шукаю роботу|ми відвідали|я пройш\w*"
    r"|(?:стажування|вебінар|конференція|курс|навчання)\s+(?:був|була|було|заверш\w*)",
    re.I,
)
DIGEST = re.compile(r"\b(?:digest|roundup)\b|дайджест|підбірк\w*\s+можливост", re.I)
MULTIPLE_ROLES = re.compile(r"на\s+такі\s+позиції|маємо\s+вакансії|кілька\s+вакансій|\b(?:several|multiple)\s+(?:positions|roles|openings)\b", re.I)
EVENT_CONTEXT = re.compile(
    r"\b(?:seminar|lecture|meetup|tournament|competition|game jam|integration bee|talk to an expert)\b"
    r"|семінар\w*|лекці\w*|зустріч\w*|турнір\w*|змаган\w*|геймджем\w*|майстер[- ]клас\w*|(?:міськ\w*\s+)?гр[аіу]\b|збірк\w*\s+арт|перегляд\w*\s+матч\w*",
    re.I,
)
INVITATION = re.compile(r"запрош\w*|чекаємо|приєдну\w*|долуч\w*|\b(?:join|invite[sd]?)\b", re.I)
EVENT_TIME = re.compile(
    r"\b\d{1,2}[:.]\d{2}\b|\b\d{1,2}\s+(?:січня|лютого|березня|квітня|травня|червня|липня|серпня|вересня|жовтня|листопада|грудня)\b"
    r"|\b(?:сьогодні|завтра)\b|коли\s*[:?]",
    re.I,
)
ROLE_WORD = re.compile(
    r"\b(?:analyst|researcher|engineer|manager|developer|assistant|scientist|designer|consultant|specialist)\b"
    r"|аналітик\w*|розробник\w*|менеджер\w*|інженер\w*|фахів\w*|дизайнер\w*",
    re.I,
)
GENERIC_ORGANIZATIONS = {
    "we", "our company", "our team", "our client", "the company", "company", "confidential",
    "ми", "наша компанія", "наша команда", "наш клієнт", "компанія",
}
UKRAINIAN_MONTHS = {
    "січня": 1, "лютого": 2, "березня": 3, "квітня": 4, "травня": 5, "червня": 6,
    "липня": 7, "серпня": 8, "вересня": 9, "жовтня": 10, "листопада": 11, "грудня": 12,
}


def _clean_line(line):
    # Rendered text normally has no Markdown markers, but copied Slack text may.
    return re.sub(r"^[^\w@]+", "", line.strip()).rstrip("*_").strip()


def _labeled_fields(lines):
    aliases = {alias: name for name, names in LABELS.items() for alias in names}
    fields, evidence = {}, {}
    for original in lines:
        line = _clean_line(original)
        label, separator, value = line.partition(":")
        name = aliases.get(label.strip(" *_").casefold())
        if separator and name and name not in fields:
            fields[name] = value.strip(" *_")
            evidence[name] = original
    return fields, evidence


def _deadline(value):
    parsed = parse_deadline(value)
    if parsed:
        return parsed
    # A named Ukrainian month still requires a literal year.
    match = re.fullmatch(r"(?:до\s+)?(\d{1,2})\s+(\w+)\s+(\d{4})(?:\s*(?:р\.?|року))?", value.strip(), re.I)
    if match and match[2].casefold() in UKRAINIAN_MONTHS:
        from datetime import date
        try:
            return date(int(match[3]), UKRAINIAN_MONTHS[match[2].casefold()], int(match[1]))
        except ValueError:
            pass
    return None


def _work_mode(value):
    matches = set()
    for mode, pattern in (
        ("remote", r"\b(?:remote|online)\b|дистанційн\w*|віддален\w*|онлайн"),
        ("hybrid", r"\bhybrid\b|гібрид\w*"),
        ("on_site", r"\b(?:on[- ]site|onsite|offline|in[- ]person)\b|офлайн"),
    ):
        if re.search(pattern, value, re.I):
            matches.add(mode)
    return next(iter(matches)) if len(matches) == 1 else ""


def _employment_type(value):
    full = re.search(r"\bfull[- ]time\b|\bповна\s+зайнятість\b", value, re.I)
    part = re.search(r"\bpart[- ]time\b|\b(?:часткова|неповна)\s+зайнятість\b", value, re.I)
    return "full_time" if full and not part else "part_time" if part and not full else ""


def _safe_links(message, text):
    supplied = message.get("links", [])
    supplied = supplied if isinstance(supplied, list) else []
    candidates = supplied + re.findall(r"https?://[^\s<>]+", text)
    links = []
    for value in candidates:
        if not isinstance(value, str):
            continue
        value = value.rstrip(".,;!?)\"]}")
        try:
            parsed = urlsplit(value)
            safe = (
                parsed.scheme in {"http", "https"} and bool(parsed.hostname)
                and parsed.username is None and parsed.password is None
                and not any(char.isspace() or ord(char) < 32 for char in value)
                and "\\" not in value
            )
            parsed.port  # Reject malformed ports without fetching the URL.
        except (ValueError, UnicodeError):
            continue
        if safe and value not in links:
            links.append(value)
    return links


def _announcement_heading(lines, *, application_context):
    """Look past a brief greeting without mining arbitrary later discussion.

    A category mentioned after an introductory line needs application context;
    a clear hiring statement supplies its own announcement context. Unsupported
    opportunity categories always require an application or registration cue.
    """
    for index, line in enumerate(lines[:6]):
        if NONANNOUNCEMENT.search(line):
            continue
        if HIRING.search(line):
            return line, False
        supported = INTERNSHIP.search(line) or EVENT.search(line)
        if supported and (index == 0 or application_context):
            if "?" not in line or application_context:
                return line, False
        if UNSUPPORTED_OPPORTUNITY.search(line) and application_context:
            return line, True
    return "", False


def _survey_request(lines):
    """Research participation requests are not advertised opportunities."""
    first = "\n".join(lines[:6])
    return bool(re.search(
        r"(?:готуємо|проводимо|створили|заповн\w*|пройдіть|пройти).{0,100}(?:анкет\w*|опитуван\w*)"
        r"|(?:анкет\w*|опитуван\w*).{0,100}(?:заповн\w*|пройдіть|пройти|ваша думка)"
        r"|\b(?:fill|complete|take|answer)\b.{0,50}\b(?:survey|questionnaire)\b",
        first, re.I | re.S,
    ))


def _role_title(lines):
    for line in lines[:7]:
        if (
            len(line) < 220 and ROLE_WORD.search(line)
            and not re.search(r"^(?:about|я\s|ми\s|компанія|company:)|\b(?:is|are|працюю|шукаю|hiring)\b", line, re.I)
        ):
            title = re.split(r"\s+на\s+про[єе]кт\b|\.\s+", line, maxsplit=1, flags=re.I)[0].strip()
            return re.sub(r"^(?:vacancy|job opening|вакансія)\s*[:—–-]\s*", "", title, flags=re.I)
    return ""


def parse_message(message):
    """Return one reviewable announcement candidate, or None for unrelated chat.

    The collector supplies a plain-text message with ``ts``, ``source_url`` and
    optional ``links``/``author``. A post announcing several roles is kept as a
    single staged announcement; it is not silently split into invented records.
    """
    if not isinstance(message, dict) or not isinstance(message.get("text"), str):
        raise ValueError("Expected a Slack message containing rendered text.")
    external_id, source_url = validate_message_identity(message.get("ts"), message.get("source_url"))
    return extract_announcement(message, external_id=external_id, source_url=source_url)


def extract_announcement(message, *, external_id, source_url, source_label="Slack",
                         title_hint="", category_hint=""):
    """Extract offline text after the caller validates its real source identity.

    Email callers may supply a title/category supported by the subject or a later
    announcement heading. These are extraction hints, never synthetic source
    fields. The default Slack wrapper retains its existing mapping behavior.
    """
    if not isinstance(message, dict) or not isinstance(message.get("text"), str):
        raise ValueError("Expected an announcement containing rendered text.")
    if category_hint not in {"", "internship", "job", "event"}:
        raise ValueError("Unsupported opportunity category hint.")
    original_text = message["text"]
    text = original_text.strip()
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return None
    fields, evidence = _labeled_fields(lines)
    content_lines = [
        _clean_line(line) for line in lines
        if not re.fullmatch(
            r"(?:@(?:channel|here|everyone)\s*)?(?:hi(?: everyone| all)?|hello(?: everyone| all)?|dear students"
            r"|привіт(?:[!,\s]+(?:усім|друзі))?|(?:усім|всім)\s+привіт|вітаю|вітання|друзі(?:[!,\s]+привіт)?|шановні студенти|дорогі студенти)?[!.,\s]*",
            _clean_line(line), re.I,
        )
    ]
    if _survey_request(content_lines):
        return None
    links = _safe_links(message, text)
    application_context = bool(APPLICATION.search(text) or any(APPLICATION.search(link) for link in links))
    first_context = "\n".join(content_lines[:6])
    is_digest = bool(DIGEST.search("\n".join(content_lines[:2])))
    multiple_roles = bool(MULTIPLE_ROLES.search(first_context)) or len(
        re.findall(r"(?im)^\s*(?:vacancy|вакансія|position|посада)\s*:", text)
    ) > 1
    volunteering = bool(re.search(
        r"шука\w*.{0,70}волонтер\w*|команд\w*.{0,40}волонтер\w*|(?:стань|стати).{0,30}волонтер\w*"
        r"|шука\w*.{0,30}тіммейт\w*|\b(?:looking for|seeking)\s+(?:a\s+)?teammate\b"
        r"|\bvolunteer(?:ing)?\s+opportunities\b|\bvolunteers?\s+wanted\b|відпрацювання\s+гранту",
        "\n".join(content_lines[:3]), re.I,
    ))
    event_announcement = bool(
        EVENT_CONTEXT.search(first_context)
        and (INVITATION.search(text) or application_context)
        and EVENT_TIME.search(text)
        and not HIRING.search(first_context)
        and not volunteering
    )
    detected_heading, unsupported_category = _announcement_heading(
        content_lines, application_context=application_context,
    )
    if is_digest or multiple_roles or volunteering:
        detected_heading = content_lines[0]
    elif event_announcement:
        first_line = content_lines[0]
        descriptive_event = next((line for line in content_lines[:6] if EVENT.search(line) or EVENT_CONTEXT.search(line)), first_line)
        introductory = bool(re.search(r"^(?:привіт|вітаю|hi|hello|маємо|друзі|новини)\b", first_line, re.I))
        detected_heading = descriptive_event if introductory or "?" in first_line else first_line
        unsupported_category = False
    if volunteering:
        unsupported_category = True
    heading = title_hint or detected_heading or (content_lines[0] if content_lines else "")
    kind = TYPE_VALUES.get(fields.get("type", "").casefold(), "") or category_hint
    strong_heading = bool(HIRING.search(heading))
    announcement = bool(
        kind or detected_heading or title_hint
        or (fields.get("title") and fields.get("organization"))
    )
    if not announcement:
        return None

    warnings = [f"{source_label} announcement fields were extracted automatically; review the original message before publication."]
    if is_digest or multiple_roles:
        kind = ""
        unsupported_category = True
        warnings.append("This digest or multi-role announcement contains several opportunities. Split it manually into separate records; organization and category are intentionally blank.")
    elif volunteering:
        kind = ""
        warnings.append("Volunteering, teammate recruitment, or grant-working announcement has no matching opportunity category; review it manually.")
    elif unsupported_category and not kind:
        warnings.append("Grant, scholarship, or training announcement needs a manually selected opportunity category.")
    title = fields.get("title", "")
    if not title:
        title = re.sub(r"^(?:vacancy|job opening|вакансія)\s*[:—–-]\s*", "", heading, flags=re.I)
        hiring_title = re.match(
            r"(?:we(?:'re| are) hiring|.{1,120}?\s+(?:is|are) hiring)\s*:?[ \t]*(?:an?\s+)?(.+)",
            title, re.I,
        )
        if hiring_title:
            title = hiring_title[1].strip()
        if strong_heading and not multiple_roles and not title_hint:
            title = _role_title(content_lines) or title
        evidence["title"] = heading
        warnings.append("Title taken from the announcement heading; verify the exact role or event name.")
    if len(title) > 200:
        title = title[:197].rstrip() + "..."
        warnings.append("Announcement heading exceeded 200 characters; the full text is retained in the description.")

    type_context = " ".join((title, heading))
    if not kind and not unsupported_category:
        if event_announcement or (EVENT.search(type_context) and not strong_heading):
            kind = "event"
        elif INTERNSHIP.search(type_context):
            kind = "internship"
        elif strong_heading:
            kind = "job"
    evidence["type"] = evidence.get("type") or (heading if kind else "")
    if fields.get("type") and not TYPE_VALUES.get(fields["type"].casefold()):
        warnings.append("Source type is not an opportunity category; check opportunity type separately from working hours.")

    organization = fields.get("organization", "")
    if not organization:
        for line in content_lines[:8]:
            introduction_line = re.sub(r"^About\s+[A-Z][A-Z0-9&.-]{1,15}\s+(?=[A-Z])", "", line)
            organization, introduction = explicit_organization_introduction(introduction_line)
            if organization:
                evidence["organization"] = introduction
                break
            match = re.match(r"([^:\n]{2,120}?)\s+is\s+(?:hosting|organizing)\b", line, re.I)
            if match:
                organization, evidence["organization"] = match[1], line
                break
            match = re.search(r"(?:зараз\s+)?працюю\s+(?:в|у)\s+([A-Za-z][\w.-]*)\b", line, re.I)
            if match:
                organization, evidence["organization"] = match[1], line
                break
            match = re.search(r"(?:що\s+)?([A-ZА-ЯІЇЄҐ][\w &'’.-]{1,100}?)\s+продовжує\s+пошук\b", line)
            if match:
                organization, evidence["organization"] = match[1], line
                break
            named_organization_line = re.sub(
                r"^\d{1,2}\s+(?:січня|лютого|березня|квітня|травня|червня|липня|серпня|вересня|жовтня|листопада|грудня)\s+",
                "", line, flags=re.I,
            )
            match = re.match(r"(?:компанія\s+)?([^:\n]{2,120}?)\s+(?:шукає|запрошує)\s+", named_organization_line, re.I)
            if match:
                organization, evidence["organization"] = match[1], line
                break
    if organization.casefold() in GENERIC_ORGANIZATIONS:
        organization = ""
    if len(organization) > 200:
        organization = ""
        warnings.append("Organization text is too long; enter the named employer or organizer during review.")

    arrangement = "\n".join(fields.get(name, "") for name in ("work_mode", "employment_type", "type", "location"))
    if strong_heading:
        arrangement += "\n" + heading + "\n" + title
    work_mode, employment_type = _work_mode(arrangement), _employment_type(arrangement)
    location = fields.get("location", "")
    if location and (TYPE_VALUES.get(location.casefold()) or _employment_type(location)):
        location = ""
        warnings.append("Location contained a category or working hours and was left blank.")
    elif location and _work_mode(location):
        # Keep only a separately stated city/country next to a work arrangement.
        pieces = re.split(r"\s*(?:/|\||;|\(|\))\s*", location)
        geographic = [part.strip() for part in pieces if part.strip() and not _work_mode(part)]
        location = "; ".join(geographic)
    if not location:
        location, location_evidence, described_mode, eligibility = geographic_location(text)
        if location:
            evidence["location"] = location_evidence
        work_mode = work_mode or described_mode
        if eligibility:
            warnings.append("Location is an applicant location requirement; no office location is confirmed.")
    if len(location) > 200:
        location = ""
        warnings.append("Location needs manual review; the full source wording is retained.")

    deadline_text = fields.get("deadline", "")
    deadline = _deadline(deadline_text) if deadline_text else None
    if deadline_text and deadline is None:
        warnings.append(f"Deadline needs review (an explicit valid year is required): {deadline_text}")
    for name, value in (("organization", organization), ("type", kind)):
        if not value:
            warnings.append(f"Missing {name}; complete it before creating a draft.")
    if multiple_roles:
        warnings.append("This message may contain several roles; review it as one announcement before publication.")
    if is_digest or multiple_roles:
        title = content_lines[0][:200]
        organization = kind = location = work_mode = employment_type = ""
        deadline = None
        evidence = {"title": content_lines[0]}
    if message.get("truncated"):
        warnings.append("The saved Slack message was marked as truncated; read the original posting before publication.")

    event_fields = {}
    if kind == "event":
        event_type = classify_event_type(title, text)
        event_fields["event_type"] = event_type
        if event_type:
            evidence["event_type"] = title if classify_event_type(title) else "\n".join(lines[:5])
        else:
            evidence["event_type"] = ""
            warnings.append("Event format is unknown or mixed; select its format during review if applicable.")

    return {
        "external_id": external_id, "source_url": source_url,
        "title": title, "organization": organization, "type": kind,
        "employment_type": employment_type, "work_mode": work_mode,
        "description": text, "location": location, "deadline": deadline,
        **event_fields,
        "raw_data": {
            "text": original_text,
            "author": message.get("author") if isinstance(message.get("author"), str) else "",
            "links": links, "fields": fields,
            "field_evidence": evidence, "warnings": warnings, "parser_version": 2,
        },
    }
