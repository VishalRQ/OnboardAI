"""Every knowledge source, by name -- the one place a new source is registered."""

from app.services.common.base import BaseRAGService
from app.services.confluence import get_confluence_service
from app.services.slack import get_slack_service


def get_services() -> dict[str, BaseRAGService]:
    return {
        "confluence": get_confluence_service(),
        "slack": get_slack_service(),
    }
