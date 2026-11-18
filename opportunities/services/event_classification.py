"""Conservative event-format hints from titles and announcement introductions.

Call this only after establishing that an opportunity is an event. These rules
do not infer the broad opportunity category and do not classify ordinary training
as an event. Unknown or mixed formats are deliberately left blank for review.
"""
import re
import unicodedata


PATTERNS = {
    "seminar": re.compile(r"\b(?:seminars?|webinars?)\b|\b(?:семінар|вебінар)\w*", re.I),
    "lecture": re.compile(r"\blectures?\b|\bлекці\w*", re.I),
    "workshop": re.compile(r"\bworkshops?\b|\bворкшоп\w*|\bмайстер[- ]клас\w*", re.I),
    "conference": re.compile(r"\bconferences?\b|\bконференці\w*", re.I),
    "competition": re.compile(
        r"\b(?:competitions?(?!\s+(?:law|policy|economics)\b)|contests?|tournaments?|hackathons?|game[- ]jams?)\b"
        r"|\b(?:конкурс|турнір|змаган|позмага|хакатон|геймджем)\w*", re.I,
    ),
}
ANNOUNCEMENT = re.compile(
    r"\b(?:join|invites?|invited|host(?:s|ing)?|hold(?:s|ing)?|held|deliver(?:s|ing)?|give(?:s|n)?)\b"
    r"|запрош\w*|відбуд\w*|провед\w*|долуч\w*|реєстр\w*|зареєстр\w*",
    re.I,
)
ROLE_ANNOUNCEMENT = re.compile(
    r"\b(?:vacancy|job opening|is hiring|are hiring|we(?:'re| are) hiring)\b|вакансі\w*|шукаємо\s+(?:фахів|менеджер|аналітик|розробник)"
    r"|^(?:(?:paid|unpaid)\s+)?internship\s+(?:opportunity|opening|position|role)\s*[:-]"
    r"|^стажування\s+на\s+(?:позиції|посаду)\b",
    re.I,
)
VOLUNTEER_ROLE_TITLE = re.compile(
    r"^(?:volunteers?\s+(?:at|for|with|wanted|needed)\b|volunteers?\s*[:-]|volunteers?$|volunteering\s+opportunit\w*\b)"
    r"|^(?:we(?:'re| are)?\s+)?(?:seeking|recruiting|looking for|need)\s+volunteers?\b"
    r"|^шука\w*.{0,40}волонтер\w*|^волонтер(?:и|ів|ом|ка|ки)?\s+(?:на|для|у|в)\b|^волонтер(?:и|ка|ки)?$",
    re.I,
)
INCIDENTAL = re.compile(
    r"\b(?:benefits?|training budget|attend(?:ing)? conferences|you (?:can|will) attend|our employees|our staff)\b"
    r"|переваги роботи|бюджет на навчання|можливість відвідувати|доступ до|оплачуємо участь|для працівників",
    re.I,
)
FORMAT_LABEL = re.compile(r"^(?:event (?:type|format)|format|тип події|формат(?: події)?)\s*:\s*", re.I)


def _normalized(value):
    if not isinstance(value, str):
        return ""
    return unicodedata.normalize("NFC", value).replace("‑", "-").replace("–", "-").replace("—", "-").strip()


def _matches(text):
    return {kind: match for kind, pattern in PATTERNS.items() if (match := pattern.search(text))}


def classify_event_type(title, description=""):
    """Return one supported event format, or ``''`` when unclear or mixed.

    The title takes precedence. Otherwise only the first five nonempty lines of
    an announcement can supply an explicit format label, invitation, or heading.
    Webinar is grouped with seminar; online/on-site remains a separate work mode.
    """
    title = _normalized(title)
    if ROLE_ANNOUNCEMENT.search(title) or VOLUNTEER_ROLE_TITLE.search(title):
        return ""
    title_matches = _matches(title)
    if title_matches:
        return next(iter(title_matches)) if len(title_matches) == 1 else ""

    description = _normalized(description)
    introductory_lines = [line.strip() for line in description.splitlines() if line.strip()][:5]
    formats = set()
    for line in introductory_lines:
        if ROLE_ANNOUNCEMENT.search(line) or INCIDENTAL.search(line):
            continue
        matches = _matches(line)
        if not matches:
            continue
        first_match = min(matches.values(), key=lambda match: match.start())
        heading_prefix = line[:first_match.start()]
        # A short noun phrase such as "Research seminar" is useful evidence;
        # arbitrary prose later in a description cannot supply the format.
        heading_like = len(line) <= 180 and len(heading_prefix.split()) <= 3 and not re.search(
            r"[.!?;]|\b(?:about|benefits|includes|experience|attended|previous|past)\b|про досвід|раніше|минул", heading_prefix, re.I,
        )
        if FORMAT_LABEL.match(line) or ANNOUNCEMENT.search(line) or heading_like:
            formats.update(matches)
    return next(iter(formats)) if len(formats) == 1 else ""
