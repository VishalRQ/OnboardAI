"""Contract every knowledge source implements.

Adding a service means: subclass this, drop it in `app/services/<name>/`,
and register a router in `app/api/routes/`.
"""

from abc import ABC, abstractmethod

from app.core.config import get_settings
from app.schemas.chat import ChatRequest, ChatResponse, SourceDocument
from app.schemas.ingest import IngestRequest, IngestResponse
from app.services.llm_service import generate_answer


def resolve_top_k(requested: int | None) -> int:
    """Retrieval depth: the caller's, else TOP_K from the environment."""
    return requested or get_settings().top_k


class BaseRAGService(ABC):
    """A single knowledge source: ingest documents, then answer over them."""

    #: short identifier used in routes, collections and responses
    name: str
    #: vector store collection backing this service
    collection: str
    #: True when `retrieve` accepts a `space_keys=` keyword. Lets the
    #: cross-source fan-out pass a space filter only where it means something,
    #: without every source having to grow the argument.
    supports_space_filter: bool = False

    @abstractmethod
    async def ingest(self, request: IngestRequest) -> IngestResponse:
        """Pull documents from the source and index them."""

    @abstractmethod
    async def retrieve(self, query: str, top_k: int) -> list[SourceDocument]:
        """Return the chunks most relevant to `query`."""

    async def respond(self, question: str, sources: list[SourceDocument]) -> ChatResponse:
        """Generate a grounded answer over already-retrieved chunks."""
        answer = await generate_answer(question, [s.text for s in sources])
        return ChatResponse(answer=answer, source=self.name, sources=sources)

    async def chat(self, request: ChatRequest) -> ChatResponse:
        """Retrieve, then generate. Override only if the flow differs."""
        sources = await self.retrieve(request.question, resolve_top_k(request.top_k))
        return await self.respond(request.question, sources)
