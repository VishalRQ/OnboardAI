"""Shared LLM + embedding access (Ollama). All services go through here."""

from functools import lru_cache

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)


@lru_cache
def get_llm():
    """Return the chat model. Stubbed until langchain-ollama is wired up."""
    settings = get_settings()
    logger.info("LLM requested: %s @ %s", settings.llm_model, settings.ollama_base_url)
    # from langchain_ollama import ChatOllama
    # return ChatOllama(model=settings.llm_model, base_url=settings.ollama_base_url)
    return None


@lru_cache
def get_embeddings():
    """Return the embedding model. Stubbed until langchain-ollama is wired up."""
    settings = get_settings()
    logger.info("Embeddings requested: %s", settings.embedding_model)
    # from langchain_ollama import OllamaEmbeddings
    # return OllamaEmbeddings(model=settings.embedding_model,
    #                         base_url=settings.ollama_base_url)
    return None


async def generate_answer(question: str, context: list[str]) -> str:
    """Dummy answer generation. Replace with a real LangChain chain."""
    llm = get_llm()
    if llm is None:
        joined = " | ".join(c[:60] for c in context) or "no context yet"
        return f"[stub answer] q={question!r} context=({joined})"
    raise NotImplementedError("Wire the LangChain chain here.")
