from fastapi import APIRouter, Depends

from app.schemas.chat import ChatRequest, ChatResponse
from app.schemas.ingest import IngestRequest, IngestResponse
from app.services.slack import SlackService, get_slack_service

router = APIRouter(prefix="/slack", tags=["slack"])


@router.post("/ingest", response_model=IngestResponse)
async def ingest(
    request: IngestRequest,
    service: SlackService = Depends(get_slack_service),
) -> IngestResponse:
    return await service.ingest(request)


@router.post("/chat", response_model=ChatResponse)
async def chat(
    request: ChatRequest,
    service: SlackService = Depends(get_slack_service),
) -> ChatResponse:
    return await service.chat(request)
