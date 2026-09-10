"""Source-agnostic chat: query one source, or fan out across several."""

import asyncio

from fastapi import APIRouter, HTTPException

from app.core.logging import get_logger
from app.schemas.chat import ChatRequest, ChatResponse, MultiChatRequest, SourceDocument
from app.services.common.base import BaseRAGService, resolve_top_k
from app.services.confluence import get_confluence_service
from app.services.slack import get_slack_service

logger = get_logger(__name__)

router = APIRouter(prefix="/chat", tags=["chat"])


def _registry() -> dict[str, BaseRAGService]:
    return {
        "confluence": get_confluence_service(),
        "slack": get_slack_service(),
    }


async def _retrieve(
    service: BaseRAGService, question: str, top_k: int, space_keys: list[str]
) -> list[SourceDocument]:
    """Retrieve from one source, tagging each chunk with where it came from."""
    if service.supports_space_filter:
        docs = await service.retrieve(question, top_k, space_keys=space_keys)
    else:
        docs = await service.retrieve(question, top_k)
    for doc in docs:
        doc.metadata.setdefault("source", service.name)
    return docs


def _interleave(results: list[list[SourceDocument]], limit: int) -> list[SourceDocument]:
    """Fuse per-source hit lists by taking rank 1 from each, then rank 2, ...

    Deliberately not a sort by score: each source has its own collection and,
    for Slack, no score at all, so the numbers are not comparable across
    sources. Rank is, and round-robin also guarantees every selected source
    gets a say instead of one of them filling the whole context.

    `limit` is TOP_K *per source*, matching what the setting says it is. Capping
    the fused list at TOP_K in total would mean retrieving 2*TOP_K chunks and
    throwing half away, and would silently halve each source's depth the moment
    a reader ticked a second box -- while the UI still reported TOP_K.
    """
    fused: list[SourceDocument] = []
    for rank in range(max((len(r) for r in results), default=0)):
        for hits in results:
            if rank < len(hits):
                fused.append(hits[rank])
                if len(fused) == limit:
                    return fused
    return fused


@router.get("/sources")
async def list_sources() -> dict:
    return {"sources": sorted(_registry())}


@router.post("", response_model=ChatResponse)
async def multi_chat(request: MultiChatRequest) -> ChatResponse:
    """Answer one question over every selected source at once."""
    registry = _registry()
    names = request.sources or sorted(registry)
    unknown = [name for name in names if name not in registry]
    if unknown:
        raise HTTPException(status_code=404, detail=f"Unknown source(s): {', '.join(unknown)}")

    top_k = resolve_top_k(request.top_k)
    services = [registry[name] for name in names]
    results = await asyncio.gather(
        *(_retrieve(s, request.question, top_k, request.space_keys) for s in services),
        return_exceptions=True,
    )

    # one source being down should degrade the answer, not lose it
    hits: list[list[SourceDocument]] = []
    answered: list[str] = []
    failed: list[str] = []
    for service, result in zip(services, results):
        if isinstance(result, Exception):
            logger.warning("Retrieval failed for %s: %s", service.name, result)
            failed.append(service.name)
            continue
        hits.append(result)
        answered.append(service.name)

    if failed and not answered:
        raise HTTPException(status_code=502, detail=f"All sources failed: {', '.join(failed)}")

    fused = _interleave(hits, top_k * len(hits))
    # any service can generate over already-retrieved chunks, but pick one that
    # actually answered rather than one that just failed
    response = await registry[answered[0]].respond(request.question, fused)
    response.source = ", ".join(answered)
    if failed:
        response.answer += f"\n\n_Note: could not reach {', '.join(failed)}._"
    return response


@router.post("/{source}", response_model=ChatResponse)
async def chat(source: str, request: ChatRequest) -> ChatResponse:
    service = _registry().get(source)
    if service is None:
        raise HTTPException(status_code=404, detail=f"Unknown source: {source}")
    return await service.chat(request)
