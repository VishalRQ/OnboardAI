"""Confluence ingestion: spaces -> pages -> documents.

Real implementation uses atlassian-python-api against settings.confluence_url.
Until credentials exist, `load_pages` serves local fixtures with the same
shape as live pages so the rest of the pipeline can be built and tuned
offline.
"""

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.core.config import get_settings
from app.core.logging import get_logger
from app.services.confluence.client import ConfluenceClient
from app.services.confluence.html_to_text import storage_to_text

logger = get_logger(__name__)


@dataclass
class RawDocument:
    id: str
    title: str
    text: str
    metadata: dict = field(default_factory=dict)


def _fixture(page_id, title, ancestors, labels, text, version=1,
             space="ENG", space_name="Engineering"):
    """Build a fixture page carrying the metadata a live page would have."""
    return RawDocument(
        id=f"confluence:{page_id}",
        title=title,
        text=text,
        metadata={
            "source": "confluence",
            "page_id": str(page_id),
            "space_key": space,
            "space_name": space_name,
            "version": version,
            "ancestors": ancestors,
            "labels": f"|{'|'.join(labels)}|" if labels else "",
            "author": "A. Rivera",
            "content_type": "page",
            "url": f"https://example.atlassian.net/wiki/spaces/{space}/pages/{page_id}",
            "updated_at": "2026-08-14T09:12:00Z",
        },
    )


FIXTURES = [
    _fixture(
        123456, "Engineering Onboarding", "Handbook > Onboarding",
        ["onboarding", "day-one"], version=7,
        text="""# Day one
Collect your laptop from IT on the third floor. Bring photo ID; they will not
release hardware without it.

## Laptop access
Submit an IT ticket through the service desk portal, category Hardware, and
wait for the confirmation email. It usually arrives within two hours. Once it
lands, sign in with your corporate account and enable FileVault immediately.

## Accounts and tooling
Your manager requests access to the core systems on your behalf. Expect email,
calendar and chat on day one; repository and deploy access follow after the
security training is complete.

# Week one
Read the deploy runbook end to end before shadowing a release. Pair with your
onboarding buddy for the first deploy rather than driving it yourself.""",
    ),
    _fixture(
        123457, "Deploy Runbook", "Handbook > Engineering > Operations",
        ["deploy", "runbook", "oncall"], version=12,
        text="""# Before you deploy
Confirm the change is merged to main and CI is green. Announce the deploy in
the engineering channel with the change summary and expected blast radius.

# Deploying
Run the pipeline from the release branch. Watch the error rate dashboard for
ten minutes after the rollout completes. Do not start a second deploy while
the first is still soaking.

# Rolling back
If the error rate rises above baseline, roll back first and investigate after.
The rollback command reverts to the previous released artifact and takes about
ninety seconds to propagate.""",
    ),
    _fixture(
        123458, "Requesting Time Off", "Handbook > People",
        ["leave", "policy"], version=3, space="HR", space_name="People Ops",
        text="""# How to request leave
Submit the request in the HR portal at least two weeks in advance for planned
leave. Your manager approves it there; no email is required.

# Sick leave
Notify your manager as early as you can on the day, then log it in the portal
retrospectively. No documentation is needed for absences under three days.""",
    ),
]


def _to_document(page: dict, base_url: str) -> RawDocument:
    """Map one Confluence API result onto our metadata schema (section 2)."""
    page_id = str(page.get("id", ""))
    space = page.get("space") or {}
    version = (page.get("version") or {}).get("number", 1)
    ancestors = " > ".join(a.get("title", "") for a in page.get("ancestors") or [])
    labels = [
        label.get("name", "")
        for label in ((page.get("metadata") or {}).get("labels") or {}).get("results", [])
    ]
    by = ((page.get("history") or {}).get("lastUpdated") or {}).get("by") or {}
    updated = ((page.get("history") or {}).get("lastUpdated") or {}).get("when", "")
    body = ((page.get("body") or {}).get("storage") or {}).get("value", "")
    webui = ((page.get("_links") or {}).get("webui")) or ""

    return RawDocument(
        id=f"confluence:{page_id}",
        title=page.get("title", ""),
        text=storage_to_text(body),
        metadata={
            "source": "confluence",
            "page_id": page_id,
            "space_key": space.get("key", ""),
            "space_name": space.get("name", ""),
            "version": version,
            "ancestors": ancestors,
            "labels": f"|{'|'.join(labels)}|" if labels else "",
            "author": by.get("displayName", ""),
            "content_type": page.get("type", "page"),
            "url": f"{base_url}{webui}" if webui else "",
            "updated_at": updated or (page.get("version") or {}).get("when", ""),
        },
    )


def has_credentials() -> bool:
    """Whether a live Confluence is reachable, as opposed to fixture mode."""
    settings = get_settings()
    return bool(settings.confluence_url and settings.confluence_api_token)


def configured_spaces() -> list[str]:
    return list(get_settings().confluence_space_keys)


#: (expires_at, spaces) for the live space list. The UI re-asks on every
#: Streamlit rerun -- that is a widget click paginating a remote API -- and
#: spaces are created about weekly, so a short TTL costs nothing in freshness.
_SPACE_CACHE: tuple[float, list[dict]] | None = None


def invalidate_space_cache() -> None:
    """Drop the cached space list. Called after an ingest, which is the one
    moment a caller has a reason to expect the list to have changed."""
    global _SPACE_CACHE
    _SPACE_CACHE = None


def list_spaces() -> list[dict]:
    """Spaces available to pick from: `{key, name}`, deduped and sorted.

    Live credentials give the real list. Without them the fixtures decide,
    so the picker still works offline instead of rendering an empty dropdown.
    """
    global _SPACE_CACHE
    if not has_credentials():
        seen = {
            page.metadata["space_key"]: page.metadata.get("space_name", "")
            for page in FIXTURES
        }
        return [{"key": key, "name": name} for key, name in sorted(seen.items())]

    if _SPACE_CACHE and _SPACE_CACHE[0] > time.monotonic():
        return _SPACE_CACHE[1]

    spaces = [
        {"key": space.get("key", ""), "name": space.get("name", "")}
        for space in ConfluenceClient().list_spaces()
        if space.get("key")
    ]
    # a space listed in .env but invisible to the token is worth showing as a
    # choice: ingesting it produces the "check the space KEY" warning, which
    # says far more than the key silently missing from the dropdown
    known = {space["key"] for space in spaces}
    spaces.extend({"key": key, "name": ""} for key in configured_spaces() if key not in known)
    spaces = sorted(spaces, key=lambda space: space["key"])
    ttl = get_settings().confluence_space_cache_seconds
    _SPACE_CACHE = (time.monotonic() + ttl, spaces)
    return spaces


def load_page_ids(space_keys: list[str]) -> set[str] | None:
    """Current page ids across `space_keys`, or None when in fixture mode.

    None rather than an empty set on purpose: reconciliation must not read
    "the source has no pages" and purge the whole index.
    """
    if not has_credentials():
        return None
    client = ConfluenceClient()
    ids: set[str] = set()
    for space_key in space_keys:
        ids |= client.list_page_ids(space_key)
    return ids


def load_pages(
    space_keys: list[str] | None = None, since: str | None = None
) -> list[RawDocument]:
    """Fetch pages from Confluence. Serves fixtures when no credentials exist.

    `since` limits the query to pages modified on or after that date, which is
    what makes the scheduled sync cheap.
    """
    settings = get_settings()
    spaces = space_keys or settings.confluence_space_keys

    if not has_credentials():
        # honour the space filter here too, or the picker looks broken offline
        selected = [
            page for page in FIXTURES
            if not spaces or page.metadata["space_key"] in spaces
        ]
        logger.warning(
            "No Confluence credentials configured; serving %d fixture pages", len(selected)
        )
        return selected

    if not spaces:
        raise ValueError("CONFLUENCE_SPACE_KEYS is empty; nothing to load")

    client = ConfluenceClient()
    base_url = settings.confluence_url.rstrip("/")
    documents: list[RawDocument] = []
    for space_key in spaces:
        raw = client.search_pages(space_key, since=since)
        if not raw:
            # Confluence answers 200 with an empty result set for a space key
            # that does not exist, so a typo is otherwise indistinguishable
            # from a space that simply has no pages.
            logger.warning(
                "Confluence: space %r returned no pages -- check the space KEY "
                "(the short code in /wiki/spaces/<KEY>/, not the display name)",
                space_key,
            )
            continue
        logger.info("Confluence: %d pages from space %s", len(raw), space_key)
        for page in raw:
            document = _to_document(page, base_url)
            if document.text.strip():
                documents.append(document)
            else:
                logger.info("Skipping empty page %s (%s)", document.id, document.title)
    return documents


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
