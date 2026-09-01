"""Confluence ingestion: spaces -> pages -> documents.

Real implementation will use atlassian-python-api or LangChain's
ConfluenceLoader against settings.confluence_url.
"""

from dataclasses import dataclass, field

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)


@dataclass
class RawDocument:
    id: str
    title: str
    text: str
    metadata: dict = field(default_factory=dict)


async def load_pages(space_keys: list[str] | None = None) -> list[RawDocument]:
    """Fetch pages from Confluence. Returns one dummy page for now."""
    settings = get_settings()
    spaces = space_keys or settings.confluence_space_keys
    logger.info("Loading Confluence pages for spaces=%s", spaces or "<all>")

    # from langchain_community.document_loaders import ConfluenceLoader
    # loader = ConfluenceLoader(url=settings.confluence_url, ...)
    return [
        RawDocument(
            id="confluence:demo-1",
            title="Engineering Onboarding",
            text="Day one: get laptop access, join #eng, read the deploy runbook.",
            metadata={"space": (spaces[0] if spaces else "ENG"), "source": "confluence"},
        )
    ]
