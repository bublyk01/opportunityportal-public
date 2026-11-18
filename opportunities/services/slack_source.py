"""Configured Slack channel identities, validated without network access."""
from dataclasses import dataclass
import re
from urllib.parse import urlsplit

from django.conf import settings


# Reserved placeholders support offline tests without identifying a real source.
# Browser access remains disabled until an approved workspace is configured.
WORKSPACE_HOST = "workspace.slack.example.invalid"
CHANNEL_ID = "C1111111111"
SOURCE_KEY = f"slack-example-workspace-{CHANNEL_ID}"
CHANNEL_URL = f"https://{WORKSPACE_HOST}/archives/{CHANNEL_ID}"
MESSAGE_TIMESTAMP = re.compile(r"[0-9]{10}\.[0-9]{6}\Z")
CHANNEL_PATH = re.compile(r"/archives/(C[A-Z0-9]{8,31})\Z")
MESSAGE_PATH = re.compile(r"/archives/(C[A-Z0-9]{8,31})/p[0-9]{16}\Z")


@dataclass(frozen=True)
class SlackChannel:
    channel_id: str
    url: str
    source_key: str


def _validated_url(value):
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("Slack source URL must be nonempty text without surrounding whitespace.")
    if any(character.isspace() or ord(character) < 32 for character in value):
        raise ValueError("Slack source URL cannot contain whitespace or control characters.")
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or parsed.netloc != WORKSPACE_HOST
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port is not None
            or parsed.query
            or parsed.fragment
            or "?" in value
            or "#" in value
        ):
            raise ValueError("Use the configured Slack workspace over HTTPS, without URL parameters.")
    except (ValueError, UnicodeError) as exc:
        raise ValueError("Invalid Slack source URL.") from exc
    return parsed


def _channel_from_url(value):
    parsed = _validated_url(value)
    match = CHANNEL_PATH.fullmatch(parsed.path)
    if not match:
        raise ValueError("Slack channel URL must identify a channel in the configured workspace.")
    channel_id = match[1]
    return SlackChannel(
        channel_id=channel_id,
        url=f"https://{WORKSPACE_HOST}/archives/{channel_id}",
        source_key=f"slack-example-workspace-{channel_id}",
    )


def get_channels():
    """Return the ordered allowlist; only settings can authorize another channel."""
    configured = getattr(settings, "SLACK_CHANNEL_URLS", (CHANNEL_URL,))
    if isinstance(configured, str):
        configured = tuple(value.strip() for value in configured.split(",") if value.strip())
    if not isinstance(configured, (list, tuple)) or not configured:
        raise ValueError("SLACK_CHANNEL_URLS must configure at least one approved Slack channel URL.")
    channels = []
    for url in configured:
        channel = _channel_from_url(url)
        if channel not in channels:
            channels.append(channel)
    return tuple(channels)


def get_channel(value=None):
    """Resolve a configured channel URL, ID, source key, or validated identity."""
    channels = get_channels()
    if value is None:
        return channels[0]
    for channel in channels:
        if isinstance(value, SlackChannel):
            if value == channel:
                return channel
        elif isinstance(value, str) and value in (channel.url, channel.channel_id, channel.source_key):
            return channel
    raise ValueError("Only an explicitly configured Slack channel may be selected.")


def validate_channel_url(value):
    """Accept a canonical URL only when its channel is explicitly allowlisted."""
    return get_channel(_channel_from_url(value)).url


def message_permalink(external_id, *, channel=None):
    if not isinstance(external_id, str) or not MESSAGE_TIMESTAMP.fullmatch(external_id):
        raise ValueError("Slack message ID must be its exact seconds.microseconds timestamp.")
    selected = get_channel(channel)
    return f"{selected.url}/p{external_id.replace('.', '')}"


def validate_message_identity(external_id, url, *, channel=None, source_key=None):
    """Reject cross-channel messages, ambiguous IDs, and mismatched permalinks."""
    parsed = _validated_url(url)
    match = MESSAGE_PATH.fullmatch(parsed.path)
    if not match:
        raise ValueError("Slack source URL must be a canonical message permalink.")
    selected = get_channel(f"https://{WORKSPACE_HOST}/archives/{match[1]}")
    if channel is not None and get_channel(channel) != selected:
        raise ValueError("Slack permalink does not match the selected channel.")
    if source_key is not None and source_key != selected.source_key:
        raise ValueError("Slack source key must match the configured message channel.")
    expected_url = message_permalink(external_id, channel=selected)
    if url != expected_url:
        raise ValueError("Slack permalink must match the configured channel and message timestamp.")
    return external_id, expected_url
