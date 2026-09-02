from fastapi import APIRouter, Depends

from app.schemas.chat import ChatRequest, ChatResponse
from app.schemas.ingest import IngestRequest, IngestResponse
from app.services.common.vectorstore import list_chunks
from app.services.confluence import ConfluenceService, get_confluence_service

router = APIRouter(prefix="/confluence", tags=["confluence"])


@router.get("/chunks")
async def chunks(
    limit: int = 50,
    offset: int = 0,
    service: ConfluenceService = Depends(get_confluence_service),
) -> dict:
    """Page through what is actually indexed. Chroma has no UI of its own."""
    return list_chunks(service.collection, limit=limit, offset=offset)


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
