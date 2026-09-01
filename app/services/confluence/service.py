"""Confluence RAG service."""

from functools import lru_cache

from app.schemas.chat import SourceDocument
from app.schemas.ingest import IngestRequest, IngestResponse
from app.services.common.base import BaseRAGService
from app.services.common.vectorstore import get_vectorstore
from app.services.confluence.loader import load_pages


class ConfluenceService(BaseRAGService):
    name = "confluence"
    collection = "confluence_docs"

    async def ingest(self, request: IngestRequest) -> IngestResponse:
        space_keys = request.options.get("space_keys")
        documents = await load_pages(space_keys)
        get_vectorstore(self.collection)  # placeholder: chunk + upsert here
        return IngestResponse(
            source=self.name,
            documents_ingested=len(documents),
            chunks_indexed=0,
            detail="stub ingest - chunking and indexing not wired up yet",
        )

    async def retrieve(self, query: str, top_k: int) -> list[SourceDocument]:
        get_vectorstore(self.collection)  # placeholder: similarity_search here
        pages = await load_pages()
        return [
            SourceDocument(id=p.id, text=p.text, score=None, metadata=p.metadata)
            for p in pages[:top_k]
        ]


@lru_cache
def get_confluence_service() -> ConfluenceService:
    return ConfluenceService()
