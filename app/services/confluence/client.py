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

    def list_spaces(self, limit: int = 50) -> list[dict]:
        """Spaces visible to this account. Used to verify access."""
        data = self._get("/rest/api/space", {"limit": limit})
        return data.get("results", [])

    def search_pages(self, space_key: str, since: str | None = None) -> list[dict]:
        """Pages in `space_key`, optionally only those modified since `since`.

        `since` is a Confluence date string (YYYY-MM-DD or 'YYYY-MM-DD HH:MM').
        """
        cql = f'space = "{space_key}" AND type = page'
        if since:
            cql += f' AND lastmodified >= "{since}"'
        cql += " ORDER BY lastmodified DESC"

        results, start = [], 0
        while True:
            data = self._get("/rest/api/content/search",
                             {"cql": cql, "limit": PAGE_LIMIT, "start": start,
                              "expand": EXPAND})
            batch = data.get("results", [])
            results.extend(batch)
            if len(batch) < PAGE_LIMIT:
                break
            start += PAGE_LIMIT
            logger.info("Fetched %d pages from %s so far", len(results), space_key)
        return results

    def whoami(self) -> dict:
        """Current user; the cheapest call that proves the credentials work."""
        return self._get("/rest/api/user/current")
