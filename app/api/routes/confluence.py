from fastapi import APIRouter, Depends, HTTPException

from app.schemas.chat import ChatRequest, ChatResponse
from app.schemas.ingest import IngestRequest, IngestResponse
from app.services.common.vectorstore import list_chunks
from app.services.confluence import ConfluenceService, get_confluence_service
from app.services.confluence.client import ConfluenceAuthError
from app.services.confluence.sync_state import SyncInProgress

router = APIRouter(prefix="/confluence", tags=["confluence"])


@router.get("/spaces")
async def spaces(service: ConfluenceService = Depends(get_confluence_service)) -> dict:
    """Spaces the UI can offer, each with its indexed chunk count."""
    try:
        return {"spaces": service.spaces()}
    except ConfluenceAuthError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/status")
async def status(service: ConfluenceService = Depends(get_confluence_service)) -> dict:
    """What is indexed and when it last synced."""
    return service.status()


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
    try:
        return await service.ingest(request)
    except SyncInProgress as exc:
        # 409, not 500: the request is valid, the resource is just busy, and a
        # scheduled caller should back off rather than retry hard
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ConfluenceAuthError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except ValueError as exc:  # e.g. no spaces configured or requested
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/chat", response_model=ChatResponse)
async def chat(
    request: ChatRequest,
    service: ConfluenceService = Depends(get_confluence_service),
) -> ChatResponse:
    return await service.chat(request)
