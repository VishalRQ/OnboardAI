"""Request/response models for ingestion endpoints."""

from pydantic import BaseModel, Field


class IngestRequest(BaseModel):
    """Generic ingest trigger. Service-specific fields go in `options`."""

    full_refresh: bool = False
    options: dict = Field(default_factory=dict)


class IngestResponse(BaseModel):
    source: str
    documents_ingested: int
    chunks_indexed: int
    #: documents already indexed at the current version, so left alone
    documents_skipped: int = 0
    #: documents removed at the source and purged from the index
    documents_purged: int = 0
    detail: str | None = None
