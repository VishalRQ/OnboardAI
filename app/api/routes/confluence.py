from fastapi import APIRouter, Depends

from app.schemas.chat import ChatRequest, ChatResponse
from app.schemas.ingest import IngestRequest, IngestResponse
from app.services.confluence import ConfluenceService, get_confluence_service

router = APIRouter(prefix="/confluence", tags=["confluence"])


@router.post("/ingest", response_model=IngestResponse)
async def ingest(
    request: IngestRequest,
    service: ConfluenceService = Depends(get_confluence_service),
) -> IngestResponse:
    return await service.ingest(request)


@router.post("/chat", response_model=ChatResponse)
async def chat(
    request: ChatRequest,
    service: ConfluenceService = Depends(get_confluence_service),
) -> ChatResponse:
    return await service.chat(request)
