"""Vector store factory. Chroma for prototyping, Qdrant for production."""

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)


def get_vectorstore(collection: str):
    """Return a vector store handle for `collection`. Stubbed for now."""
    settings = get_settings()
    backend = settings.vector_backend.lower()
    logger.info("Vector store requested: backend=%s collection=%s", backend, collection)

    if backend == "chroma":
        # from langchain_chroma import Chroma
        # return Chroma(collection_name=collection,
        #               embedding_function=get_embeddings(),
        #               persist_directory=settings.chroma_path)
        return None
    if backend == "qdrant":
        # from langchain_qdrant import QdrantVectorStore
        # return QdrantVectorStore.from_existing_collection(...)
        return None
    raise ValueError(f"Unknown vector backend: {settings.vector_backend}")
