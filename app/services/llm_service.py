"""Shared LLM + embedding access (Ollama). All services go through here."""

from functools import lru_cache

import httpx
from langchain_ollama import ChatOllama, OllamaEmbeddings

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

#: nomic-embed-text is trained with task prefixes; mismatching them between
#: ingest and query silently degrades recall. Centralised here so no caller
#: can get it wrong -- see docs/CONFLUENCE.md section 4.3.
DOCUMENT_PREFIX = "search_document: "
QUERY_PREFIX = "search_query: "

ANSWER_PROMPT = """You answer questions for new joiners using only the \
internal documentation excerpts below.

Rules:
- Use only the excerpts. Do not rely on outside knowledge.
- If the excerpts do not contain the answer, say so plainly and stop.
- Cite the excerpt numbers you used, like [1] or [2].
- Be brief and concrete. Prefer steps over prose.

Excerpts:
{context}

Question: {question}

Answer:"""


class PrefixedOllamaEmbeddings(OllamaEmbeddings):
    """OllamaEmbeddings that applies the nomic-embed-text task prefixes."""

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return super().embed_documents([DOCUMENT_PREFIX + t for t in texts])

    def embed_query(self, text: str) -> list[float]:
        return super().embed_query(QUERY_PREFIX + text)

    async def aembed_documents(self, texts: list[str]) -> list[list[float]]:
        return await super().aembed_documents([DOCUMENT_PREFIX + t for t in texts])

    async def aembed_query(self, text: str) -> list[float]:
        return await super().aembed_query(QUERY_PREFIX + text)


@lru_cache
def get_llm() -> ChatOllama:
    """Return the chat model."""
    settings = get_settings()
    logger.info("LLM: %s @ %s", settings.llm_model, settings.ollama_base_url)
    return ChatOllama(
        model=settings.llm_model,
        base_url=settings.ollama_base_url,
        temperature=0,
    )


@lru_cache
def get_embeddings() -> PrefixedOllamaEmbeddings:
    """Return the embedding model, with task prefixes applied."""
    settings = get_settings()
    logger.info("Embeddings: %s", settings.embedding_model)
    return PrefixedOllamaEmbeddings(
        model=settings.embedding_model,
        base_url=settings.ollama_base_url,
    )


def _tag_matches(configured: str, tag: str) -> bool:
    """Ollama reports 'llama3.2:1b' and 'nomic-embed-text:latest'; a config
    value may or may not carry the tag, so compare both ways."""
    return tag == configured or tag == f"{configured}:latest" or tag.split(":")[0] == configured


def ollama_status(timeout: float = 2.0) -> dict:
    """Liveness of the Ollama server and whether our two models are pulled.

    /api/tags is the cheapest call that proves the daemon is actually serving.
    Kept short-timeout and exception-free so /health never hangs or 500s.
    """
    settings = get_settings()
    result = {
        "reachable": False,
        "base_url": settings.ollama_base_url,
        "llm_model": settings.llm_model,
        "embedding_model": settings.embedding_model,
        "llm_available": False,
        "embedding_available": False,
        "detail": "",
    }
    try:
        response = httpx.get(f"{settings.ollama_base_url}/api/tags", timeout=timeout)
        response.raise_for_status()
        tags = [m.get("name", "") for m in response.json().get("models", [])]
    except Exception as exc:  # noqa: BLE001 - health must report, never raise
        result["detail"] = f"{type(exc).__name__}: {exc}"[:200]
        return result

    result["reachable"] = True
    result["llm_available"] = any(_tag_matches(settings.llm_model, t) for t in tags)
    result["embedding_available"] = any(
        _tag_matches(settings.embedding_model, t) for t in tags
    )

    missing = [
        name
        for name, ok in (
            (settings.llm_model, result["llm_available"]),
            (settings.embedding_model, result["embedding_available"]),
        )
        if not ok
    ]
    if missing:
        result["detail"] = "not pulled: " + ", ".join(f"ollama pull {m}" for m in missing)
    return result


def build_context(chunks: list[str]) -> str:
    """Number the excerpts so the model can cite them, and cap total size."""
    budget = get_settings().max_context_chars
    parts: list[str] = []
    used = 0
    for i, chunk in enumerate(chunks, start=1):
        block = f"[{i}] {chunk}"
        if used + len(block) > budget:
            logger.info("Context budget reached; using %d of %d chunks", i - 1, len(chunks))
            break
        parts.append(block)
        used += len(block)
    return "\n\n".join(parts)


async def generate_answer(question: str, context: list[str]) -> str:
    """Answer `question` grounded in `context`. No context -> no answer."""
    if not context:
        return "I could not find anything about that in the indexed documentation."

    prompt = ANSWER_PROMPT.format(context=build_context(context), question=question)
    response = await get_llm().ainvoke(prompt)
    return response.content.strip()
