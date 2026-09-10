from fastapi import APIRouter

from app.core.config import get_settings
from app.services.llm_service import ollama_status

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict:
    """Liveness of the API plus its one hard dependency, Ollama.

    Reports 'degraded' rather than failing: the API is genuinely up, but
    every chat and ingest call would 500, and the UI needs to say so.
    """
    settings = get_settings()
    ollama = ollama_status()
    healthy = ollama["reachable"] and ollama["llm_available"] and ollama["embedding_available"]
    return {
        "status": "ok" if healthy else "degraded",
        "app": settings.app_name,
        "vector_backend": settings.vector_backend,
        "llm_model": settings.llm_model,
        # the UI shows retrieval depth rather than offering it as a control
        "top_k": settings.top_k,
        "ollama": ollama,
    }
