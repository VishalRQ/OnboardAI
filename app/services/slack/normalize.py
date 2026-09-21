"""Raw Slack message dict -> clean plain text + a stable edit marker.

A workspace export and `conversations.history` return message objects with the
same shape, so one normalizer serves both ingestion paths.

Slack wraps entities in angle brackets: `<@U123|alice>`, `<#C1|general>`,
`<https://x|label>`, `<!here>`. Left raw they are noise to both the embedder
and the reader, so they are unwrapped to what a person would have typed.
"""

import html
import re

_MENTION = re.compile(r"<@([A-Z0-9]+)(?:\|([^>]+))?>")
_CHANNEL = re.compile(r"<#[A-Z0-9]+\|([^>]+)>")
_LINK = re.compile(r"<(https?://[^|>]+)(?:\|([^>]+))?>")
#: <!here>, <!channel>, <!subteam^S123|@platform>, <!date^...|fallback>
_SPECIAL = re.compile(r"<!(\w+)(?:\^[^|>]+)?(?:\|([^>]+))?>")

#: subtypes that are Slack system chatter, never knowledge
SKIP_SUBTYPES = {
    "channel_join", "channel_leave", "channel_topic", "channel_purpose",
    "channel_name", "channel_archive", "channel_unarchive", "pinned_item",
    "bot_add", "bot_remove", "reminder_add", "app_conversation_join",
}


def clean_text(text: str, users: dict[str, str]) -> str:
    if not text:
        return ""
    text = _MENTION.sub(lambda m: "@" + (m.group(2) or users.get(m.group(1), m.group(1))), text)
    text = _CHANNEL.sub(lambda m: "#" + m.group(1), text)
    text = _SPECIAL.sub(lambda m: "@" + (m.group(2) or m.group(1)).lstrip("@"), text)
    text = _LINK.sub(lambda m: m.group(2) or m.group(1), text)
    return html.unescape(text).strip()


def is_noise(msg: dict) -> bool:
    """True for anything that should never reach the index."""
    if msg.get("type") != "message":
        return True
    if msg.get("subtype") in SKIP_SUBTYPES:
        return True
    # a bot post with no text body is a card / attachment-only app message
    return msg.get("subtype") == "bot_message" and not (msg.get("text") or "").strip()


def message_version(msg: dict) -> str:
    """A marker that increases when the message is edited.

    Slack edits a message in place and only bumps `edited.ts`, so the unit's
    version has to consider both. Comparing the string form is safe: Slack
    timestamps are zero-padded `seconds.micros`.
    """
    edited = (msg.get("edited") or {}).get("ts")
    return max(str(msg.get("ts", "0")), str(edited or "0"))


def normalize(msg: dict, channel: str, users: dict[str, str]) -> dict | None:
    """One raw message -> a flat row, or None if it carries no text."""
    text = clean_text(msg.get("text", ""), users)
    if not text:
        return None
    ts = str(msg["ts"])
    uid = msg.get("user") or msg.get("bot_id") or ""
    return {
        "channel": channel,
        "ts": ts,
        "thread_ts": str(msg.get("thread_ts") or ts),
        "user_id": uid,
        "user_name": users.get(uid) or msg.get("username") or uid or "unknown",
        "text": text,
        "version": message_version(msg),
    }
