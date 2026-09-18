"""Streamlit UI for the OnboardAI backend.

A thin HTTP client over the FastAPI app -- no RAG logic lives here, so the
backend stays the single source of truth. Run the two side by side:

    uvicorn app.main:app --reload --reload-dir app
    streamlit run streamlit_app.py
"""

import os

import requests
import streamlit as st
from dotenv import load_dotenv

# same .env the backend reads, so the two are configured in one place
load_dotenv()

#: where the FastAPI app lives. Deployment config, not a user choice -- set
#: ONBOARDAI_API_URL in .env rather than exposing it as a field anyone can
#: point at an arbitrary host.
API_URL = os.getenv("ONBOARDAI_API_URL", "http://localhost:8000").rstrip("/")
#: ingest walks every page and embeds it, so it needs a far longer leash
CHAT_TIMEOUT = 300
INGEST_TIMEOUT = 1800
SLACK_INGEST_TIMEOUT = 7200
#: sources that can be searched together; the backend is the authority, this
#: only decides the label and the order they appear in
SOURCE_LABELS = {"confluence": "Confluence", "slack": "Slack"}

st.set_page_config(page_title="OnboardAI", page_icon="🧭", layout="wide")


def api_get(base: str, path: str, timeout: int = 30):
    response = requests.get(f"{base}{path}", timeout=timeout)
    response.raise_for_status()
    return response.json()


def api_post(base: str, path: str, payload: dict, timeout: int):
    response = requests.post(f"{base}{path}", json=payload, timeout=timeout)
    response.raise_for_status()
    return response.json()


def error_detail(exc: requests.RequestException) -> str:
    """FastAPI's `detail` if there is one -- it is written for a human."""
    response = getattr(exc, "response", None)
    if response is not None:
        try:
            detail = response.json().get("detail")
        except (ValueError, AttributeError):
            detail = None
        if isinstance(detail, str) and detail:
            return detail
        if isinstance(detail, list):
            # a 422: detail is a list of validation errors, and dumping the raw
            # dicts into a banner shows the reader Python instead of a problem
            return "; ".join(
                str(item.get("msg", item)) if isinstance(item, dict) else str(item)
                for item in detail
            ) or str(exc)
        # An unhandled exception in the backend: FastAPI returns the bare string
        # "Internal Server Error" with no JSON body, so there is nothing here
        # written for a reader. Say where the real message is instead of
        # forwarding a URL and a status code that explain nothing.
        if response.status_code >= 500:
            return (
                f"the backend raised an unhandled error ({response.status_code}). "
                "The traceback is in the uvicorn console, not here."
            )
    return str(exc)


# Streamlit reruns this whole script on every widget interaction. Uncached,
# each click cost two collection scans and a paginated call to Confluence.
@st.cache_data(ttl=300, show_spinner=False)
def fetch_spaces(base: str) -> list[dict]:
    return api_get(base, "/api/confluence/spaces")["spaces"]


@st.cache_data(ttl=15, show_spinner=False)
def fetch_status(base: str) -> dict:
    return api_get(base, "/api/confluence/status")


@st.cache_data(ttl=300, show_spinner=False)
def fetch_channels(base: str) -> list[dict]:
    """Channels the bot can read, with indexed counts.

    Cached like fetch_spaces: this costs a live Slack API call, and the app is
    rate-limited to roughly one call a minute -- an uncached call on every
    widget interaction would make the sidebar unusable.
    """
    return api_get(base, "/api/slack/channels")["channels"]


@st.cache_data(ttl=15, show_spinner=False)
def fetch_slack_total(base: str) -> int:
    """How many Slack chunks are indexed.

    Slack has no `/status` of its own, so ask the chunk pager for the total and
    throw the page away -- limit=1 keeps it from shipping the whole index back.
    """
    return api_get(base, "/api/slack/chunks?limit=1").get("total", 0)


def technical_detail(text: str) -> None:
    with st.expander("Technical detail"):
        st.code(text, language=None)


def render_citations(docs: list[dict]) -> None:
    """One compact line per cited document: where it came from, not what it said.

    The answer already quotes the relevant text; repeating the excerpt under it
    buries the answer. A reader needs the document name to judge the source and
    a link to go read it in full.

    Chunks are grouped by document, since top_k=4 is often four chunks of the
    same page -- one page listed four times looks like four sources. The
    markers stay attached so [2] in the answer is still findable.
    """
    if not docs:
        return

    grouped: dict[str, dict] = {}
    for marker, doc in enumerate(docs, start=1):
        meta = doc.get("metadata", {})
        # Confluence cites a page; Slack cites a channel
        name = (
            meta.get("title")
            or meta.get("channel")
            or meta.get("channel_id")
            or doc.get("id", "chunk")
        )
        key = meta.get("url") or meta.get("doc_id") or name
        entry = grouped.setdefault(
            key,
            {
                "name": name,
                "url": meta.get("url", ""),
                "where": meta.get("space_name") or meta.get("space_key")
                or meta.get("source", ""),
                "markers": [],
            },
        )
        entry["markers"].append(marker)

    lines = []
    for entry in grouped.values():
        markers = ", ".join(str(m) for m in entry["markers"])
        label = f"[{entry['name']}]({entry['url']})" if entry["url"] else f"**{entry['name']}**"
        suffix = f" · {entry['where']}" if entry["where"] else ""
        lines.append(f"\\[{markers}\\] {label}{suffix}")
    st.caption("Sources")
    st.markdown("  \n".join(lines))


# --- sidebar: connection, sources, ingestion ------------------------------

with st.sidebar:
    st.title("OnboardAI")

    # Two separate failures, two separate messages: the API can be perfectly
    # healthy while Ollama -- which every chat and ingest call needs -- is down.
    health = {}
    try:
        health = api_get(API_URL, "/api/health")
        ollama = health.get("ollama", {})
        online = health.get("status") == "ok"
        if online:
            st.success(f"Connected · {health['llm_model']}")
        elif not ollama.get("reachable", False):
            st.error("Ollama is not running")
            st.markdown(
                "Chat and ingest both need it. Start the **Ollama** app from "
                "Applications, or run `ollama serve` in a terminal, then reload "
                "this page."
            )
        else:
            missing = [
                ollama[key]
                for key, flag in (
                    ("llm_model", "llm_available"),
                    ("embedding_model", "embedding_available"),
                )
                if not ollama.get(flag)
            ]
            st.error("Model not pulled: " + ", ".join(f"`ollama pull {m}`" for m in missing))
        # the raw detail is a developer string (ConnectError, errno...) -- keep
        # it available for debugging, but never as the user-facing message
        if not online and ollama.get("detail"):
            technical_detail(ollama["detail"])
        # top_k is a server setting (TOP_K in .env), shown here rather than
        # offered as a control: it trades context budget against breadth, which
        # is an operator decision, not a per-question one
        st.caption(
            f"vector backend: {health['vector_backend']} · "
            f"top_k: {health.get('top_k', '?')}"
        )
    except requests.RequestException as exc:
        st.error("Backend not reachable")
        st.markdown(
            f"Nothing is answering at `{API_URL}`. Start it with "
            "`uvicorn app.main:app --reload --reload-dir app`, then reload."
        )
        technical_detail(str(exc))
        online = False

    available = []
    if online:
        try:
            available = api_get(API_URL, "/api/chat/sources")["sources"]
        except requests.RequestException as exc:
            st.warning(f"Could not list sources: {error_detail(exc)}")

    selected_sources = st.multiselect(
        "Sources",
        available or ["confluence"],
        default=available or ["confluence"],
        format_func=lambda s: SOURCE_LABELS.get(s, s.title()),
        disabled=not available,
        help="Every selected source is searched; the answer cites all of them.",
    )

    # --- spaces ----------------------------------------------------------
    # Confluence organises a project's pages into spaces. Picking them from a
    # list beats typing keys: the key is a short code (/wiki/spaces/<KEY>/)
    # that is easy to get wrong, and a wrong one fails silently as "no pages".
    spaces = []
    if online and "confluence" in selected_sources:
        try:
            spaces = fetch_spaces(API_URL)
        except requests.RequestException as exc:
            st.warning(f"Could not list Confluence spaces: {error_detail(exc)}")

    def space_label(key: str) -> str:
        space = next((s for s in spaces if s["key"] == key), {})
        name = space.get("name")
        indexed = space.get("indexed_chunks", 0)
        return f"{key}{f' — {name}' if name else ''}" + (
            f" ({indexed} chunks)" if indexed else " (not indexed)"
        )

    space_keys = []
    if spaces:
        space_keys = st.multiselect(
            "Confluence spaces",
            [s["key"] for s in spaces],
            format_func=space_label,
            help="Blank searches every indexed space.",
        )
    elif "confluence" in selected_sources and online:
        st.caption("No Confluence spaces visible yet — run an ingest.")

    st.divider()
    st.subheader("Ingestion")
    full_refresh = st.checkbox(
        "Full refresh",
        help="Re-index everything. Otherwise only documents changed since the "
             "last sync are fetched, and unchanged ones are skipped.",
    )

    # Each source is scoped by something different -- Confluence by space,
    # Slack by channel -- so each gets its own control, shown only when that
    # source is selected. One shared box would mean two different things
    # depending on what happened to be ticked.
    ingest_spaces = []
    if "confluence" in selected_sources:
        ingest_spaces = st.multiselect(
            "Spaces to ingest",
            [s["key"] for s in spaces],
            default=space_keys,
            format_func=space_label,
            disabled=not spaces,
            help="Blank uses CONFLUENCE_SPACE_KEYS from .env.",
        )

    # Same reasoning as the space picker: a typed channel name the bot cannot
    # see fails the whole ingest with "No matching Slack channels", so offer
    # only channels that actually exist and are readable.
    slack_channels: list[str] = []
    if "slack" in selected_sources:
        st.caption(
            "Slack ingest is rate-limited by the workspace app, so it can take "
            "many minutes. Leave this tab open until it reports back."
        )
        channels = []
        try:
            channels = fetch_channels(API_URL)
        except requests.RequestException as exc:
            st.warning(f"Could not list Slack channels: {error_detail(exc)}")

        def channel_label(name: str) -> str:
            ch = next((c for c in channels if c["name"] == name), {})
            indexed = ch.get("indexed_chunks", 0)
            private = " \N{LOCK}" if ch.get("is_private") else ""
            return f"#{name}{private}" + (
                f" ({indexed} chunks)" if indexed else " (not indexed)"
            )

        # Only channels the bot has joined can be read: conversations.history
        # on any other fails with not_in_channel and takes the whole ingest
        # down with it. A channel with no id is one the index still holds but
        # Slack will not serve -- left, renamed, or leftover fixture data.
        # Neither belongs in a picker that drives an ingest.
        readable = [c for c in channels if c.get("id") and c.get("is_member")]
        uninvited = [c for c in channels if c.get("id") and not c.get("is_member")]
        orphans = [c for c in channels if not c.get("id")]

        if readable:
            slack_channels = st.multiselect(
                "Slack channels",
                [c["name"] for c in readable],
                format_func=channel_label,
                help="Blank uses SLACK_CHANNELS from .env. Only channels the "
                     "bot has been invited to can be read.",
            )
        elif channels:
            st.caption(
                "No readable Slack channels. Invite the bot with "
                "`/invite @<app>` in the channel you want indexed."
            )

        if uninvited:
            st.caption(
                "Bot not in: "
                + ", ".join(f"#{c['name']}" for c in uninvited)
                + ". Run `/invite @OnboardIQ` in the channel to index it."
            )

        if orphans:
            st.caption(
                "Indexed but no longer readable: "
                + ", ".join(f"#{c['name']}" for c in orphans)
                + ". Run a full refresh with no channels selected to clear them."
            )

    def ingest_options(source: str) -> dict:
        """Per-source `options` for the ingest body."""
        if source == "confluence":
            return {"space_keys": ingest_spaces} if ingest_spaces else {}
        return {"channels": slack_channels} if slack_channels else {}

    def skip_reason(source: str) -> str | None:
        """Why this source is not worth a call, or None to go ahead.

        An empty Slack picker is not "ingest the .env default": Slack is
        throttled to about one API call a minute, so falling back to whatever
        SLACK_CHANNELS happens to hold can spend many minutes on channels
        nobody asked for. Confluence keeps the blank-means-.env default --
        it is a handful of fast calls and the fallback is the documented way
        to run it unattended.
        """
        if source == "slack" and not slack_channels:
            return "no channels selected"
        return None

    def report_ingest(source: str, result: dict) -> None:
        """Turn one IngestResponse into the shortest true sentence about it."""
        detail = result.get("detail") or ""
        ingested = result["documents_ingested"]
        skipped = result.get("documents_skipped", 0)
        label = SOURCE_LABELS.get(source, source.title())
        # nothing seen at all on a full window means the query matched nothing
        # anywhere -- for Confluence that is almost always a mistyped space
        # key, and it is distinguishable from "nothing changed", which skips
        if not ingested and not skipped and "since=beginning" in detail:
            if source == "confluence":
                st.warning(
                    "No pages found. Check the space **key** -- the short "
                    "code in /wiki/spaces/<KEY>/, not the display name."
                )
            else:
                st.warning(f"{label}: nothing found to index.")
        elif not ingested:
            # the count is only worth printing when the source reports one;
            # not every service populates documents_skipped
            st.info(
                f"{label} already up to date"
                + (f" — {skipped} unchanged." if skipped else ".")
            )
        else:
            st.success(
                f"{label}: {ingested} docs, {result['chunks_indexed']} chunks"
                + (f" · {skipped} unchanged" if skipped else "")
            )
        purged = result.get("documents_purged", 0)
        if purged:
            st.caption(f"Purged {purged} document(s) deleted in {label}.")
        st.caption(detail)

    if st.button(
        "Run ingest",
        disabled=not online or not selected_sources,
        use_container_width=True,
        help="Ingests every selected source.",
    ):
        for source in selected_sources:
            skipped_because = skip_reason(source)
            if skipped_because:
                st.info(
                    f"{SOURCE_LABELS.get(source, source.title())} skipped "
                    f"— {skipped_because}."
                )
                continue
            with st.spinner(
                f"Ingesting {SOURCE_LABELS.get(source, source)} -- this embeds "
                "every chunk and can take a while..."
            ):
                try:
                    report_ingest(
                        source,
                        api_post(
                            API_URL,
                            f"/api/{source}/ingest",
                            {"full_refresh": full_refresh, "options": ingest_options(source)},
                            SLACK_INGEST_TIMEOUT if source == "slack" else INGEST_TIMEOUT,
                        ),
                    )
                except requests.RequestException as exc:
                    # one source failing should not skip the rest
                    st.error(f"{source} ingest failed — {error_detail(exc)}")
                    technical_detail(str(exc))
        # the index changed, so every cached count is stale as of now
        fetch_spaces.clear()
        fetch_status.clear()
        fetch_slack_total.clear()
        fetch_channels.clear()

    if online and "confluence" in selected_sources:
        try:
            status = fetch_status(API_URL)
            synced = status.get("last_synced_at") or "never"
            st.caption(
                f"Confluence: {status['pages']} pages / {status['chunks']} chunks · "
                f"last sync: {synced}"
            )
        except requests.RequestException:
            pass  # the index summary is a nicety; never block the UI on it

    if online and "slack" in selected_sources:
        try:
            total = fetch_slack_total(API_URL)
            st.caption(
                f"Slack: {total} chunks" if total
                else "Slack: nothing indexed yet — run an ingest."
            )
        except requests.RequestException:
            pass

    if st.button("Clear chat", use_container_width=True):
        st.session_state.messages = []
        st.rerun()


# --- main pane: chat ------------------------------------------------------

st.session_state.setdefault("messages", [])

for message in st.session_state.messages:
    with st.chat_message(message["role"]):
        st.markdown(message["content"])
        render_citations(message.get("sources", []))

question = st.chat_input(
    "Ask about onboarding...", disabled=not online or not selected_sources
)
if not selected_sources and online:
    st.info("Select at least one source in the sidebar to ask a question.")

if question:
    st.session_state.messages.append({"role": "user", "content": question})
    with st.chat_message("user"):
        st.markdown(question)

    with st.chat_message("assistant"):
        with st.spinner("Thinking..."):
            try:
                # one fan-out call: the backend queries every selected source
                # and fuses the hits, so top_k stays a server-side budget
                result = api_post(
                    API_URL,
                    "/api/chat",
                    {
                        "question": question,
                        "sources": selected_sources,
                        "space_keys": space_keys,
                    },
                    CHAT_TIMEOUT,
                )
                answer, docs = result["answer"], result.get("sources", [])
                failure = ""
            except requests.RequestException as exc:
                answer, docs, failure = (
                    "Sorry -- I could not reach the backend to answer that.",
                    [],
                    error_detail(exc),
                )
        st.markdown(answer)
        if failure:
            technical_detail(failure)
        elif not docs:
            st.caption("No excerpt was close enough to the question to cite.")

    st.session_state.messages.append(
        {"role": "assistant", "content": answer, "sources": docs}
    )
    # rerun so the history loop above renders this turn's citations
    st.rerun()
