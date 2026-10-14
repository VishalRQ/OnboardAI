"""Streamlit UI for the OnboardAI backend.

A thin HTTP client over the FastAPI app -- no RAG logic lives here, so the
backend stays the single source of truth. Run the two side by side:

    uvicorn app.main:app --reload --reload-dir app
    streamlit run streamlit_app.py

One page: the chat in the main pane; what to search, and the button that
brings the index up to date, in the sidebar.
"""

import os
import time

import requests
import streamlit as st
from dotenv import load_dotenv

# same .env the backend reads, so the two are configured in one place
load_dotenv()

#: where the FastAPI app lives. Deployment config, not a user choice -- set
#: ONBOARDAI_API_URL in .env rather than exposing it as a field anyone can
#: point at an arbitrary host.
API_URL = os.getenv("ONBOARDAI_API_URL", "http://localhost:8000").rstrip("/")
CHAT_TIMEOUT = 300
#: how often the sidebar asks whether a background update has finished
JOB_POLL_SECONDS = 3
#: sources that can be searched together; the backend is the authority, this
#: only decides the label and the order they appear in
SOURCE_LABELS = {"confluence": "Confluence", "slack": "Slack"}
#: per-source badge colour and icon, so a citation says where it came from at
#: a glance instead of in a trailing word
SOURCE_STYLE = {
    "confluence": ("blue", ":material/menu_book:"),
    "slack": ("violet", ":material/forum:"),
}
#: starter questions for an empty chat. A new hire does not know what the index
#: holds; one click on a real question shows the shape of an answer.
SUGGESTIONS = [
    "How do I set up my dev environment?",
    "How do I get access to the staging database?",
    "What's the deploy process for a backend service?",
    "Where are the API docs?",
]
USER_AVATAR = ":material/person:"
ASSISTANT_AVATAR = ":material/explore:"

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


@st.cache_data(ttl=300, show_spinner=False)
def fetch_channels(base: str) -> list[dict]:
    """Channels the bot can read, with indexed counts.

    Cached like fetch_spaces: this costs a live Slack API call, and the app is
    rate-limited to roughly one call a minute -- an uncached call on every
    widget interaction would make the sidebar unusable.
    """
    return api_get(base, "/api/slack/channels")["channels"]


def clear_index_caches() -> None:
    """The index changed, so every cached count is stale as of now."""
    fetch_spaces.clear()
    fetch_channels.clear()


def technical_detail(text: str) -> None:
    with st.expander("Technical detail"):
        st.code(text, language=None)


def label(source: str) -> str:
    return SOURCE_LABELS.get(source, source.title())


def badge(source: str) -> str:
    color, icon = SOURCE_STYLE.get(source, ("gray", ":material/description:"))
    return f":{color}-badge[{icon} {label(source)}]"


def link_text(text: str, limit: int = 80) -> str:
    """Make a document name safe to sit inside a markdown link.

    A Slack title is the thread's first line, verbatim: backticks start a code
    span that swallows the link, and brackets end the link text early.
    """
    text = " ".join(text.replace("`", "").replace("[", "(").replace("]", ")").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


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
                "source": meta.get("source", ""),
                # a Confluence page lives in a space; a Slack thread is placed
                # by its date, since the channel is already in the name
                "where": meta.get("space_name") or meta.get("space_key")
                or (meta.get("created_at") or "")[:10],
                "markers": [],
            },
        )
        entry["markers"].append(marker)

    lines = []
    for entry in grouped.values():
        markers = ", ".join(str(m) for m in entry["markers"])
        name = link_text(entry["name"])
        link = f"[{name}]({entry['url']})" if entry["url"] else f"**{name}**"
        suffix = f" · :gray[{entry['where']}]" if entry["where"] else ""
        lines.append(f"{badge(entry['source'])} \\[{markers}\\] {link}{suffix}")

    with st.container(border=True):
        st.caption(f"Sources · {len(grouped)}")
        st.markdown("  \n".join(lines))


def render_answer_extras(message: dict) -> None:
    """Everything under an answer. Stored on the message, not drawn once.

    The chat reruns as soon as a turn finishes so the history loop owns all
    rendering -- anything drawn only during the turn (a failure's detail, the
    "nothing to cite" note) vanished on that rerun before anyone could read it.
    """
    failure = message.get("failure")
    if failure:
        technical_detail(failure)
    docs = message.get("sources", [])
    if docs:
        render_citations(docs)
    elif not failure:
        st.caption("No excerpt was close enough to the question to cite.")
    facts = []
    if message.get("elapsed") is not None:
        facts.append(f":material/schedule: {message['elapsed']:.1f}s")
    if message.get("searched"):
        facts.append("searched " + ", ".join(label(s) for s in message["searched"]))
    if facts:
        st.caption(" · ".join(facts))


def space_name(space: dict) -> str:
    """How a reader knows a space: its name, falling back to its key.

    A personal space is keyed by the owner's account id (~7120...), forty
    characters that identify nothing and push every other column off screen.
    """
    key, name = space["key"], space.get("name")
    if key.startswith("~"):
        return name or "Personal space"
    return f"{key} — {name}" if name and name != key else key


# --- background updates -----------------------------------------------------
# Update starts one backend job per source and returns at once, so the chat
# stays usable while Slack crawls through its rate limit. A fragment polls the
# jobs every few seconds and pops a toast as each finishes.

def notify(text: str, icon: str, sticky: bool = False) -> None:
    """Queue a toast for the next full run.

    Queued, not shown: a toast raised just before st.rerun() is lost with the
    rerun. `sticky` ones -- failures and warnings worth acting on -- also stay
    in the sidebar until the next Update, since a toast is gone in seconds.
    """
    st.session_state.setdefault("toasts", []).append((text, icon))
    if sticky:
        st.session_state.setdefault("update_notes", []).append((text, icon))


def job_summary(job: dict) -> tuple[str, str, bool]:
    """A finished job as (message, icon, sticky)."""
    name = label(job["source"])
    if job["status"] == "failed":
        return (
            f"{name} update failed — {job.get('error') or 'unknown error'}",
            ":material/error:", True,
        )
    result = job.get("result") or {}
    ingested = result.get("documents_ingested", 0)
    skipped = result.get("documents_skipped", 0)
    # nothing seen at all over a full window means the query matched nothing
    # anywhere -- for Confluence almost always a mistyped space key -- which
    # is not the same as "nothing changed"
    if not ingested and not skipped and "since=beginning" in (result.get("detail") or ""):
        hint = " Check the space key." if job["source"] == "confluence" else ""
        return f"{name}: nothing found to index.{hint}", ":material/warning:", True
    if ingested:
        text = f"{name} updated — {ingested} docs, {result.get('chunks_indexed', 0)} chunks"
    else:
        text = f"{name} is already up to date"
    purged = result.get("documents_purged", 0)
    if purged:
        text += f", {purged} deleted at the source removed"
    return text, ":material/check_circle:", False


def watch(job: dict) -> None:
    st.session_state.setdefault("watched_jobs", {})[job["id"]] = job["source"]


def start_updates(plan: dict[str, dict], full_refresh: bool) -> None:
    """Start a background job per planned source, then return at once."""
    st.session_state.update_notes = []
    for source, options in plan.items():
        try:
            watch(api_post(
                API_URL,
                f"/api/jobs/ingest/{source}",
                {"full_refresh": full_refresh, "options": options},
                timeout=30,
            ))
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 409:
                # started elsewhere -- another tab, another person; the watcher
                # adopts running jobs, so this one still reports back here
                notify(f"{label(source)} is already updating.", ":material/sync:")
            else:
                notify(f"{label(source)} update failed — {error_detail(exc)}",
                       ":material/error:", sticky=True)
        except requests.RequestException as exc:
            notify(f"{label(source)} update failed — {error_detail(exc)}",
                   ":material/error:", sticky=True)
    st.rerun()


def adopt_running_jobs() -> None:
    """Follow every update the backend is running, not only this tab's.

    After a page reload, or when someone else pressed Update, the job is still
    worth reporting -- and an Update click for it would only get a 409.
    """
    try:
        for job in api_get(API_URL, "/api/jobs?status=running", timeout=5)["jobs"]:
            watch(job)
    except requests.RequestException:
        pass  # the watcher is a nicety; never block the sidebar on it


@st.fragment(run_every=JOB_POLL_SECONDS)
def job_watcher() -> None:
    """Poll the watched jobs; on any finishing, toast it and rerun the app.

    Only rendered while something is watched, so polling stops by itself once
    the last job reports back.
    """
    watched: dict = st.session_state.get("watched_jobs", {})
    finished = []
    for job_id, source in list(watched.items()):
        try:
            job = api_get(API_URL, f"/api/jobs/{job_id}", timeout=5)
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                # jobs live in the backend's memory; a restart loses them
                finished.append(job_id)
                notify(f"{label(source)} update was interrupted by a backend "
                       "restart. Press Update again.", ":material/error:", sticky=True)
            continue
        except requests.RequestException:
            continue  # backend briefly unreachable; try again next tick
        if job["status"] == "running":
            st.caption(f":material/progress_activity: Updating {label(source)} in the background...")
        else:
            finished.append(job_id)
            notify(*job_summary(job))
    if finished:
        for job_id in finished:
            watched.pop(job_id, None)
        # the index changed: fresh chunk counts in the pickers, and the toasts
        clear_index_caches()
        st.rerun()


for text, icon in st.session_state.pop("toasts", []):
    st.toast(text, icon=icon, duration="long")


# --- sidebar: connection and what to search -------------------------------
# Lives in the entrypoint, not a page, so the choices survive page switches.

with st.sidebar:
    st.title("🧭 OnboardAI")
    st.caption("Answers from your team's docs and conversations.")

    # Two separate failures, two separate messages: the API can be perfectly
    # healthy while Ollama -- which every chat and ingest call needs -- is down.
    health = {}
    try:
        health = api_get(API_URL, "/api/health")
        ollama = health.get("ollama", {})
        online = health.get("status") == "ok"
        if online:
            st.badge(
                f"Connected · {health['llm_model']}",
                icon=":material/check_circle:",
                color="green",
            )
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

    selected_sources = st.pills(
        "Search in",
        available or list(SOURCE_LABELS),
        selection_mode="multi",
        default=available or None,
        format_func=lambda s: f"{SOURCE_STYLE.get(s, ('', ''))[1]} {label(s)}",
        disabled=not available,
        key="selected_sources",
        help="Every selected source is searched; the answer cites all of them.",
    ) or []

    # --- spaces ----------------------------------------------------------
    # Confluence organises a project's pages into spaces. Picking them from a
    # list beats typing keys: the key is a short code (/wiki/spaces/<KEY>/)
    # that is easy to get wrong, and a wrong one fails silently as "no pages".
    spaces = []
    if online and "confluence" in available:
        try:
            spaces = fetch_spaces(API_URL)
        except requests.RequestException as exc:
            st.warning(f"Could not list Confluence spaces: {error_detail(exc)}")

    def space_label(key: str) -> str:
        space = next((s for s in spaces if s["key"] == key), {})
        name = space.get("name")
        indexed = space.get("indexed_chunks", 0)
        return f"{space_name(space or {'key': key})}" + (
            f" ({indexed} chunks)" if indexed else " (not indexed)"
        )

    space_keys = []
    if "confluence" in selected_sources:
        if spaces:
            space_keys = st.multiselect(
                "Confluence spaces",
                [s["key"] for s in spaces],
                format_func=space_label,
                placeholder="All indexed spaces",
                key="space_keys",
                help="Blank searches every indexed space.",
            )
        elif online:
            st.caption("No Confluence spaces visible yet — run an ingest.")

    # --- channels --------------------------------------------------------
    # Same picker for Slack, listed live from the workspace. Only channels the
    # bot is in: those are the ones it can read, so the only ones an update
    # can refresh -- anything else in the list would be a dead end.
    channels: list[dict] = []
    if online and "slack" in available:
        try:
            channels = fetch_channels(API_URL)
        except requests.RequestException as exc:
            st.warning(f"Could not list Slack channels: {error_detail(exc)}")
    joined = {c["name"] for c in channels if c.get("id") and c.get("is_member")}

    def channel_label(name: str) -> str:
        ch = next((c for c in channels if c["name"] == name), {})
        indexed = ch.get("indexed_chunks", 0)
        text = f"#{name}" + (" \N{LOCK}" if ch.get("is_private") else "")
        return text + (f" ({indexed} chunks)" if indexed else " (not indexed)")

    slack_channels: list[str] = []
    if "slack" in selected_sources:
        if joined:
            slack_channels = st.multiselect(
                "Slack channels",
                sorted(joined),
                format_func=channel_label,
                placeholder="All channels",
                key="slack_channels",
                help="Blank searches every indexed channel and updates every "
                     "channel the bot is in. `/invite` the bot to add one.",
            )
        elif online:
            st.caption("The bot is in no Slack channel. `/invite` it to one to index it.")

    # --- update ------------------------------------------------------------
    # Which sources to re-index is its own choice, not "Search in": a reader
    # may search both while only Confluence needs refreshing, and Slack costs
    # minutes of rate-limited calls. Scope per source: the Confluence spaces
    # picked above (blank = every space in Confluence), and every Slack
    # channel the bot has joined. Membership is the Slack allowlist --
    # inviting the bot is how a channel opts in, and conversations.history
    # fails on any channel it is not in.
    if online and available:
        st.divider()
        st.caption("Update index")
        with st.container(horizontal=True):
            update_sources = [
                source for source in available
                if st.checkbox(
                    label(source),
                    # Slack is opt-in per run: it is the slow one
                    value=source != "slack",
                    key=f"update_{source}",
                    help="Rate-limited: can take several minutes."
                    if source == "slack" else None,
                )
            ]

        # Each ticked source gets its own picker for what to refresh, separate
        # from what to search: with hundreds of spaces or channels "update
        # everything" is slow (Slack runs about a call a minute), and the
        # handful that changed are rarely the ones being searched. Blank
        # still means all.
        plan: dict[str, dict] = {}
        if "confluence" in update_sources:
            picked = st.multiselect(
                "Spaces to update",
                [s["key"] for s in spaces],
                format_func=space_label,
                placeholder="All spaces",
                key="update_space_keys",
                help="Blank updates every space in Confluence.",
            )
            plan["confluence"] = {"space_keys": picked} if picked else {}
        if "slack" in update_sources:
            if joined:
                picked = st.multiselect(
                    "Channels to update",
                    sorted(joined),
                    format_func=channel_label,
                    placeholder="All channels",
                    key="update_channels",
                    help="Blank updates every channel the bot is in.",
                )
                # no list sent for "all": the backend resolves the bot's
                # channels itself, and a full refresh then reconciles the
                # whole collection, dropping channels the bot has left
                plan["slack"] = {"channels": picked} if picked else {}
            else:
                st.caption("Slack: the bot is in no channel. `/invite` it to one.")

        full_refresh = st.checkbox(
            "Full refresh",
            help="Re-index everything and drop what was deleted at the source. "
                 "Otherwise only what changed since the last update is fetched.",
        )
        if st.button(
            "Update",
            icon=":material/sync:",
            width="stretch",
            disabled=not plan,
            help="Runs in the background: keep chatting, and a popup says when "
                 "each source is done.",
        ):
            start_updates(plan, full_refresh)
        adopt_running_jobs()
        if st.session_state.get("watched_jobs"):
            job_watcher()
        for text, icon in st.session_state.get("update_notes", []):
            (st.error if icon == ":material/error:" else st.warning)(text, icon=icon)

    st.divider()
    if st.button(
        "New chat",
        icon=":material/add_comment:",
        width="stretch",
        disabled=not st.session_state.get("messages"),
    ):
        st.session_state.messages = []
        st.rerun()


# --- chat -------------------------------------------------------------------

def welcome(can_ask: bool) -> None:
    st.caption("Try one of these")
    with st.container(horizontal=True):
        for suggestion in SUGGESTIONS:
            # a click queues the question and reruns, so the welcome panel is
            # gone by the time the answer is drawn
            if st.button(suggestion, icon=":material/arrow_outward:", disabled=not can_ask):
                st.session_state.pending_question = suggestion
                st.rerun()


def ask_page() -> None:
    st.session_state.setdefault("messages", [])
    messages = st.session_state.messages
    can_ask = online and bool(selected_sources)
    pending = st.session_state.pop("pending_question", None)

    if not messages and not pending:
        welcome(can_ask)

    for message in messages:
        avatar = USER_AVATAR if message["role"] == "user" else ASSISTANT_AVATAR
        with st.chat_message(message["role"], avatar=avatar):
            st.markdown(message["content"])
            if message["role"] == "assistant":
                render_answer_extras(message)

    question = st.chat_input(
        "Ask about onboarding..." if can_ask else "Select a source to ask a question",
        disabled=not can_ask,
    ) or pending
    if online and not selected_sources:
        st.info("Select at least one source in the sidebar to ask a question.")

    if not question:
        return

    messages.append({"role": "user", "content": question})
    with st.chat_message("user", avatar=USER_AVATAR):
        st.markdown(question)

    with st.chat_message("assistant", avatar=ASSISTANT_AVATAR):
        started = time.monotonic()
        with st.spinner(f"Searching {', '.join(label(s) for s in selected_sources)}..."):
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
                        "channels": slack_channels,
                    },
                    CHAT_TIMEOUT,
                )
                turn = {
                    "content": result["answer"],
                    "sources": result.get("sources", []),
                    # the sources that actually answered; a failed one is
                    # dropped here and named in a note on the answer itself
                    "searched": [s for s in result.get("source", "").split(", ") if s],
                }
            except requests.RequestException as exc:
                turn = {
                    "content": "Sorry -- I could not reach the backend to answer that.",
                    "failure": error_detail(exc),
                }

    messages.append(
        {"role": "assistant", "elapsed": time.monotonic() - started, **turn}
    )
    # rerun so the history loop above renders this turn's citations
    st.rerun()


ask_page()
