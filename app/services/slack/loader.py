"""Slack ingestion: export files now, Web API incremental sync later.

Real implementation will read a Slack export directory (channels.json,
users.json, per-channel day files), then move to slack-sdk
conversations.history / conversations.replies for incremental sync.
"""

from dataclasses import dataclass, field

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)


@dataclass
class RawThread:
    id: str
    channel: str
    text: str
    metadata: dict = field(default_factory=dict)


async def load_from_export(export_path: str | None = None) -> list[RawThread]:
    """Read threads from a Slack export archive."""
    settings = get_settings()
    path = export_path or settings.slack_export_path
    logger.info("Loading Slack export from %s", path or "<unset>")
    return [
        RawThread(
            id="slack:C123-1700000000.000100",
            channel="#eng-onboarding",
            text="Q: how do I get staging DB access? A: open a ticket in #it-help.",
            metadata={"channel_id": "C123", "source": "slack"},
        )
    ]


async def load_incremental(channel_ids: list[str]) -> list[RawThread]:
    """Pull new messages via the Slack Web API. Not implemented yet."""
    logger.info("Incremental Slack sync requested for %s", channel_ids)
    # from slack_sdk import WebClient
    # client = WebClient(token=get_settings().slack_bot_token)
    return []
