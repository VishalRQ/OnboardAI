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
    return str(exc)


# Streamlit reruns this whole script on every widget interaction. Uncached,
# each click cost two collection scans and a paginated call to Confluence.
@st.cache_data(ttl=300, show_spinner=False)
def fetch_spaces(base: str) -> list[dict]:
    return api_get(base, "/api/confluence/spaces")["spaces"]


@st.cache_data(ttl=15, show_spinner=False)
def fetch_status(base: str) -> dict:
    return api_get(base, "/api/confluence/status")


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
        help="Re-index every page. Otherwise only pages changed since the last "
             "sync are fetched, and unchanged ones are skipped.",
    )
    ingest_spaces = st.multiselect(
        "Spaces to ingest",
        [s["key"] for s in spaces],
        default=space_keys,
        format_func=space_label,
        disabled=not spaces,
        help="Blank uses CONFLUENCE_SPACE_KEYS from .env.",
    )

    if st.button(
        "Run ingest",
        disabled=not online or "confluence" not in selected_sources,
        use_container_width=True,
    ):
        options = {"space_keys": ingest_spaces} if ingest_spaces else {}
        with st.spinner("Ingesting -- this embeds every chunk and can take a while..."):
            try:
                result = api_post(
                    API_URL,
                    "/api/confluence/ingest",
                    {"full_refresh": full_refresh, "options": options},
                    INGEST_TIMEOUT,
                )
                # the index changed, so the cached space counts and status are
                # stale the moment this returns
                fetch_spaces.clear()
                fetch_status.clear()
                detail = result.get("detail") or ""
                ingested = result["documents_ingested"]
                skipped = result.get("documents_skipped", 0)
                # nothing seen at all on a full window means the query matched
                # no page anywhere -- almost always a mistyped space key, and
                # distinguishable from "nothing changed", which skips pages
                if not ingested and not skipped and "since=beginning" in detail:
                    st.warning(
                        "No pages found. Check the space **key** -- the short "
                        "code in /wiki/spaces/<KEY>/, not the display name."
                    )
                elif not ingested:
                    st.info(
                        f"Already up to date — {skipped} page(s) unchanged "
                        "since the last sync."
                    )
                else:
                    st.success(
                        f"{ingested} docs, {result['chunks_indexed']} chunks"
                        + (f" · {skipped} unchanged" if skipped else "")
                    )
                purged = result.get("documents_purged", 0)
                if purged:
                    st.caption(f"Purged {purged} page(s) deleted in Confluence.")
                st.caption(detail)
            except requests.RequestException as exc:
                st.error(f"Ingest failed — {error_detail(exc)}")
                technical_detail(str(exc))

    if online and "confluence" in selected_sources:
        try:
            status = fetch_status(API_URL)
            synced = status.get("last_synced_at") or "never"
            st.caption(
                f"indexed: {status['pages']} pages / {status['chunks']} chunks · "
                f"last sync: {synced}"
            )
        except requests.RequestException:
            pass  # the index summary is a nicety; never block the UI on it

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
