"""Slack ingestion logic that needs no network (normalize, grouping, loader).

Run: `pip install pytest pytest-asyncio && pytest tests/test_slack.py`
The service-level ingest/retrieve tests need Chroma + Ollama and live in the
end-to-end script, not here.
"""

import asyncio

from app.core.config import get_settings
from app.services.slack.loader import (
    WINDOW_GAP_MINUTES,
    _group_units,
    _unit_to_thread,
    load_threads,
)
from app.services.slack.normalize import (
    clean_text,
    is_noise,
    message_version,
    normalize,
)

USERS = {"U1": "alice", "U2": "bob"}


# ----- normalize --------------------------------------------------------

def test_clean_text_unwraps_entities():
    raw = "hi <@U1> in <#C9|general>, see <https://x.com/a|the doc> <!here> <!subteam^S1|@platform>"
    assert clean_text(raw, USERS) == "hi @alice in #general, see the doc @here @platform"


def test_clean_text_falls_back_to_user_id_when_unknown():
    assert clean_text("ping <@U404>", USERS) == "ping @U404"


def test_is_noise_filters_system_and_empty_bot_posts():
    assert is_noise({"type": "message", "subtype": "channel_join"})
    assert is_noise({"type": "message", "subtype": "bot_message", "text": "  "})
    assert not is_noise({"type": "message", "text": "real content"})
    assert not is_noise({"type": "message", "subtype": "bot_message", "text": "deploy done"})


def test_message_version_bumps_on_edit():
    base = {"ts": "1000.000100"}
    edited = {"ts": "1000.000100", "edited": {"ts": "2000.000200"}}
    assert message_version(edited) > message_version(base)


def test_normalize_drops_textless_messages():
    assert normalize({"ts": "1.0", "text": "", "user": "U1"}, "eng", USERS) is None
    row = normalize({"ts": "1.0", "text": "hello <@U2>", "user": "U1"}, "eng", USERS)
    assert row["text"] == "hello @bob"
    assert row["user_name"] == "alice"
    assert row["thread_ts"] == "1.0"  # its own ts when not in a thread


# ----- grouping -------------------------------------------------------

def _row(ts, text, thread_ts=None, user="alice"):
    return {"channel": "eng", "ts": f"{ts}.000000", "text": text, "user_name": user,
            "thread_ts": f"{thread_ts or ts}.000000", "version": f"{ts}.000000"}


def test_thread_stays_one_unit():
    rows = [_row(100, "root"), _row(160, "reply", thread_ts=100, user="bob"),
            _row(200, "reply2", thread_ts=100)]
    units = _group_units("eng", "C1", rows)
    assert len(units) == 1
    assert units[0][0] == "thread"
    assert len(units[0][1]) == 3


def test_loose_messages_split_on_time_gap():
    gap = (WINDOW_GAP_MINUTES + 5) * 60
    rows = [_row(100, "a"), _row(160, "b"), _row(160 + gap, "much later")]
    units = _group_units("eng", "C1", rows)
    assert [u[0] for u in units] == ["window", "window"]
    assert len(units[0][1]) == 2 and len(units[1][1]) == 1


def test_substantial_loose_messages_each_get_their_own_unit():
    # standalone knowledge posts must not collapse into one grab-bag chunk
    note = "x" * 300
    rows = [_row(100, note), _row(140, note), _row(180, note)]
    units = _group_units("eng", "C1", rows)
    assert len(units) == 3
    assert all(len(u[1]) == 1 for u in units)


def test_thread_and_loose_coexist():
    rows = [_row(100, "root"), _row(150, "in thread", thread_ts=100), _row(300, "standalone")]
    units = _group_units("eng", "C1", rows)
    kinds = sorted(u[0] for u in units)
    assert kinds == ["thread", "window"]


def test_unit_metadata_shape():
    rows = [_row(100, "why did we pick X?"), _row(160, "because Y", user="bob")]
    unit = _unit_to_thread(channel="eng", channel_id="C1", rows=rows,
                           unit_type="thread", permalink="https://s/p1")
    m = unit.metadata
    assert m["source"] == "slack"
    assert m["doc_id"] == "C1:100.000000"
    assert m["channel"] == "#eng"
    assert m["participants"] == "alice, bob"
    assert m["version"] == "160.000000"      # newest message drives incremental sync
    assert m["visibility"] == "public"
    assert m["url"] == "https://s/p1"


# ----- loader fallback ------------------------------------------------

def test_load_threads_serves_fixtures_without_credentials(monkeypatch):
    # force the offline path regardless of a real token in the dev .env
    s = get_settings()
    monkeypatch.setattr(s, "slack_bot_token", None)
    monkeypatch.setattr(s, "slack_export_path", None)
    units = asyncio.run(load_threads())
    assert len(units) == 4
    assert all(u.metadata["source"] == "slack" for u in units)
    assert {u.metadata["channel"] for u in units} == {"#eng-onboarding", "#payments", "#infra"}
