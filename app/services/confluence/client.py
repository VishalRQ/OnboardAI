"""Thin Confluence Cloud REST client.

Talks to the v1 REST API directly rather than through atlassian-python-api:
CQL (needed for `lastmodified` incremental sync) is a v1 endpoint, and going
direct keeps pagination and 429 backoff under our control.
"""

import time

import httpx

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

PAGE_LIMIT = 50
MAX_RETRIES = 4
EXPAND = "body.storage,version,ancestors,metadata.labels,space,history.lastUpdated"


class ConfluenceAuthError(RuntimeError):
    """Credentials rejected by Confluence."""


class ConfluenceClient:
    def __init__(self, url=None, username=None, token=None, timeout=30.0):
        settings = get_settings()
        base = (url or settings.confluence_url or "").rstrip("/")
        if not base:
            raise ValueError("CONFLUENCE_URL is not configured")
        self.base = base
        self.auth = (username or settings.confluence_username or "",
                     token or settings.confluence_api_token or "")
        self.timeout = timeout

    def _get(self, path: str, params: dict | None = None) -> dict:
        """GET with retry on 429/5xx, honouring Retry-After."""
        url = f"{self.base}{path}"
        delay = 1.0
        for attempt in range(MAX_RETRIES):
            with httpx.Client(timeout=self.timeout, follow_redirects=True) as client:
                response = client.get(url, params=params, auth=self.auth,
                                      headers={"Accept": "application/json"})
            if response.status_code == 401:
                raise ConfluenceAuthError(
                    "401 from Confluence: check CONFLUENCE_USERNAME (must be the "
                    "account email) and CONFLUENCE_API_TOKEN."
                )
            if response.status_code == 403:
                raise ConfluenceAuthError(
                    "403 from Confluence: the account authenticated but lacks "
                    "permission for this resource."
                )
            if response.status_code == 429 or response.status_code >= 500:
                wait = float(response.headers.get("Retry-After", delay))
                logger.warning("Confluence %s; retrying in %.1fs (attempt %d/%d)",
                               response.status_code, wait, attempt + 1, MAX_RETRIES)
                time.sleep(wait)
                delay *= 2
                continue
            response.raise_for_status()
            return response.json()
        raise RuntimeError(f"Confluence still failing after {MAX_RETRIES} attempts: {url}")

    def _paginate(self, path: str, params: dict, label: str) -> list[dict]:
        """Walk every page of a paged Confluence collection."""
        results: list[dict] = []
        start = 0
        while True:
            data = self._get(path, {**params, "limit": PAGE_LIMIT, "start": start})
            batch = data.get("results", [])
            results.extend(batch)
            if len(batch) < PAGE_LIMIT:
                break
            start += PAGE_LIMIT
            logger.info("Fetched %d %s so far", len(results), label)
        return results

    def list_spaces(self, include_personal: bool = False) -> list[dict]:
        """Spaces visible to this account, for the space picker.

        Excludes personal spaces (~accountid): every user's own scratch area,
        which only clutters a list meant for team knowledge.

        Deliberately filters by exclusion rather than asking the API for
        type="global". Confluence has more team space types than that -- a
        knowledge base is type "knowledge_base" -- so requesting global alone
        silently hides real spaces from the picker, which then looks like the
        space does not exist rather than like a filter.
        """
        spaces = self._paginate("/rest/api/space", {}, "spaces")
        if include_personal:
            return spaces
        return [space for space in spaces if space.get("type") != "personal"]

    @staticmethod
    def _page_cql(space_key: str, since: str | None) -> str:
        cql = f'space = "{space_key}" AND type = page'
        if since:
            cql += f' AND lastmodified >= "{since}"'
        return cql + " ORDER BY lastmodified DESC"

    def search_pages(self, space_key: str, since: str | None = None) -> list[dict]:
        """Pages in `space_key`, optionally only those modified since `since`.

        `since` is a Confluence date string (YYYY-MM-DD or 'YYYY-MM-DD HH:MM').
        """
        return self._paginate(
            "/rest/api/content/search",
            {"cql": self._page_cql(space_key, since), "expand": EXPAND},
            f"pages from {space_key}",
        )

    def list_page_ids(self, space_key: str) -> set[str]:
        """Every current page id in `space_key`, with no body expansion.

        Deletion reconciliation only needs identity, and skipping `expand`
        keeps the daily sweep to a fraction of an ingest's cost.
        """
        pages = self._paginate(
            "/rest/api/content/search",
            {"cql": self._page_cql(space_key, None)},
            f"page ids from {space_key}",
        )
        return {str(page["id"]) for page in pages if page.get("id")}

    def whoami(self) -> dict:
        """Current user; the cheapest call that proves the credentials work."""
        return self._get("/rest/api/user/current")
