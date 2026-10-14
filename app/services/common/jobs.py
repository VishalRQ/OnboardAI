"""Background ingest jobs.

An ingest takes seconds (Confluence) to many minutes (Slack, throttled to about
a call a minute). Held open as one HTTP request, it pins the caller until it
finishes and is lost if the browser tab goes away. A job returns at once and
runs in its own thread; callers poll it for the outcome.

In memory on purpose: there is one backend process, and a job that dies with it
is simply gone -- the next update starts over, which incremental sync makes
cheap. One job per source at a time, so two updates never write the same
collection at once.
"""

import asyncio
import threading
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

from app.core.logging import get_logger
from app.schemas.ingest import IngestRequest
from app.services.common.base import BaseRAGService

logger = get_logger(__name__)

#: finished jobs kept for polling; older ones are dropped so memory stays flat
KEEP_FINISHED = 50


@dataclass
class Job:
    id: str
    source: str
    full_refresh: bool
    started_at: str
    status: str = "running"  # running | done | failed
    finished_at: str | None = None
    #: the IngestResponse, once done
    result: dict | None = None
    #: why it failed, written for a reader
    error: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


class JobAlreadyRunning(RuntimeError):
    def __init__(self, job: Job):
        self.job = job
        super().__init__(f"{job.source} is already updating (job {job.id})")


_jobs: dict[str, Job] = {}
_lock = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def describe(exc: Exception) -> str:
    """A failure as a sentence. Slack's errors carry a code that names the fix;
    their str() is a dump of the whole response."""
    response = getattr(exc, "response", None)
    code = response.get("error") if hasattr(response, "get") else None
    if isinstance(code, str):
        message = f"Slack refused the request: {code}"
        if code == "not_in_channel":
            message += ". Invite the bot with /invite @<app> in that channel."
        return message
    return str(exc) or type(exc).__name__


def start(service: BaseRAGService, request: IngestRequest) -> Job:
    """Start an ingest of `service` in the background and return its job."""
    with _lock:
        running = next(
            (j for j in _jobs.values() if j.source == service.name and j.status == "running"),
            None,
        )
        if running:
            raise JobAlreadyRunning(running)
        job = Job(
            id=uuid.uuid4().hex,
            source=service.name,
            full_refresh=request.full_refresh,
            started_at=_now(),
        )
        _jobs[job.id] = job
        _prune()
    threading.Thread(
        target=_run, args=(job, service, request), name=f"ingest-{service.name}", daemon=True
    ).start()
    logger.info("Started %s ingest job %s", service.name, job.id)
    return job


def _run(job: Job, service: BaseRAGService, request: IngestRequest) -> None:
    try:
        # a fresh event loop in this thread: ingest is a coroutine, and keeping
        # it off the server's loop is the whole point
        result = asyncio.run(service.ingest(request))
        outcome = {"status": "done", "result": result.model_dump()}
        logger.info("%s ingest job %s done: %s", job.source, job.id, result.detail)
    except Exception as exc:  # a job must always end, whatever went wrong
        logger.exception("%s ingest job %s failed", job.source, job.id)
        outcome = {"status": "failed", "error": describe(exc)}
    with _lock:
        job.status = outcome["status"]
        job.result = outcome.get("result")
        job.error = outcome.get("error")
        job.finished_at = _now()


def _prune() -> None:
    """Drop the oldest finished jobs past KEEP_FINISHED. Caller holds _lock."""
    finished = sorted(
        (j for j in _jobs.values() if j.status != "running"), key=lambda j: j.started_at
    )
    for job in finished[: max(0, len(finished) - KEEP_FINISHED)]:
        del _jobs[job.id]


def get(job_id: str) -> Job | None:
    with _lock:
        return _jobs.get(job_id)


def list_jobs(status: str | None = None) -> list[Job]:
    """Newest first, optionally only those in `status`."""
    with _lock:
        jobs = [j for j in _jobs.values() if status is None or j.status == status]
    return sorted(jobs, key=lambda j: j.started_at, reverse=True)
