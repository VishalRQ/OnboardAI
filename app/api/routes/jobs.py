"""Background ingest: start an update, then poll it.

`POST /api/{source}/ingest` still runs an ingest to completion in the request,
for scripts and a cron job that want the result in one call. These endpoints
are for the UI, which should not sit on an open request for minutes.
"""

from fastapi import APIRouter, HTTPException

from app.schemas.ingest import IngestRequest
from app.services.common import jobs
from app.services.registry import get_services

router = APIRouter(prefix="/jobs", tags=["jobs"])


@router.post("/ingest/{source}", status_code=202)
async def start_ingest(source: str, request: IngestRequest) -> dict:
    """Start updating `source` in the background. 409 if it already is."""
    service = get_services().get(source)
    if service is None:
        raise HTTPException(status_code=404, detail=f"Unknown source: {source}")
    try:
        return jobs.start(service, request).to_dict()
    except jobs.JobAlreadyRunning as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("")
async def list_jobs(status: str | None = None) -> dict:
    """Recent jobs, newest first. `?status=running` for what is in flight."""
    return {"jobs": [job.to_dict() for job in jobs.list_jobs(status)]}


@router.get("/{job_id}")
async def get_job(job_id: str) -> dict:
    job = jobs.get(job_id)
    if job is None:
        # unknown, or lost with a backend restart -- either way it is not running
        raise HTTPException(status_code=404, detail=f"No such job: {job_id}")
    return job.to_dict()
