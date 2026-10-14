"""Background ingest jobs, with fake services -- no network, Chroma or Ollama.

Run: `pytest tests/test_jobs.py`
"""

import threading
import time

import pytest

from app.schemas.ingest import IngestRequest, IngestResponse
from app.services.common import jobs


class FakeService:
    """Ingest blocks until `release` is set, then succeeds or raises."""

    def __init__(self, name="fake", error: Exception | None = None):
        self.name = name
        self.error = error
        self.release = threading.Event()

    async def ingest(self, request):
        self.release.wait(timeout=5)
        if self.error:
            raise self.error
        return IngestResponse(source=self.name, documents_ingested=2, chunks_indexed=3)


@pytest.fixture(autouse=True)
def clean_registry():
    jobs._jobs.clear()
    yield
    jobs._jobs.clear()


def wait_until_finished(job_id: str) -> jobs.Job:
    for _ in range(100):
        job = jobs.get(job_id)
        if job.status != "running":
            return job
        time.sleep(0.02)
    raise AssertionError("job never finished")


def test_job_returns_at_once_then_reports_its_result():
    service = FakeService()
    job = jobs.start(service, IngestRequest())
    # still running: start() did not wait for the ingest
    assert jobs.get(job.id).status == "running"

    service.release.set()
    done = wait_until_finished(job.id)
    assert done.status == "done"
    assert done.result["documents_ingested"] == 2
    assert done.finished_at


def test_second_update_of_the_same_source_is_refused():
    service = FakeService()
    first = jobs.start(service, IngestRequest())
    with pytest.raises(jobs.JobAlreadyRunning) as refused:
        jobs.start(service, IngestRequest())
    assert refused.value.job.id == first.id
    service.release.set()
    wait_until_finished(first.id)
    # finished, so a new one may start
    service.release.set()
    assert jobs.start(service, IngestRequest()).id != first.id


def test_different_sources_run_side_by_side():
    a, b = FakeService("a"), FakeService("b")
    ja, jb = jobs.start(a, IngestRequest()), jobs.start(b, IngestRequest())
    assert {j.source for j in jobs.list_jobs("running")} == {"a", "b"}
    a.release.set(), b.release.set()
    wait_until_finished(ja.id), wait_until_finished(jb.id)


def test_failure_is_recorded_as_a_sentence():
    service = FakeService(error=ValueError("No matching Slack channels"))
    service.release.set()
    job = wait_until_finished(jobs.start(service, IngestRequest()).id)
    assert job.status == "failed"
    assert job.error == "No matching Slack channels"


def test_slack_error_code_becomes_the_message():
    class SlackApiError(Exception):
        response = {"error": "not_in_channel", "ok": False}

    assert jobs.describe(SlackApiError("long raw response dump")).startswith(
        "Slack refused the request: not_in_channel"
    )


def test_old_finished_jobs_are_pruned(monkeypatch):
    monkeypatch.setattr(jobs, "KEEP_FINISHED", 2)
    for n in range(4):
        service = FakeService(f"s{n}")
        service.release.set()
        wait_until_finished(jobs.start(service, IngestRequest()).id)
    jobs.start(FakeService("last"), IngestRequest())  # pruning happens on start
    finished = [j for j in jobs.list_jobs() if j.status != "running"]
    assert len(finished) <= 2
