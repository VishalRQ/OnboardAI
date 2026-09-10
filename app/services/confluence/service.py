"""Confluence RAG service."""

import asyncio
from datetime import datetime, timezone
from functools import lru_cache

from app.core.config import get_settings
from app.core.logging import get_logger
from app.schemas.chat import ChatRequest, ChatResponse, SourceDocument
from app.schemas.ingest import IngestRequest, IngestResponse
from app.services.common.base import BaseRAGService, resolve_top_k
from app.services.common.chunking import chunk_document
from app.services.common.vectorstore import (
    count,
    delete_by_metadata,
    field_maps,
    get_vectorstore,
    group_by,
)
from app.services.confluence.loader import (
    RawDocument,
    configured_spaces,
    has_credentials,
    invalidate_space_cache,
    list_spaces,
    load_page_ids,
    load_pages,
    now_iso,
)
from app.services.confluence.sync_state import (
    last_synced_at,
    read_state,
    reconcile_due,
    record_sync,
    since_watermark,
    sync_lock,
)

logger = get_logger(__name__)


class ConfluenceService(BaseRAGService):
    name = "confluence"
    collection = "confluence_docs"
    supports_space_filter = True

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

    def _indexed_versions(self) -> dict[str, object]:
        """page_id -> indexed version, in one scan rather than a query per page."""
        return field_maps(self.collection, "page_id", ("version",))["version"]

    def _purge_deleted(
        self, space_key: str, live_ids: set[str], indexed_spaces: dict[str, object]
    ) -> int:
        """Drop indexed pages of `space_key` that no longer exist at the source.

        A `lastmodified` delta query can never report a deletion, so the only
        way to see one is to diff the full id list.
        """
        stale = [
            page_id
            for page_id, indexed_space in indexed_spaces.items()
            # only judge the space this pass actually queried; a page in a
            # space we did not query is absent from live_ids for a boring
            # reason and must not be purged
            if indexed_space == space_key and page_id not in live_ids
        ]
        if not stale:
            return 0
        # one $in delete, not one round trip per page
        delete_by_metadata(self.collection, {"page_id": {"$in": stale}})
        for page_id in stale:
            indexed_spaces.pop(page_id, None)
        logger.info("Purged %d pages deleted from %s: %s", len(stale), space_key, stale)
        return len(stale)

    def _resolve_spaces(self, request: IngestRequest) -> list[str]:
        """Spaces this run covers: the request's, else .env, else the fixtures."""
        space_keys = request.options.get("space_keys") or configured_spaces()
        if space_keys:
            return list(space_keys)
        if has_credentials():
            raise ValueError("CONFLUENCE_SPACE_KEYS is empty; nothing to load")
        # fixture mode: the fixtures themselves define what there is to ingest
        return [space["key"] for space in list_spaces()]

    def _index_space(self, space_key, since, store, ingested_at, indexed_versions, full):
        """Fetch and index one space. Returns (pages_seen, indexed, skipped, chunks)."""
        pages = load_pages([space_key], since=since)
        indexed = skipped = chunks_written = 0
        for page in pages:
            page_id = str(page.metadata.get("page_id", page.id))
            version = page.metadata.get("version", 1)

            if not full and indexed_versions.get(page_id) == version:
                skipped += 1
                continue

            # Chunk ids embed the version, so stale chunks must go explicitly.
            delete_by_metadata(self.collection, {"page_id": page_id})

            chunks = self._chunk(page, ingested_at)
            if not chunks:
                continue
            store.add_texts(
                texts=[c.text for c in chunks],
                metadatas=[
                    {**c.metadata, "chunk_id": c.id, "raw_text": c.raw_text}
                    for c in chunks
                ],
                ids=[c.id for c in chunks],
            )
            indexed_versions[page_id] = version
            indexed += 1
            chunks_written += len(chunks)
        return len(pages), indexed, skipped, chunks_written

    def _ingest_blocking(self, request: IngestRequest) -> IngestResponse:
        """The whole ingest pipeline. Synchronous top to bottom: the Confluence
        client, the embedding calls behind `add_texts` and the retry sleeps all
        block, which is why `ingest` keeps this off the event loop."""
        space_keys = self._resolve_spaces(request)

        with sync_lock():
            state = read_state()
            store = get_vectorstore(self.collection)
            ingested_at = now_iso()
            # one scan for both maps: scanning is the cost, not the bookkeeping
            maps = field_maps(self.collection, "page_id", ("version", "space_key"))
            indexed_versions, indexed_spaces = maps["version"], maps["space_key"]

            seen = indexed = skipped = chunks_written = purged = 0
            full_window = True
            for space_key in space_keys:
                # start time, not end time: edits made during a long run must
                # fall inside the next window rather than being skipped
                run_started = datetime.now(timezone.utc)
                # a full refresh re-reads everything; an incremental run asks
                # Confluence only for what changed since this space's last run
                since = None if request.full_refresh else since_watermark(state, space_key)
                full_window = full_window and since is None

                counts = self._index_space(
                    space_key, since, store, ingested_at, indexed_versions,
                    request.full_refresh,
                )
                seen += counts[0]
                indexed += counts[1]
                skipped += counts[2]
                chunks_written += counts[3]

                reconciled = request.full_refresh or reconcile_due(state, space_key)
                if reconciled:
                    live_ids = load_page_ids([space_key])
                    if live_ids is None:
                        reconciled = False  # fixture mode: nothing authoritative to diff
                    else:
                        purged += self._purge_deleted(space_key, live_ids, indexed_spaces)

                # only a clean pass advances a watermark -- a failure raises out
                # of here and leaves this space's window for the next attempt,
                # while the spaces already done keep the ground they gained
                stamp = run_started.isoformat(timespec="seconds")
                record_sync(space_key, stamp, stamp if reconciled else None)

        # the run may have made a newly created space answerable
        invalidate_space_cache()

        detail = (
            f"pages_seen={seen} indexed={indexed} skipped={skipped} "
            f"purged={purged} spaces={','.join(space_keys) or 'all'} "
            f"since={'beginning' if full_window else 'last sync'} "
            f"collection_total={count(self.collection)}"
        )
        logger.info("Confluence ingest: %s", detail)
        return IngestResponse(
            source=self.name,
            documents_ingested=indexed,
            chunks_indexed=chunks_written,
            documents_skipped=skipped,
            documents_purged=purged,
            detail=detail,
        )

    async def ingest(self, request: IngestRequest) -> IngestResponse:
        # off the event loop: an ingest runs for minutes, and on the loop it
        # would stall every other request -- health checks and chat included
        return await asyncio.to_thread(self._ingest_blocking, request)

    async def retrieve(
        self, query: str, top_k: int, space_keys: list[str] | None = None
    ) -> list[SourceDocument]:
        store = get_vectorstore(self.collection)
        # narrowing to the chosen spaces before the search, not after, so the
        # k slots are all spent on candidates the reader actually asked for
        where = {"space_key": {"$in": list(space_keys)}} if space_keys else None
        # embedding the query is a blocking round trip to Ollama; off the loop
        # so concurrent chats interleave instead of queueing behind each other
        hits = await asyncio.to_thread(
            store.similarity_search_with_score, query, k=top_k, filter=where
        )

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

    async def chat(self, request: ChatRequest) -> ChatResponse:
        sources = await self.retrieve(
            request.question,
            resolve_top_k(request.top_k),
            space_keys=request.space_keys,
        )
        return await self.respond(request.question, sources)

    def spaces(self) -> list[dict]:
        """Selectable spaces, annotated with what is currently indexed."""
        indexed = group_by(self.collection, "space_key")
        available = list_spaces()
        known = {space["key"] for space in available}
        # a space that was ingested and later dropped from .env is still
        # answerable, so it belongs in the picker
        available += [{"key": key, "name": ""} for key in indexed if key not in known]
        return [
            {**space, "indexed_chunks": indexed.get(space["key"], 0)}
            for space in sorted(available, key=lambda space: space["key"])
        ]

    def status(self) -> dict:
        """Index + sync state. Deliberately does not embed `spaces()`: the UI
        polls this on every rerun, and `spaces()` costs a collection scan plus
        a live space listing that the /spaces endpoint already serves."""
        state = read_state()
        return {
            "collection": self.collection,
            "chunks": count(self.collection),
            "pages": len(self._indexed_versions()),
            # oldest per-space watermark: the honest "how stale can this be?"
            "last_synced_at": last_synced_at(state),
            "spaces_synced": state.get("spaces") or {},
        }


@lru_cache
def get_confluence_service() -> ConfluenceService:
    return ConfluenceService()
