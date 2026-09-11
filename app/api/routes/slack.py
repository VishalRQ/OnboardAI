from fastapi import APIRouter, Depends

from app.schemas.chat import ChatRequest, ChatResponse
from app.schemas.ingest import IngestRequest, IngestResponse
from app.services.common.vectorstore import list_chunks
from app.services.slack import SlackService, get_slack_service

router = APIRouter(prefix="/slack", tags=["slack"])


@router.get("/chunks")
async def chunks(
    limit: int = 50,
    offset: int = 0,
    service: SlackService = Depends(get_slack_service),
) -> dict:
    """Page through what is actually indexed. Chroma has no UI of its own."""
    return list_chunks(service.collection, limit=limit, offset=offset)


@router.post("/ingest", response_model=IngestResponse)
async def ingest(
    request: IngestRequest,
    service: SlackService = Depends(get_slack_service),
) -> IngestResponse:
    """Index Slack messages.

    Body `options`: `channels` (list of names/ids), `since` (Slack ts, live
    path only), `export_path` (override SLACK_EXPORT_PATH). `full_refresh`
    re-indexes every unit and reconciles deletions for the covered channels.
    """
    return await service.ingest(request)


@router.post("/chat", response_model=ChatResponse)
async def chat(
    request: ChatRequest,
    service: SlackService = Depends(get_slack_service),
) -> ChatResponse:
    return await service.chat(request)
