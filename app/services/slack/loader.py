"""Slack ingestion: channels -> messages -> thread/window units -> documents.

Three input paths, in priority order:
  1. live Web API      when SLACK_BOT_TOKEN is set
  2. workspace export   when SLACK_EXPORT_PATH points at a .zip or unzipped dir
  3. fixtures           otherwise, so the rest of the pipeline runs offline

A retrieval unit is either a whole thread (root + all replies) or a
time-window of stand-alone messages. Threads are never split -- the answer to
"why did we do X" is almost always in the replies, not the root.
"""

import json
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from app.core.config import get_settings
from app.core.logging import get_logger
from app.services.slack.normalize import is_noise, normalize

logger = get_logger(__name__)

#: Flush a window of loose (non-threaded) messages when any of these trips.
#: The char cap keeps a window to roughly one topic: a channel of standalone
#: "onboarding note: ..." posts should not collapse into one grab-bag chunk
#: that matches no query well. Real back-and-forth lives in threads, which are
#: never windowed.
WINDOW_MAX_MESSAGES = 10
WINDOW_MAX_CHARS = 350
WINDOW_GAP_MINUTES = 30


@dataclass
class RawThread:
    """One retrieval unit before chunking. Same shape as Confluence's RawDocument."""

    id: str
    title: str
    text: str
    metadata: dict = field(default_factory=dict)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _iso(ts: str) -> str:
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# grouping: normalized messages -> thread / window units -> RawThread
# --------------------------------------------------------------------------

def _render(channel: str, rows: list[dict]) -> str:
    date = _iso(rows[0]["ts"])[:10]
    lines = [f"#{channel} • {date}"]
    for r in rows:
        hhmm = _iso(r["ts"])[11:16]
        lines.append(f"[{hhmm}] {r['user_name']}: {r['text']}")
    return "\n".join(lines)


def _participants(rows: list[dict]) -> str:
    seen: list[str] = []
    for r in rows:
        if r["user_name"] not in seen:
            seen.append(r["user_name"])
    return ", ".join(seen)


def _unit_to_thread(*, channel, channel_id, rows, unit_type, permalink, visibility="public") -> RawThread:
    rows = sorted(rows, key=lambda r: float(r["ts"]))
    root_ts = rows[0]["thread_ts"] if unit_type == "thread" else rows[0]["ts"]
    latest_version = max(r.get("version", r["ts"]) for r in rows)
    doc_id = f"{channel_id or channel}:{root_ts}"
    first_line = rows[0]["text"].splitlines()[0][:80]
    title = f"#{channel} — {first_line}"
    text = _render(channel, rows)
    return RawThread(
        id=f"slack:{doc_id}",
        title=title,
        text=text,
        metadata={
            "source": "slack",
            "doc_id": doc_id,
            "channel": f"#{channel}",
            "channel_id": channel_id or "",
            "thread_ts": root_ts,
            "unit_type": unit_type,
            "participants": _participants(rows),
            "author": rows[0]["user_name"],
            "message_count": len(rows),
            # drives the incremental skip check; bumps when any message is edited
            "version": latest_version,
            "created_at": _iso(rows[0]["ts"]),
            "updated_at": _iso(rows[-1]["ts"]),
            "url": permalink or "",
            "visibility": visibility,
        },
    )


def _group_units(channel: str, channel_id: str, rows: list[dict]) -> list[list[dict]]:
    """Bucket rows into thread groups and windowed groups of loose messages."""
    rows = sorted(rows, key=lambda r: float(r["ts"]))
    buckets: dict[str, list[dict]] = {}
    for r in rows:
        buckets.setdefault(r["thread_ts"], []).append(r)

    units: list[tuple[str, list[dict]]] = []
    loose: list[dict] = []
    for msgs in buckets.values():
        if len(msgs) > 1:
            units.append(("thread", msgs))
        else:
            loose.extend(msgs)

    loose.sort(key=lambda r: float(r["ts"]))
    window: list[dict] = []
    chars = 0
    for r in loose:
        if window and (
            len(window) >= WINDOW_MAX_MESSAGES
            or chars + len(r["text"]) > WINDOW_MAX_CHARS
            or (float(r["ts"]) - float(window[-1]["ts"])) / 60.0 > WINDOW_GAP_MINUTES
        ):
            units.append(("window", window))
            window, chars = [], 0
        window.append(r)
        chars += len(r["text"])
    if window:
        units.append(("window", window))

    units.sort(key=lambda u: float(u[1][0]["ts"]))
    return units


# --------------------------------------------------------------------------
# fixtures -- realistic onboarding threads, used when nothing is configured
# --------------------------------------------------------------------------

def _fixture(channel_id, channel, root_ts, rows, *, private=False) -> RawThread:
    """rows: list of (offset_seconds, user, text). First row is the thread root."""
    base = 1_705_300_000
    root = f"{base + root_ts}.000000"
    msgs = [
        {
            "ts": f"{base + root_ts + off}.000000",
            "user_name": user,
            "text": text,
            "thread_ts": root,
            "version": f"{base + root_ts + off}.000000",
        }
        for off, user, text in rows
    ]
    return _unit_to_thread(
        channel=channel, channel_id=channel_id, rows=msgs, unit_type="thread",
        permalink=f"https://acme.slack.com/archives/{channel_id}/p{base + root_ts}000000",
        visibility="private" if private else "public",
    )


_FIXTURE_ROWS = [
    ("C0ENG", "eng-onboarding", 100, [
        (0, "priya", "Welcome @sam! Quick start: all our code is in one repo, `platform-monorepo`. Clone it and run `make dev` to bring up Postgres, the API and the web app with docker compose."),
        (90, "sam", "Thanks! `make dev` is failing on my M2 MacBook with a platform error."),
        (150, "priya", "Known issue on Apple silicon. Add `export DOCKER_DEFAULT_PLATFORM=linux/amd64` to your shell profile and re-run. That fixes it for everyone here."),
        (240, "sam", "That worked, stack is up. Where are the API docs?"),
        (300, "priya", "`make dev` serves them at http://localhost:8080/docs. The architecture overview is in `docs/architecture.md`."),
    ]),
    ("C0ENG", "eng-onboarding", 5000, [
        (0, "marco", "How do I get access to the staging database?"),
        (120, "deepa", "Open a ticket in the service desk portal, category Access, and pick the `staging-db-readonly` role. Your manager approves it, usually within a day."),
        (200, "marco", "And production?"),
        (260, "deepa", "Production DB access is on-call only and time-boxed. You request it through the same portal when you're on the rota, not before."),
    ]),
    ("C0PAY", "payments", 9000, [
        (0, "lena", "Why did we move off Stripe Connect for marketplace payouts?"),
        (140, "omar", "Two reasons. The payout timing didn't fit our sellers -- Connect held funds longer than our SLA promised -- and the fee structure got worse after their 2024 pricing change."),
        (220, "omar", "We switched payouts to Adyen for Platforms in Q3. Card acquiring stayed on Stripe; only the payout leg moved."),
        (300, "lena", "Got it, so new services should use the `payouts-adyen` client, not Connect."),
    ]),
    ("C0INF", "infra", 12000, [
        (0, "raj", "What's the deploy process for a backend service?"),
        (110, "nina", "Merge to `main` with CI green, then announce in #infra with the change summary. Run the deploy pipeline from the release branch and watch the error-rate dashboard for ~10 minutes."),
        (190, "nina", "If the error rate climbs above baseline, roll back first with `deploy rollback <service>` and investigate after. Rollback takes about 90 seconds to propagate."),
        (250, "raj", "Can I run two deploys at once if they're different services?"),
        (280, "nina", "Different services yes. Never a second deploy of the same service while the first is still soaking."),
    ]),
]

FIXTURES: list[RawThread] = [_fixture(*row) for row in _FIXTURE_ROWS]


# --------------------------------------------------------------------------
# input path 2: workspace export
# --------------------------------------------------------------------------

def _open_export(path: Path) -> Path:
    if path.is_dir():
        return path
    if path.suffix == ".zip":
        target = path.with_suffix("")
        with zipfile.ZipFile(path) as zf:
            zf.extractall(target)
        nested = [p for p in target.iterdir() if p.is_dir()]
        if len(nested) == 1 and not (target / "channels.json").exists():
            return nested[0]
        return target
    raise ValueError(f"Not a Slack export dir or .zip: {path}")


def _read_export(export_path: str, wanted: list[str]) -> tuple[list[tuple], dict[str, str]]:
    src = _open_export(Path(export_path))
    users: dict[str, str] = {}
    if (src / "users.json").exists():
        for u in json.loads((src / "users.json").read_text()):
            p = u.get("profile", {})
            users[u["id"]] = p.get("display_name") or p.get("real_name") or u.get("name") or u["id"]

    id_by_name = {}
    if (src / "channels.json").exists():
        for c in json.loads((src / "channels.json").read_text()):
            id_by_name[c["name"]] = c["id"]

    wanted_set = {w.lstrip("#") for w in wanted}
    out = []
    for ch_dir in sorted(p for p in src.iterdir() if p.is_dir()):
        if wanted_set and ch_dir.name not in wanted_set:
            continue
        raw: list[dict] = []
        for day in sorted(ch_dir.glob("*.json")):
            raw.extend(json.loads(day.read_text()))
        out.append((ch_dir.name, id_by_name.get(ch_dir.name, ""), raw))
    return out, users


# --------------------------------------------------------------------------
# input path 1: live Web API
# --------------------------------------------------------------------------

def _archive_link(team_url: str, channel_id: str, ts: str) -> str:
    """Build a message permalink without spending an API call per unit.

    Slack's own permalink for a root message is exactly this shape; doing it
    locally matters when a throttled app would otherwise burn a minute per
    thread on chat.getPermalink.
    """
    if not (team_url and channel_id):
        return ""
    return f"{team_url.rstrip('/')}/archives/{channel_id}/p{ts.replace('.', '')}"


def _fetch_live(
    wanted: list[str], since: str | None
) -> tuple[list[tuple], dict[str, str], str]:
    from app.services.slack.client import SlackClient

    client = SlackClient()
    who = client.auth_test()
    team_url = who.get("url", "")
    logger.info("Slack: authenticated as %s in %s", who.get("user"), who.get("team"))
    users = client.users()

    settings = get_settings()
    channels = client.resolve_channels(wanted, include_private=settings.slack_index_private)
    if not channels:
        raise ValueError(
            "No matching Slack channels. Invite the bot to the channel "
            "(/invite @<app>) and check SLACK_CHANNELS."
        )

    out = []
    for ch in channels:
        top = client.history(ch["id"], oldest=since)
        raw: list[dict] = []
        for msg in top:
            if msg.get("thread_ts") and msg.get("reply_count"):
                raw.extend(client.replies(ch["id"], msg["thread_ts"]))
            else:
                raw.append(msg)
        logger.info("Slack: #%s -> %d messages", ch["name"], len(raw))
        out.append((ch["name"], ch["id"], raw, ch.get("is_private", False)))
    return out, users, team_url


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------

async def load_threads(
    channels: list[str] | None = None,
    since: str | None = None,
    export_path: str | None = None,
) -> list[RawThread]:
    settings = get_settings()
    wanted = channels or settings.slack_channels
    export_path = export_path or settings.slack_export_path

    team_url = ""
    # each entry: (channel_name, channel_id, raw_messages, is_private)
    raw_channels: list[tuple[str, str, list[dict], bool]]
    if settings.slack_bot_token:
        live, users, team_url = _fetch_live(wanted, since)
        raw_channels = live
    elif export_path:
        logger.info("Slack: reading workspace export at %s", export_path)
        exported, users = _read_export(export_path, wanted)
        raw_channels = [(name, cid, raw, False) for (name, cid, raw) in exported]
    else:
        logger.warning("No Slack token or export path; serving %d fixture threads", len(FIXTURES))
        return list(FIXTURES)

    threads: list[RawThread] = []
    for channel, channel_id, raw, is_private in raw_channels:
        norm = [normalize(m, channel, users) for m in raw if not is_noise(m)]
        norm = [m for m in norm if m]
        if not norm:
            continue
        visibility = "private" if is_private else "public"
        for unit_type, rows in _group_units(channel, channel_id, norm):
            unit = _unit_to_thread(
                channel=channel, channel_id=channel_id, rows=rows,
                unit_type=unit_type, permalink=None, visibility=visibility,
            )
            unit.metadata["url"] = _archive_link(team_url, channel_id, unit.metadata["thread_ts"])
            threads.append(unit)

    logger.info("Slack: %d retrieval units from %d channels", len(threads), len(raw_channels))
    return threads
