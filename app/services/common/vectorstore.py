"""Vector store factory. Chroma for prototyping, Qdrant for production."""

from functools import lru_cache

from app.core.config import get_settings
from app.core.logging import get_logger
from app.services.llm_service import get_embeddings

logger = get_logger(__name__)


@lru_cache
def get_vectorstore(collection: str):
    """Return a vector store handle for `collection`.

    Cached: embedded Chroma expects a single writer per persist directory,
    so every caller shares one handle (docs/CONFLUENCE.md section 6).
    """
    settings = get_settings()
    backend = settings.vector_backend.lower()
    logger.info("Vector store: backend=%s collection=%s", backend, collection)

    if backend == "chroma":
        from langchain_chroma import Chroma

        return Chroma(
            collection_name=collection,
            embedding_function=get_embeddings(),
            persist_directory=settings.chroma_path,
        )
    if backend == "qdrant":
        # from langchain_qdrant import QdrantVectorStore
        raise NotImplementedError("Qdrant backend not wired yet; use chroma.")
    raise ValueError(f"Unknown vector backend: {settings.vector_backend}")


def delete_by_metadata(collection: str, where: dict) -> int:
    """Drop every chunk matching `where`. Used to retire superseded versions.

    Chunk ids embed the document version, so an edited page would otherwise
    leave its old chunks behind alongside the new ones.
    """
    store = get_vectorstore(collection)
    handle = getattr(store, "_collection", None)
    if handle is None:  # pragma: no cover - non-Chroma backend
        raise NotImplementedError("delete_by_metadata is Chroma-specific for now")

    existing = handle.get(where=where, include=[])
    ids = existing.get("ids", [])
    if ids:
        handle.delete(ids=ids)
        logger.info("Deleted %d stale chunks matching %s", len(ids), where)
    return len(ids)


def list_chunks(collection: str, limit: int = 50, offset: int = 0) -> dict:
    """A page of stored chunks, for inspecting what actually got indexed.

    Chroma ships no UI, and the raw sqlite file interleaves documents with
    HNSW state, so this is the supported way to see what a query can reach.
    """
    store = get_vectorstore(collection)
    handle = getattr(store, "_collection", None)
    if handle is None:  # pragma: no cover - non-Chroma backend
        raise NotImplementedError("list_chunks is Chroma-specific for now")

    got = handle.get(limit=limit, offset=offset, include=["metadatas", "documents"])
    metadatas = got.get("metadatas") or []
    documents = got.get("documents") or []
    items = [
        {
            "id": chunk_id,
            # the stored document carries the breadcrumb prefix; raw_text is
            # the clean body, which is what a reader actually wants to see
            "text": (meta or {}).get("raw_text") or doc,
            "metadata": meta or {},
        }
        for chunk_id, doc, meta in zip(got.get("ids", []), documents, metadatas)
    ]
    return {"total": handle.count(), "limit": limit, "offset": offset, "items": items}


def count(collection: str) -> int:
    """Number of chunks currently indexed in `collection`."""
    store = get_vectorstore(collection)
    handle = getattr(store, "_collection", None)
    return handle.count() if handle is not None else 0
