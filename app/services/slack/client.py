"""Thin Slack Web API client for ingestion.

Wraps only the calls ingestion needs -- list/history/replies, users, permalink
-- with cursor pagination, a configurable inter-request pause, and 429
Retry-After backoff.

Rate limits: since 2025 a new, non-Marketplace app is throttled to roughly
one `conversations.history` call per minute, 15 messages per page. Set
`SLACK_MIN_REQUEST_INTERVAL=60` for such an app so a backfill self-paces
instead of tripping 429s the whole way. An older or Marketplace-approved app
can leave it at 0.
"""

import time
from collections.abc import Iterator

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

_MAX_RETRIES = 5


class SlackAuthError(RuntimeError):
    """Token missing, revoked, or lacking a required scope."""


class SlackClient:
    def __init__(self, token: str | None = None, min_interval: float | None = None):
        settings = get_settings()
        tok = token or settings.slack_bot_token
        if not tok:
            raise SlackAuthError("SLACK_BOT_TOKEN is not configured")
        self._client = WebClient(token=tok)
        self._pause = (
            settings.slack_min_request_interval if min_interval is None else min_interval
        )
        self._last_call = 0.0
        self._page = 15 if self._pause >= 60 else 200

    # ----- transport -----------------------------------------------------

    def _call(self, method: str, **params) -> dict:
        """One API call, self-paced, retrying on 429 and transient 5xx."""
        gap = self._pause - (time.monotonic() - self._last_call)
        if gap > 0:
            time.sleep(gap)

        for attempt in range(_MAX_RETRIES):
            try:
                resp = self._client.api_call(method, params=params)
                self._last_call = time.monotonic()
                return resp.data
            except SlackApiError as exc:
                status = exc.response.status_code
                err = exc.response.get("error", "")
                if err in {"invalid_auth", "not_authed", "account_inactive", "token_revoked"}:
                    raise SlackAuthError(f"Slack rejected the token: {err}") from exc
                if err == "missing_scope":
                    raise SlackAuthError(
                        f"Token is missing a scope for {method}: needs "
                        f"{exc.response.get('needed')!r}"
                    ) from exc
                if status == 429:
                    wait = int(exc.response.headers.get("Retry-After", 30))
                    logger.warning("Slack 429 on %s; sleeping %ds (%d/%d)",
                                   method, wait, attempt + 1, _MAX_RETRIES)
                    time.sleep(wait)
                    continue
                if status >= 500:
                    logger.warning("Slack %s on %s; retry %d/%d", status, method,
                                   attempt + 1, _MAX_RETRIES)
                    time.sleep(2 ** attempt)
                    continue
                raise
        raise RuntimeError(f"Slack still failing after {_MAX_RETRIES} attempts: {method}")

    def _paginate(self, method: str, key: str, **params) -> Iterator[dict]:
        cursor = None
        while True:
            page = self._call(method, cursor=cursor, limit=self._page, **params)
            yield from page.get(key, [])
            cursor = (page.get("response_metadata") or {}).get("next_cursor") or ""
            if not cursor:
                return

    # ----- calls we use ------------------------------------------------

    def auth_test(self) -> dict:
        """Cheapest call that proves the token works; also gives the team url."""
        return self._call("auth.test")

    def users(self) -> dict[str, str]:
        """user_id -> display name, for un-wrapping @mentions."""
        out: dict[str, str] = {}
        for u in self._paginate("users.list", "members"):
            profile = u.get("profile", {})
            out[u["id"]] = (
                profile.get("display_name")
                or profile.get("real_name")
                or u.get("name")
                or u["id"]
            )
        return out

    def resolve_channels(self, wanted: list[str], include_private: bool) -> list[dict]:
        """Map channel names/ids to `{id, name}`. Empty `wanted` = all the bot is in."""
        types = "public_channel,private_channel" if include_private else "public_channel"
        wanted_set = {w.lstrip("#") for w in wanted}
        found: list[dict] = []
        for ch in self._paginate("conversations.list", "channels",
                                 types=types, exclude_archived=True):
            if not wanted_set or ch["id"] in wanted_set or ch["name"] in wanted_set:
                found.append({"id": ch["id"], "name": ch["name"],
                              "is_private": ch.get("is_private", False)})
        missing = wanted_set - {c["id"] for c in found} - {c["name"] for c in found}
        if missing:
            logger.warning("Slack: requested channels not found or bot not a member: %s",
                           ", ".join(sorted(missing)))
        return found

    def history(self, channel_id: str, oldest: str | None = None) -> list[dict]:
        """Top-level messages in a channel, oldest first."""
        params = {"channel": channel_id}
        if oldest:
            params["oldest"] = oldest
        msgs = list(self._paginate("conversations.history", "messages", **params))
        msgs.sort(key=lambda m: float(m["ts"]))
        return msgs

    def replies(self, channel_id: str, thread_ts: str) -> list[dict]:
        """Every message in a thread (root included), oldest first."""
        msgs = list(self._paginate("conversations.replies", "messages",
                                   channel=channel_id, ts=thread_ts))
        msgs.sort(key=lambda m: float(m["ts"]))
        return msgs

    def permalink(self, channel_id: str, ts: str) -> str | None:
        try:
            return self._call("chat.getPermalink", channel=channel_id,
                              message_ts=ts).get("permalink")
        except Exception as exc:  # noqa: BLE001 - a missing permalink must not abort ingest
            logger.info("No permalink for %s/%s: %s", channel_id, ts, exc)
            return None
