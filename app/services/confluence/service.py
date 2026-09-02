"""Confluence RAG service."""

from functools import lru_cache

from app.core.config import get_settings
from app.core.logging import get_logger
from app.schemas.chat import SourceDocument
from app.schemas.ingest import IngestRequest, IngestResponse
from app.services.common.base import BaseRAGService
from app.services.common.chunking import chunk_document
from app.services.common.vectorstore import count, delete_by_metadata, get_vectorstore
from app.services.confluence.loader import RawDocument, load_pages, now_iso

logger = get_logger(__name__)


class ConfluenceService(BaseRAGService):
    name = "confluence"
    collection = "confluence_docs"

    def _chunk(self, page: RawDocument, ingested_at: str):
        """Turn one page into indexable chunks with full citation metadata."""
        version = page.metadata.get("version", 1)
        return chunk_document(
            doc_id=page.id,
            text=page.text,
            title=page.title,
            breadcrumb=page.metadata.get("ancestors", ""),
            id_prefix=f"{page.id}:v{version}",
            metadata={**page.metadata, "ingested_at": ingested_at},
        )

    def _indexed_version(self, page_id: str) -> int | None:
        """Version currently indexed for `page_id`, or None if absent."""
        store = get_vectorstore(self.collection)
        existing = store._collection.get(
            where={"page_id": page_id}, limit=1, include=["metadatas"]
        )
        metadatas = existing.get("metadatas") or []
        return metadatas[0].get("version") if metadatas else None

    async def ingest(self, request: IngestRequest) -> IngestResponse:
        space_keys = request.options.get("space_keys")
        pages = await load_pages(space_keys)
        store = get_vectorstore(self.collection)
        ingested_at = now_iso()

        indexed = skipped = chunks_written = 0
        for page in pages:
            page_id = page.metadata.get("page_id", page.id)
            version = page.metadata.get("version", 1)

            if not request.full_refresh and self._indexed_version(page_id) == version:
                skipped += 1
                continue

            # Chunk ids embed the version, so stale chunks must go explicitly.
            delete_by_metadata(self.collection, {"page_id": page_id})

            chunks = self._chunk(page, ingested_at)
            if not chunks:
                continue
            store.add_texts(
                texts=[c.text for c in chunks],
                metadatas=[{**c.metadata, "chunk_id": c.id, "raw_text": c.raw_text} for c in chunks],
                ids=[c.id for c in chunks],
            )
            indexed += 1
            chunks_written += len(chunks)

        detail = (
            f"pages_seen={len(pages)} indexed={indexed} skipped={skipped} "
            f"collection_total={count(self.collection)}"
        )
        logger.info("Confluence ingest: %s", detail)
        return IngestResponse(
            source=self.name,
            documents_ingested=indexed,
            chunks_indexed=chunks_written,
            detail=detail,
        )

    async def retrieve(self, query: str, top_k: int) -> list[SourceDocument]:
        store = get_vectorstore(self.collection)
        hits = store.similarity_search_with_score(query, k=top_k)

        # similarity_search_with_score always returns k chunks, however far
        # away they are. Drop the ones past the cutoff so an off-topic question
        # reaches the generator with no context and gets a clean refusal
        # instead of a summary of whatever happened to be nearest.
        limit = get_settings().max_retrieval_distance
        kept = [(doc, score) for doc, score in hits if score <= limit]
        if len(kept) < len(hits):
            logger.info(
                "Dropped %d/%d chunks past distance %.2f for %r",
                len(hits) - len(kept), len(hits), limit, query,
            )

        return [
            SourceDocument(
                id=doc.metadata.get("chunk_id") or doc.id or "",
                # cite the clean text; the breadcrumb prefix is for the embedder
                text=doc.metadata.get("raw_text", doc.page_content),
                score=float(score),
                metadata={k: v for k, v in doc.metadata.items() if k != "raw_text"},
            )
            for doc, score in kept
        ]


@lru_cache
def get_confluence_service() -> ConfluenceService:
    return ConfluenceService()
