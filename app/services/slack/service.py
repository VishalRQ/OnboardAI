"""Slack RAG service."""

from functools import lru_cache

from app.schemas.chat import SourceDocument
from app.schemas.ingest import IngestRequest, IngestResponse
from app.services.common.base import BaseRAGService
from app.services.common.vectorstore import get_vectorstore
from app.services.slack.loader import load_from_export, load_incremental


class SlackService(BaseRAGService):
    name = "slack"
    collection = "slack_threads"

    async def ingest(self, request: IngestRequest) -> IngestResponse:
        if request.full_refresh:
            threads = await load_from_export(request.options.get("export_path"))
        else:
            threads = await load_incremental(request.options.get("channel_ids", []))
        get_vectorstore(self.collection)  # placeholder: chunk + upsert here
        return IngestResponse(
            source=self.name,
            documents_ingested=len(threads),
            chunks_indexed=0,
            detail="stub ingest - chunking and indexing not wired up yet",
        )

    async def retrieve(self, query: str, top_k: int) -> list[SourceDocument]:
        get_vectorstore(self.collection)  # placeholder: hybrid search + rank fusion
        threads = await load_from_export()
        return [
            SourceDocument(id=t.id, text=t.text, score=None, metadata=t.metadata)
            for t in threads[:top_k]
        ]


@lru_cache
def get_slack_service() -> SlackService:
    return SlackService()
