"""Source-agnostic chat: pick a service by name, or fan out later."""

from fastapi import APIRouter, HTTPException

from app.schemas.chat import ChatRequest, ChatResponse
from app.services.common.base import BaseRAGService
from app.services.confluence import get_confluence_service
from app.services.slack import get_slack_service

router = APIRouter(prefix="/chat", tags=["chat"])


def _registry() -> dict[str, BaseRAGService]:
    return {
        "confluence": get_confluence_service(),
        "slack": get_slack_service(),
    }


@router.get("/sources")
async def list_sources() -> dict:
    return {"sources": sorted(_registry())}


@router.post("/{source}", response_model=ChatResponse)
async def chat(source: str, request: ChatRequest) -> ChatResponse:
    service = _registry().get(source)
    if service is None:
        raise HTTPException(status_code=404, detail=f"Unknown source: {source}")
    return await service.chat(request)
