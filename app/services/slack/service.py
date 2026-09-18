"""Slack RAG service.

Same contract as Confluence (ingest / retrieve / chat via BaseRAGService) and
the same shared chunker, vector store and LLM. What differs is Slack-specific:
the retrieval unit is a thread or a message window, and "has this changed?"
is keyed on the newest message timestamp rather than a page version number.
"""

from functools import lru_cache

from app.core.config import get_settings
from app.core.logging import get_logger
from app.schemas.chat import SourceDocument
from app.schemas.ingest import IngestRequest, IngestResponse
from app.services.common.base import BaseRAGService
from app.services.common.chunking import chunk_document
from app.services.common.vectorstore import (
    count,
    delete_by_metadata,
    get_vectorstore,
    group_by,
)
from app.services.slack.loader import RawThread, load_threads, now_iso

logger = get_logger(__name__)


class SlackService(BaseRAGService):
    name = "slack"
    collection = "slack_messages"

    def _chunk(self, unit: RawThread, ingested_at: str):
        """One thread/window -> indexable chunks with citation metadata.

        A rendered thread has no markdown headings, so the shared chunker
        emits exactly one chunk unless the thread is very long, in which case
        it splits on blank lines -- i.e. between messages.
        """
        version = unit.metadata.get("version", "0")
        doc_id = unit.metadata["doc_id"]
        # doc_id (not unit.id) is the key every lookup here filters on, so the
        # chunker must stamp that same value onto each chunk's metadata.
        return chunk_document(
            doc_id=doc_id,
            text=unit.text,
            title=unit.title,
            breadcrumb=unit.metadata.get("channel", ""),
            id_prefix=f"{doc_id}:v{version}",
            metadata={**unit.metadata, "ingested_at": ingested_at},
        )

    def _indexed_version(self, doc_id: str) -> str | None:
        """Message timestamp currently indexed for `doc_id`, or None if absent."""
        store = get_vectorstore(self.collection)
        existing = store._collection.get(
            where={"doc_id": doc_id}, limit=1, include=["metadatas"]
        )
        metadatas = existing.get("metadatas") or []
        return metadatas[0].get("version") if metadatas else None

    def _reap(self, keep: set[str], channels: set[str] | None) -> int:
        """Delete indexed docs not in `keep`. Scoped to `channels` if given.

        Called only on a full refresh, which is authoritative: a doc that was
        indexed for a covered channel but did not come back this run was
        deleted at the source.
        """
        store = get_vectorstore(self.collection)
        got = store._collection.get(include=["metadatas"])
        stale = {
            m.get("doc_id")
            for m in (got.get("metadatas") or [])
            if m.get("doc_id") not in keep
            and (channels is None or m.get("channel") in channels)
        }
        removed = 0
        for doc_id in stale:
            removed += delete_by_metadata(self.collection, {"doc_id": doc_id})
        return removed

    async def ingest(self, request: IngestRequest) -> IngestResponse:
        opts = request.options
        requested = opts.get("channels") or None
        units = await load_threads(
            channels=requested,
            since=opts.get("since"),
            export_path=opts.get("export_path"),
        )
        store = get_vectorstore(self.collection)
        ingested_at = now_iso()

        seen: set[str] = set()
        indexed = skipped = chunks_written = 0
        for unit in units:
            doc_id = unit.metadata["doc_id"]
            seen.add(doc_id)
            version = unit.metadata.get("version", "0")

            if not request.full_refresh and self._indexed_version(doc_id) == version:
                skipped += 1
                continue

            # chunk ids embed the version, so superseded chunks go explicitly
            delete_by_metadata(self.collection, {"doc_id": doc_id})
            chunks = self._chunk(unit, ingested_at)
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
            indexed += 1
            chunks_written += len(chunks)

        # reconcile deletions. If specific channels were requested, reap within
        # them (even if none came back). If not, only reap when the run
        # returned something -- a loader that silently yielded nothing must
        # not wipe the whole collection.
        removed = 0
        if request.full_refresh:
            scope = None
            if requested:
                scope = {c if c.startswith("#") else f"#{c}" for c in requested}
            if scope is not None or units:
                removed = self._reap(seen, scope)

        detail = (
            f"units_seen={len(units)} indexed={indexed} skipped={skipped} "
            f"removed={removed} collection_total={count(self.collection)}"
        )
        logger.info("Slack ingest: %s", detail)
        return IngestResponse(
            source=self.name,
            documents_ingested=indexed,
            chunks_indexed=chunks_written,
            detail=detail,
        )

    async def retrieve(self, query: str, top_k: int) -> list[SourceDocument]:
        store = get_vectorstore(self.collection)
        hits = store.similarity_search_with_score(query, k=top_k)

        # drop chunks past the distance cutoff so an off-topic question reaches
        # the generator with no context and gets a clean "not in Slack" refusal
        limit = get_settings().max_retrieval_distance
        kept = [(doc, score) for doc, score in hits if score <= limit]
        if len(kept) < len(hits):
            logger.info(
                "Slack: dropped %d/%d chunks past distance %.2f for %r",
                len(hits) - len(kept), len(hits), limit, query,
            )

        return [
            SourceDocument(
                id=doc.metadata.get("chunk_id") or doc.id or "",
                text=doc.metadata.get("raw_text", doc.page_content),
                score=float(score),
                metadata={k: v for k, v in doc.metadata.items() if k != "raw_text"},
            )
            for doc, score in kept
        ]

    def channels(self) -> list[dict]:
        """Channels the UI can offer, each with its indexed chunk count.

        Mirrors ConfluenceService.spaces(). Typing channel names by hand is the
        same trap as typing space keys: a name the bot cannot see fails the
        whole ingest, so the picker should only ever offer real options.

        `resolve_channels([])` returns everything the bot can see, which is the
        only sensible candidate list -- a channel it is not in cannot be read.
        """
        from app.services.slack.client import SlackClient

        # chunk metadata stores the display form ("#general"); the API returns
        # the bare name, so compare on the stripped form
        indexed = {k.lstrip("#"): v for k, v in group_by(self.collection, "channel").items()}
        # members_only=False so the picker can show unjoined channels as
        # needing an invite rather than hiding them and looking broken
        available = SlackClient().resolve_channels(
            [], include_private=get_settings().slack_index_private, members_only=False
        )
        known = {c["name"] for c in available}
        # a channel that was indexed and later left is still answerable, so it
        # belongs in the picker even though the bot can no longer read it
        available += [
            {"id": "", "name": name, "is_private": False, "is_member": False}
            for name in indexed
            if name not in known
        ]
        return [
            {**c, "indexed_chunks": indexed.get(c["name"], 0)}
            for c in sorted(available, key=lambda c: c["name"])
        ]


@lru_cache
def get_slack_service() -> SlackService:
    return SlackService()
