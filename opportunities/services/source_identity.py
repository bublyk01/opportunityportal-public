"""Dispatch trusted source identity validation independently of collectors."""


def normalize_source_identity(source, source_key, external_id, url):
    if source == "gmail":
        from .gmail_source import validate_message_identity

        if not isinstance(source_key, str) or not source_key:
            raise ValueError("Gmail imports must use the configured account source key.")
        return validate_message_identity(external_id, url, source_key=source_key)
    if source == "slack":
        from .slack_source import validate_message_identity

        if not isinstance(source_key, str) or not source_key:
            raise ValueError("Slack imports must use the configured workspace and channel source key.")
        return validate_message_identity(external_id, url, source_key=source_key)
    if source == "notion":
        from .notion_public import canonical_page_id, validate_source_url

        external_id = canonical_page_id(external_id)
        url = validate_source_url(url)
        if url.rsplit("/", 1)[-1] != external_id:
            raise ValueError("Source URL must match the external Notion page ID.")
        return external_id, url
    raise ValueError("Only configured Notion, Slack, and Gmail sources can be imported.")
