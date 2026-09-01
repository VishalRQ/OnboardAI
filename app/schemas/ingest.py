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
    detail: str | None = None
