"""Application settings, loaded from environment / .env."""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "OnboardAI"
    debug: bool = True

    # Ollama
    ollama_base_url: str = "http://localhost:11434"
    llm_model: str = "llama3.2:1b"
    embedding_model: str = "nomic-embed-text"

    chunk_size: int = 800
    chunk_overlap: int = 120
    min_chunk_tokens: int = 20

    # Retrieval
    #: chunks pulled per source for one question. Server-side on purpose: it
    #: trades answer breadth against context budget and latency, which is an
    #: operator decision, not something a reader should be tuning per question.
    top_k: int = Field(default=4, ge=1, le=50)
    max_context_chars: int = 12000
    max_retrieval_distance: float = 0.7

    # Vector store: "chroma" for prototyping, "qdrant" for production
    vector_backend: str = "chroma"
    chroma_path: str = "./data/chroma"
    qdrant_url: str = "http://localhost:6333"

    # Confluence
    confluence_url: str | None = None
    confluence_username: str | None = None
    confluence_api_token: str | None = None
    confluence_space_keys: list[str] = Field(default_factory=list)
    #: where the incremental-sync watermark lives
    confluence_sync_state_path: str = "./data/confluence_sync_state.json"
    #: re-query this far back from the watermark to absorb clock skew and
    #: Confluence's eventual consistency on lastmodified
    confluence_sync_overlap_minutes: int = 15
    #: hours between deletion-reconciliation passes; a delta query cannot see
    #: a deleted page, so only a periodic full id-list diff can purge one
    confluence_reconcile_interval_hours: int = 24
    #: a sync lock older than this is assumed to belong to a killed process and
    #: is taken over, so a crash mid-ingest cannot wedge every scheduled run
    confluence_sync_lock_stale_minutes: int = 120
    #: how long the space list from Confluence is reused. Spaces are created
    #: about weekly, and the UI re-asks on every rerun, so this is what keeps
    #: a checkbox click from paginating a remote API.
    confluence_space_cache_seconds: int = 300



@lru_cache
def get_settings() -> Settings:
    return Settings()
