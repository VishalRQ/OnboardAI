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
    so every caller shares one handle.
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


def _handle(collection: str):
    """The raw Chroma collection behind `collection`.

    Everything below reaches past LangChain because the metadata scans and
    id-level deletes these need have no equivalent in the VectorStore API.
    """
    store = get_vectorstore(collection)
    handle = getattr(store, "_collection", None)
    if handle is None:  # pragma: no cover - non-Chroma backend
        raise NotImplementedError("this helper is Chroma-specific for now")
    return handle


def delete_by_metadata(collection: str, where: dict) -> int:
    """Drop every chunk matching `where`. Used to retire superseded versions.

    Chunk ids embed the document version, so an edited page would otherwise
    leave its old chunks behind alongside the new ones.
    """
    handle = _handle(collection)
    existing = handle.get(where=where, include=[])
    ids = existing.get("ids", [])
    if ids:
        handle.delete(ids=ids)
        logger.info("Deleted %d stale chunks matching %s", len(ids), where)
    return len(ids)


def scan_metadata(collection: str, batch: int = 1000) -> list[dict]:
    """Every chunk's metadata, paged so a large collection does not spike RAM.

    Chroma has no aggregation of its own, so the counts and lookup tables the
    UI and incremental sync need are built here, in one pass.
    """
    handle = _handle(collection)
    total = handle.count()
    metadatas: list[dict] = []
    for offset in range(0, total, batch):
        got = handle.get(limit=batch, offset=offset, include=["metadatas"])
        metadatas.extend(m or {} for m in (got.get("metadatas") or []))
    return metadatas


def list_chunks(collection: str, limit: int = 50, offset: int = 0) -> dict:
    """A page of stored chunks, for inspecting what actually got indexed.

    Chroma ships no UI, and the raw sqlite file interleaves documents with
    HNSW state, so this is the supported way to see what a query can reach.
    """
    handle = _handle(collection)
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
    return _handle(collection).count()


def group_by(collection: str, field: str) -> dict[str, int]:
    """Chunk counts per distinct value of `field`, for "what is indexed?" views."""
    counts: dict[str, int] = {}
    for meta in scan_metadata(collection):
        value = meta.get(field)
        if value not in (None, ""):
            counts[str(value)] = counts.get(str(value), 0) + 1
    return counts


def field_maps(
    collection: str, key_field: str, value_fields: tuple[str, ...]
) -> dict[str, dict[str, object]]:
    """Map `key_field` -> each of `value_fields`, in a single pass.

    One scan replaces a per-document lookup: incremental sync asks "what
    version of this page is indexed?" once per page, which is a query per page
    against a store that can answer for all of them at once. Several fields are
    built together because a scan is the expensive part, not the bookkeeping --
    one ingest needs both the indexed version and the space of every page.
    """
    maps: dict[str, dict[str, object]] = {field: {} for field in value_fields}
    for meta in scan_metadata(collection):
        key = meta.get(key_field)
        if key in (None, ""):
            continue
        key = str(key)
        for field in value_fields:
            maps[field].setdefault(key, meta.get(field))
    return maps

