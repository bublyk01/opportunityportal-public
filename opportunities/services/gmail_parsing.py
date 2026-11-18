"""Offline opportunity extraction from fully rendered, authorized email bodies.

The full source stays in staff-only snapshots. Public descriptions omit email
envelopes, quoted replies and recognizable signatures; no links are fetched.
"""
import re
import unicodedata
from datetime import date, datetime

from .event_classification import classify_event_type
from .gmail_source import get_account, validate_message_identity
from .notion_public import explicit_organization_introduction
from .slack_parsing import (
    DIGEST, EVENT_CONTEXT, HIRING, INTERNSHIP,
    NONANNOUNCEMENT, TYPE_VALUES,
    _clean_line, _deadline, _labeled_fields, _survey_request,
    extract_announcement,
)


OPPORTUNITY_WORDS = re.compile(
    r"\b(?:opportunit\w*|career\w*|jobs?|vacanc\w*|hiring|intern\w*|fellowship\w*|scholarship\w*|grants?"
    r"|seminars?|lectures?|workshops?|conferences?|webinars?|hackathons?|competitions?|contests?|tournaments?"
    r"|invitations?|events?|meetups?|summer schools?|training|courses?|volunteer\w*|applications? open|call for)\b"
    r"|можливост\w*|кар[’ʼ']?єр\w*|вакансі\w*|стажуван\w*|стажер\w*|грант\w*|стипенді\w*|запрош\w*"
    r"|семінар\w*|лекці\w*|воркшоп\w*|майстер[- ]клас\w*|конференц\w*|вебінар\w*|хакатон\w*|конкурс\w*|турнір\w*"
    r"|дайджест|волонтер\w*|навчанн\w*|курс\w*|освітн\w*\s+програм\w*|пропозиці\w*|поді\w*|заход\w*|\bробот(?:а|и|у|ою|і)\b",
    re.I,
)
TRANSACTIONAL_SUBJECT = re.compile(
    r"\b(?:your application|application (?:received|confirmation|status)|thank you for applying"
    r"|interview (?:invitation|scheduled|confirmation)|registration (?:confirmed|confirmation)|submission received)\b"
    r"|ваш\w*\s+заявк\w*|заявк\w*\s+(?:отримано|прийнято)|підтвердження реєстрації|реєстрацію підтверджено"
    r"|^(?:accepted|declined|tentative|прийнято|відхилено)\s*:",
    re.I,
)
CLIPPED = re.compile(r"\[\s*message clipped\s*\]|view entire message|повідомлення обрізано|переглянути повне повідомлення", re.I)
REPLY_BOUNDARY = re.compile(
    r"^On\s+.{5,250}\s+wrote:\s*$|^-{2,}\s*Original Message\s*-{2,}$"
    r"|^.{5,200}\s+(?:писав|писала|написав|написала)\s*:\s*$",
    re.I,
)
FORWARD_BOUNDARY = re.compile(r"forwarded message|begin forwarded message|переслане повідомлення|пересланий лист", re.I)
HEADER = re.compile(r"^(?:to|cc|bcc|delivered-to|from|sent|date|subject|кому|копія|прихована копія|від|дата|тема)\s*:\s*", re.I)
SALUTATION = re.compile(
    r"^(?:dear(?:\s+[\w’ʼ'-]+){1,4}|(?:hi|hello)[,\s]+(?:[\w’ʼ'-]+\s*){1,3}|шановн\w*(?:\s+[\w’ʼ'-]+){1,4}"
    r"|(?:привіт|добрий день|вітаю)[,\s]+[А-ЯІЇЄҐ][\w’ʼ'-]{1,50})[!,\s]*$", re.I,
)
SIGNATURE = re.compile(r"^(?:best(?: regards| wishes)?|kind regards|regards|sincerely|з повагою|щиро ваш[аі]?)[!,\s]*$", re.I)
FOOTER = re.compile(
    r"^(?:unsubscribe\b|manage your (?:subscription|preferences)|update preferences|you (?:are receiving|received) this"
    r"|this (?:email|message) (?:was|is) sent to|click here to leave mailing list|відписатися|керувати підпискою|ви отримали цей лист)", re.I,
)
MEETING_ARTIFACT = re.compile(
    r"\b(?:read (?:the )?meeting report|meeting (?:report|recap|notes|summary|recording)|transcript|recording(?:s)?(?: available)?|watch (?:the )?replay)\b"
    r"|\b(?:pre-read|review of the previous meeting|meeting summary|summary for)\b"
    r"|звіт (?:про|із|з) зустріч|запис (?:зустрічі|лекції|семінару|вебінару)|стенограм\w*|матеріали\s*:\s*.*(?:лекці|семінар|вебінар)",
    re.I,
)
PAST_EVENT = re.compile(
    r"\b(?:I|we)\s+(?:recently\s+)?(?:returned|attended|spoke|presented|participated|enjoyed|visited)\b"
    r"|\b(?:returned from|looking back|recap of|was held|were held|took place|last (?:week|month|year))\b"
    r"|(?:щойно|нещодавно)\s+(?:поверн\w*|відвіда\w*)|відбувся|відбулася|відбулися|минулого тижня",
    re.I,
)
ACTIVE_EVENT = re.compile(
    r"\b(?:join (?:us|our|the)|you(?:'re| are) invited|we invite|I invite|invitation to|register (?:now|here|for|at)|registration (?:is )?(?:now )?(?:open|available)|upcoming|save the date|RSVP|will (?:take place|be held|host))\b"
    r"|запрошуємо|запрошую|запрошує\b|приєднуйте\w*|долучайте\w*|зареєструй\w*|реєструй\w*"
    r"|реєстраці\w*\s+(?:відкрита|триває|обов['’ʼ]язкова|за посиланням)|відбудеться|пройде\b",
    re.I,
)
UPCOMING = re.compile(r"\b(?:upcoming|tomorrow|next (?:week|month|monday|tuesday|wednesday|thursday|friday|saturday|sunday))\b|завтра|наступн\w*", re.I)
GMAIL_EVENT_WORDS = re.compile(r"\b(?:fair|festival)\b|ярмар\w*|фестивал\w*", re.I)
GMAIL_UNSUPPORTED = re.compile(
    r"\b(?:grant|scholarship|fellowship|training|course|bootcamp|intensive|summer school)\b"
    r"|грант\w*|стипенді\w*|освітн\w*\s+програм\w*|навчальн\w*\s+програм\w*|курс\w*|інтенсив\w*",
    re.I,
)
PROGRAM_ACTION = re.compile(
    r"\b(?:apply|applications? (?:are )?(?:still )?open|register|registration|sign up|join (?:the|our) (?:program|project))\b"
    r"|подати\s+заявку|заявк\w*\s+(?:прийма|відкрит)|реєстрац\w*|зареєстр\w*|реєструй\w*",
    re.I,
)
PROJECT_PARTICIPATION = re.compile(
    r"\b(?:join|invitation to join)\b.{0,70}\b(?:research project|monitoring project)\b"
    r"|запрош\w*.{0,90}(?:проєкт|проект|дослідження|моніторинг)|долуч\w*.{0,40}(?:проєкт|проект|дослідження)",
    re.I,
)


def _readable_text(value):
    # Email preheaders contain invisible padding and nonbreaking spaces. Keep
    # these exact original bytes in raw_data, not in public titles/descriptions.
    value = "".join(character for character in value if unicodedata.category(character) != "Cf")
    return re.sub(r"[^\S\n]+", " ", value).strip()


def might_contain_opportunity(subject, snippet=""):
    """A broad triage hint, never a substitute for reading the complete body."""
    return bool(OPPORTUNITY_WORDS.search("\n".join(
        value for value in (subject, snippet) if isinstance(value, str)
    )))


def _subject(value):
    value = re.sub(r"^(?:(?:re|fw|fwd|відп|пересл)\s*:\s*)+", "", value.strip(), flags=re.I).strip()
    calendar = re.match(r"^(?:updated invitation|invitation|запрошення)\s*:\s*(.+?)\s+@\s+(.+)$", value, re.I)
    if calendar and re.match(
        r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\b|\d{4}-\d{2}-\d{2}|(?:понеділок|вівторок|середа|четвер|п['’ʼ]ятниця|субота|неділя)\b",
        calendar[2], re.I,
    ):
        return calendar[1].strip()
    return value


def _credible_inferred_organization(name, evidence):
    """Do not turn a conversational Ukrainian subject into an employer name."""
    if not name or re.search(r"[?]|\b(?:можливо|яка|який|у вас|ви|ми|there|you|our|your)\b", name, re.I):
        return False
    inferred, _ = explicit_organization_introduction(evidence)
    if inferred == name:
        return True
    entity_word = re.search(
        r"\b(?:company|university|academy|institute|bank|foundation|club|association)\b"
        r"|\b(?:компані|університет|академі|інститут|банк|фонд|клуб|асоціац|центр|школ)\w*|^(?:ГО|ТОВ|АТ)\b",
        name, re.I,
    )
    actor = re.match(
        rf"^(?:компанія\s+)?{re.escape(name)}\s+(?:шукає|запрошує|організовує|проводить)\b",
        evidence, re.I,
    )
    return bool(entity_word and actor)


def _public_body(text, *, forwarded_email=False):
    lines = text.splitlines()
    kept, forwarded_subjects = [], []
    trimmed = False
    forwarded = False
    skip_forwarding_signature = False

    def forwarding_boundary(line):
        return FORWARD_BOUNDARY.search(line) or (
            forwarded_email and re.fullmatch(r"-{2,}\s*Original Message\s*-{2,}", line.strip(), re.I)
        )

    for index, original in enumerate(lines):
        line = original.strip()
        if forwarding_boundary(line):
            forwarded = True
            skip_forwarding_signature = False
            trimmed = True
            continue
        if skip_forwarding_signature:
            continue
        if REPLY_BOUNDARY.match(line):
            trimmed = True
            break
        if line.startswith(">"):
            trimmed = True
            continue
        header = HEADER.match(line)
        if header:
            if not forwarded and re.match(r"^(?:date|дата)\s*:", line, re.I):
                kept.append(original)
                continue
            if forwarded and re.match(r"^(?:subject|тема)\s*:", line, re.I):
                forwarded_subjects.append(line[header.end():].strip())
            trimmed = True
            continue
        if forwarded and line:
            forwarded = False
        if SALUTATION.match(line):
            trimmed = True
            continue
        following = [value.strip() for value in lines[index + 1:] if value.strip()][:6]
        signature_divider = bool(re.fullmatch(r"-{2,}", line)) and any(
            SIGNATURE.fullmatch(value) or re.search(
                r"\b(?:Ph\.?D\.?|Rector|Program Manager|Director|Tel\.?|Mob\.?|Phone)\b|^www\.", value, re.I,
            ) for value in following
        )
        if kept and (SIGNATURE.fullmatch(line) or signature_divider):
            trimmed = True
            # A forwarding note may precede the actual announced opportunity.
            if any(forwarding_boundary(later) for later in lines[index + 1:]):
                skip_forwarding_signature = True
                continue
            break
        if index >= len(lines) // 2 and FOOTER.match(line):
            trimmed = True
            break
        kept.append(original)
    return "\n".join(kept).strip(), forwarded_subjects, trimmed


def _reference_date(value):
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _has_upcoming_date(text, reference):
    if reference is None:
        return False
    patterns = (
        r"\b\d{4}-\d{2}-\d{2}\b", r"\b\d{1,2}\.\d{1,2}\.\d{4}\b",
        r"\b[A-Za-z]+\s+\d{1,2},\s+\d{4}\b", r"\b\d{1,2}\s+[A-Za-zА-Яа-яІіЇїЄєҐґ]+\s+\d{4}\b",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            parsed = _deadline(match[0])
            if isinstance(parsed, date) and parsed >= reference:
                return True
    return False


def _event_signal(line, body, *, reference=None):
    if MEETING_ARTIFACT.search(line) or PAST_EVENT.search(line):
        return False
    body_lines = [value.strip() for value in body.splitlines() if value.strip()]
    try:
        index = body_lines.index(line)
    except ValueError:
        nearby = body_lines[:12]
    else:
        nearby = body_lines[max(0, index - 1):index + 8]
    context = "\n".join(nearby)
    clear_invitation = any(
        ACTIVE_EVENT.search(value) and not MEETING_ARTIFACT.search(value) and not PAST_EVENT.search(value)
        for value in [line, *nearby]
    )
    if MEETING_ARTIFACT.search(context) and not any(
        ACTIVE_EVENT.search(value) and UPCOMING.search(value)
        and (classify_event_type(value) or EVENT_CONTEXT.search(value))
        for value in [line, *nearby]
    ):
        return False
    return clear_invitation or bool(UPCOMING.search(context)) or _has_upcoming_date(context, reference)


def _category(line, body, *, reference=None):
    if NONANNOUNCEMENT.search(line) or PAST_EVENT.search(line) or MEETING_ARTIFACT.search(line):
        return None
    if HIRING.search(line):
        return "job"
    if re.match(r"^(?:job(?: opportunity)?|position|role|посада)\s*[:—–-]\s*\S", line, re.I):
        return "job"
    if classify_event_type(line):
        return "event" if _event_signal(line, body, reference=reference) else None
    if INTERNSHIP.search(line):
        return "internship"
    if EVENT_CONTEXT.search(line) or GMAIL_EVENT_WORDS.search(line):
        return "event" if _event_signal(line, body, reference=reference) else None
    if PROJECT_PARTICIPATION.search(line):
        return ""
    if GMAIL_UNSUPPORTED.search(line) and PROGRAM_ACTION.search(body):
        return ""
    return None


def _digest(subject, body, headings, links):
    if DIGEST.search(subject) or DIGEST.search("\n".join(body.splitlines()[:2])):
        return True
    if re.search(r"(?i)newsletter|weekly opportunities|щотижнев\w*\s+можливост", subject) and len(headings) > 1:
        return True
    if re.search(r"(?i)opportunities for students|student opportunities|можливості для студентів", subject) and len(headings) > 1:
        return True
    if re.search(r"(?i)series of events|сері\w*\s+поді\w*", subject + "\n" + body) and re.search(
        r"(?i)separate registration|окрема реєстрація", body,
    ):
        return True
    labeled_titles = re.findall(r"(?im)^\s*(?:vacancy|вакансія|title|position|посада)\s*:", body)
    if len(labeled_titles) > 1:
        return True
    application_links = [link for link in links if isinstance(link, str) and re.search(r"apply|application|careers|register", link, re.I)]
    return len(application_links) > 1 and len({heading for heading, _ in headings}) > 1


def parse_email(message):
    """Return one evidence-backed candidate, or None for unrelated email.

    ``complete`` must explicitly be true. Attachments remain unparsed and are
    recorded as a warning; neither a snippet nor a clipped body is accepted as
    the complete source. Multiple opportunities remain one staged snapshot.
    """
    if not isinstance(message, dict) or not isinstance(message.get("text"), str):
        raise ValueError("Expected an email containing its rendered full body.")
    external_id, source_url = validate_message_identity(message.get("external_id"), message.get("source_url"))
    if message.get("complete") is not True or CLIPPED.search(message["text"]):
        raise ValueError("The email body is incomplete or clipped; read its complete message before importing.")
    subject = message.get("subject", "")
    if not isinstance(subject, str):
        raise ValueError("Email subject must be text.")
    attachments = message.get("attachments", [])
    if not isinstance(attachments, list) or any(not isinstance(value, str) for value in attachments):
        raise ValueError("Email attachments must be a list of rendered filenames.")
    if TRANSACTIONAL_SUBJECT.search(subject) or re.search(r"\b(?:cancelled|canceled|cancellation|postponed)\b|скасован\w*|перенесен\w*", subject, re.I):
        return None
    if re.search(r"\[moodle-reminder\]|\b(?:assignment reminder|home task|homework due)\b", subject, re.I):
        return None

    body, forwarded_subjects, trimmed = _public_body(
        message["text"], forwarded_email=bool(re.match(r"^(?:fw|fwd|пересл)\s*:", subject.strip(), re.I)),
    )
    public_body = re.sub(re.escape(get_account().email), "[recipient]", _readable_text(body), flags=re.I)
    trimmed = trimmed or public_body != body
    body = public_body
    normalized_subject = _readable_text(_subject(subject))
    body_lines = [_clean_line(line) for line in body.splitlines() if line.strip()]
    if _survey_request(body_lines):
        return None
    reference = _reference_date(message.get("message_date"))
    headings = [(line, kind) for line in body_lines if (kind := _category(line, body, reference=reference)) is not None]
    if MEETING_ARTIFACT.search(normalized_subject):
        # An automated report's "upcoming meeting" boilerplate is not an open
        # event. Preserve only a separate, explicit invitation to a named format.
        headings = [
            (line, kind) for line, kind in headings
            if kind == "event" and classify_event_type(line)
            and ACTIVE_EVENT.search(line) and (UPCOMING.search(line) or PROGRAM_ACTION.search(line))
        ]
    fields, _ = _labeled_fields(body_lines)
    explicit_kind = TYPE_VALUES.get(fields.get("type", "").casefold(), "")
    is_digest = _digest(normalized_subject, body, headings, message.get("links", []) or [])
    if explicit_kind == "event" and not is_digest and not _event_signal(
        fields.get("title") or normalized_subject, body, reference=reference,
    ):
        return None
    subject_kind = _category(normalized_subject, body, reference=reference)
    if re.match(r"^re\s*:", subject, re.I) and trimmed and not headings and not explicit_kind:
        return None
    if not body and not attachments:
        if might_contain_opportunity(subject):
            raise ValueError("The email has no readable announcement body.")
        return None

    title_hint, category_hint = "", ""
    if subject_kind is not None:
        title_hint, category_hint = normalized_subject, subject_kind
    elif forwarded_subjects and (forwarded_kind := _category(forwarded_subjects[0], body, reference=reference)) is not None:
        title_hint, category_hint = _subject(forwarded_subjects[0]), forwarded_kind
    elif headings:
        title_hint, category_hint = headings[0]
    elif fields.get("title") and (fields.get("organization") or explicit_kind):
        title_hint, category_hint = fields["title"], explicit_kind
    else:
        return None

    announcement_body = body
    if not is_digest and subject_kind is None and title_hint in body_lines:
        source_lines = body.splitlines()
        start = next((index for index, line in enumerate(source_lines) if _clean_line(line) == title_hint), 0)
        if start > 0:
            announcement_body = "\n".join(source_lines[start:]).strip()
            trimmed = True

    candidate = extract_announcement(
        {"text": announcement_body or normalized_subject, "links": message.get("links", [])},
        external_id=external_id, source_url=source_url, source_label="Email",
        title_hint=title_hint, category_hint=category_hint,
    )
    if candidate is None:
        return None
    raw_data = candidate["raw_data"]
    warnings = raw_data["warnings"]
    candidate["description"] = announcement_body
    announcement_lines = [_clean_line(line) for line in announcement_body.splitlines() if line.strip()]
    inferred_organization = candidate["organization"]
    organization_evidence = raw_data["field_evidence"].get("organization", "")
    if inferred_organization and not raw_data.get("fields", {}).get("organization") and not _credible_inferred_organization(
        inferred_organization, organization_evidence,
    ):
        candidate["organization"] = ""
        raw_data["field_evidence"]["organization"] = ""
        warnings.append("The inferred organization was not a clearly named employer or organizer; verify it manually.")
    if not raw_data.get("fields", {}).get("organization") and re.match(r"^(?:from|on|between|during)\b|^\d", candidate["organization"], re.I):
        candidate["organization"] = ""
        warnings.append("Organization introduction included a date or narrative prefix; verify the organizer manually.")
    if not candidate["organization"]:
        for line in announcement_lines:
            organization, evidence = explicit_organization_introduction(line)
            if organization and len(organization) <= 200 and not re.match(r"^(?:from|on|between|during)\b|^\d", organization, re.I):
                candidate["organization"] = organization
                raw_data["field_evidence"]["organization"] = evidence
                warnings[:] = [warning for warning in warnings if not warning.startswith("Missing organization;")]
                break
            explicit = re.match(r"(?:organizers|organisers|організатори)\s*:\s*(.+)$", line, re.I)
            if explicit and len(explicit[1]) <= 200:
                candidate["organization"] = explicit[1]
                raw_data["field_evidence"]["organization"] = line
                break
    # Email newsletters often use an em dash rather than a property-row colon.
    if candidate["deadline"] is None:
        for line in announcement_lines:
            match = re.match(r"(?:deadline|application deadline|registration deadline|дедлайн(?: реєстрації)?|термін подачі)\s*[:—–-]\s*(.+)$", line, re.I)
            if match:
                candidate["deadline"] = _deadline(match[1].strip())
                raw_data["field_evidence"]["deadline"] = line
                if candidate["deadline"] is None:
                    warnings.append(f"Deadline needs review (an explicit valid year is required): {match[1]}")
                break
    if is_digest:
        candidate.update({
            "title": (normalized_subject or title_hint)[:200],
            "organization": "", "type": "", "event_type": "", "employment_type": "",
            "work_mode": "", "location": "", "deadline": None,
        })
        raw_data["field_evidence"] = {"title": normalized_subject or title_hint}
        warnings.append("This email contains several opportunities. Split the digest manually into separate records; organization and category are intentionally blank.")
    candidate["title"] = re.sub(
        re.escape(get_account().email), "[recipient]", _readable_text(candidate["title"]), flags=re.I,
    )
    if trimmed:
        warnings.append("Quoted replies, email headers, or a recognizable signature were omitted from the public description; the full source is retained for staff review.")
    if attachments:
        warnings.append("Attachments were not parsed; review the original email and its attachments before publication.")
    raw_data.update({
        "text": message["text"], "subject": subject,
        "sender": message.get("sender") if isinstance(message.get("sender"), str) else "",
        "message_date": message.get("message_date", ""),
        "displayed_date": message.get("displayed_date", ""),
        "attachments": attachments, "complete": True, "parser_version": 1,
    })
    # Preserve explicitly supplied legacy metadata, but never infer delivery time
    # from Gmail's displayed Date header.
    if "received_at" in message:
        raw_data["received_at"] = message["received_at"]
    return candidate
