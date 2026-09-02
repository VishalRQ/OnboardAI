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

    # Slack
    slack_bot_token: str | None = None
    slack_export_path: str | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()
