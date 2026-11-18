"""Configured Gmail message identities, validated without accessing a mailbox."""
from dataclasses import dataclass
import hashlib
import re

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import validate_email


GMAIL_HOST = "mail.google.com"
INBOX_URL = f"https://{GMAIL_HOST}/mail/u/0/#inbox"
MESSAGE_ID = re.compile(r"[0-9a-f]{16}\Z")


@dataclass(frozen=True)
class GmailAccount:
    email: str
    source_key: str


def get_account():
    """Require an explicitly configured email; never infer it from browser state."""
    configured = getattr(settings, "GMAIL_ACCOUNT_EMAIL", "")
    if not isinstance(configured, str) or not configured.strip():
        raise ValueError("Set GMAIL_ACCOUNT_EMAIL to the Gmail account authorized for import.")
    email = configured.strip().casefold()
    try:
        validate_email(email)
    except ValidationError as exc:
        raise ValueError("GMAIL_ACCOUNT_EMAIL must be a valid email address.") from exc
    digest = hashlib.sha256(email.encode("utf-8")).hexdigest()[:40]
    return GmailAccount(email=email, source_key=f"gmail-{digest}")


def message_permalink(external_id):
    """Use a legacy message ID without exposing the configured email in its URL."""
    if not isinstance(external_id, str) or not MESSAGE_ID.fullmatch(external_id):
        raise ValueError("Gmail message ID must contain exactly 16 lowercase hexadecimal characters.")
    get_account()
    return f"{INBOX_URL}/{external_id}"


def validate_message_identity(external_id, url, *, source_key=None):
    """Validate the exact canonical URL and its configured-mailbox source key."""
    account = get_account()
    if source_key is not None and source_key != account.source_key:
        raise ValueError("Gmail source key must match the explicitly configured account.")
    expected_url = message_permalink(external_id)
    if not isinstance(url, str) or url != expected_url:
        raise ValueError("Gmail source URL must be the canonical inbox permalink for its message ID.")
    return external_id, expected_url
