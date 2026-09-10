"""Watermark + run lock for incremental Confluence sync.

The whole point of the watermark is that a scheduled run costs a handful of
API calls: CQL returns only pages touched since the last clean run, and a page
whose version is already indexed is skipped before any text extraction.

Bookkeeping is per space, not global. An ingest can name any subset of spaces,
so a single shared watermark would let a run over ENG advance the clock for HR
too, and every HR page older than that run would then fall outside the next
delta query and never be indexed at all.
"""

import json
import os
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

#: Confluence CQL wants 'YYYY-MM-DD HH:MM', not ISO-8601 with a T and a zone
CQL_DATE_FORMAT = "%Y-%m-%d %H:%M"


class SyncInProgress(RuntimeError):
    """Another ingest holds the lock. Second run exits rather than overlapping."""


def _state_path() -> Path:
    return Path(get_settings().confluence_sync_state_path)


def _lock_path() -> Path:
    return _state_path().with_suffix(".lock")


def read_state() -> dict:
    """Last sync bookkeeping. A missing or corrupt file means "never synced"."""
    path = _state_path()
    try:
        state = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        # a damaged state file must not wedge ingestion -- a full window is
        # only ever more work, never wrong
        logger.warning("Unreadable sync state at %s (%s); treating as empty", path, exc)
        return {}
    if not isinstance(state, dict):
        logger.warning("Sync state at %s is not an object; treating as empty", path)
        return {}
    # A pre-per-space file carried last_synced_at at the top level. There is no
    # way to know which spaces it covered, so it is dropped: every space simply
    # runs one full window, which costs time and never loses a page.
    if "spaces" not in state and "last_synced_at" in state:
        logger.info("Migrating legacy global sync watermark; next run is a full window")
        return {}
    return state


def space_state(state: dict, space_key: str) -> dict:
    """Bookkeeping recorded for one space, or an empty dict if never synced."""
    spaces = state.get("spaces")
    if not isinstance(spaces, dict):
        return {}
    entry = spaces.get(space_key)
    return entry if isinstance(entry, dict) else {}


def record_sync(space_key: str, synced_at: str, reconciled_at: str | None = None) -> dict:
    """Merge one space's watermarks into the state file and return the result.

    Read-modify-write per space rather than one write at the end of the run, so
    a run that fails partway keeps the watermarks the earlier spaces earned.
    Callers hold `sync_lock`, which is what makes this safe.
    """
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    state = read_state()
    spaces = dict(state.get("spaces") or {})
    entry = {**space_state(state, space_key), "last_synced_at": synced_at}
    if reconciled_at:
        entry["last_reconciled_at"] = reconciled_at
    spaces[space_key] = entry
    state = {**state, "spaces": spaces}
    # write-then-rename so an interrupted write cannot leave a half file that
    # reads as "never synced" and triggers a full re-index
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2, sort_keys=True))
    temporary.replace(path)
    return state


def last_synced_at(state: dict) -> str | None:
    """The oldest per-space watermark, for a single "last sync" line in the UI.

    Oldest rather than newest: it is the honest answer to "how stale can what
    I am reading be?", which is what the number is there to tell a reader.
    """
    stamps = [
        entry.get("last_synced_at")
        for entry in (state.get("spaces") or {}).values()
        if isinstance(entry, dict) and entry.get("last_synced_at")
    ]
    return min(stamps) if stamps else None


def _parse(stamp: str | None) -> datetime | None:
    """Parse a stored ISO stamp into an aware UTC datetime, or None."""
    if not stamp:
        return None
    try:
        moment = datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        logger.warning("Ignoring unparseable sync timestamp %r", stamp)
        return None
    return moment.replace(tzinfo=timezone.utc) if moment.tzinfo is None else moment


def since_watermark(state: dict, space_key: str) -> str | None:
    """CQL `lastmodified` bound for `space_key`, or None to fetch everything.

    Steps back by the configured overlap: Confluence's `lastmodified` is
    eventually consistent and clocks drift, and re-processing a page is free
    because chunk ids are deterministic.
    """
    moment = _parse(space_state(state, space_key).get("last_synced_at"))
    if moment is None:
        return None
    overlap = timedelta(minutes=get_settings().confluence_sync_overlap_minutes)
    return (moment - overlap).astimezone(timezone.utc).strftime(CQL_DATE_FORMAT)


def reconcile_due(state: dict, space_key: str) -> bool:
    """Whether `space_key` is owed a deletion sweep.

    A delta query cannot see a deleted page, so only a full id-list diff can
    purge one -- expensive, and needed about daily rather than every run.
    """
    moment = _parse(space_state(state, space_key).get("last_reconciled_at"))
    if moment is None:
        return True
    interval = timedelta(hours=get_settings().confluence_reconcile_interval_hours)
    return datetime.now(timezone.utc) - moment >= interval


def _lock_age_seconds(path: Path) -> float | None:
    """How long the held lock has existed, by its own stamp then by mtime."""
    try:
        held = _parse(path.read_text().strip())
    except OSError:
        held = None
    if held is not None:
        return (datetime.now(timezone.utc) - held).total_seconds()
    try:
        return time.time() - path.stat().st_mtime
    except OSError:
        return None


@contextmanager
def sync_lock():
    """Fail fast if an ingest is already running.

    A full space index can outlast the interval between scheduled runs, and two
    concurrent writers against one embedded Chroma directory is the one thing
    the store cannot take.

    A lock left behind by a killed process is taken over once it is older than
    CONFLUENCE_SYNC_LOCK_STALE_MINUTES. Without that, a `kill -9` mid-ingest
    wedges every future run until somebody reads the error and deletes a file
    by hand -- which a scheduled caller can never do.
    """
    path = _lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    stale_after = get_settings().confluence_sync_lock_stale_minutes * 60

    for attempt in (1, 2):
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            age = _lock_age_seconds(path)
            if attempt == 1 and age is not None and age >= stale_after:
                logger.warning(
                    "Breaking a Confluence sync lock held for %.0f min (limit %.0f); "
                    "the holding process is assumed dead",
                    age / 60, stale_after / 60,
                )
                path.unlink(missing_ok=True)
                continue
            held = f" for {age / 60:.0f} min" if age is not None else ""
            raise SyncInProgress(
                f"A Confluence ingest is already running{held}. "
                f"If nothing is running, delete {path}."
            ) from None
    else:  # pragma: no cover - the second attempt raises or succeeds
        raise SyncInProgress(f"Could not acquire the Confluence sync lock at {path}.")

    try:
        os.write(descriptor, datetime.now(timezone.utc).isoformat(timespec="seconds").encode())
        os.close(descriptor)
        yield
    finally:
        path.unlink(missing_ok=True)
