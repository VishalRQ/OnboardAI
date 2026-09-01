"""Contract every knowledge source implements.

Adding a service means: subclass this, drop it in `app/services/<name>/`,
and register a router in `app/api/routes/`.
"""

from abc import ABC, abstractmethod

from app.schemas.chat import ChatRequest, ChatResponse, SourceDocument
from app.schemas.ingest import IngestRequest, IngestResponse
from app.services.llm_service import generate_answer


class BaseRAGService(ABC):
    """A single knowledge source: ingest documents, then answer over them."""

    #: short identifier used in routes, collections and responses
    name: str
    #: vector store collection backing this service
    collection: str

    @abstractmethod
    async def ingest(self, request: IngestRequest) -> IngestResponse:
        """Pull documents from the source and index them."""

    @abstractmethod
    async def retrieve(self, query: str, top_k: int) -> list[SourceDocument]:
        """Return the chunks most relevant to `query`."""

    async def chat(self, request: ChatRequest) -> ChatResponse:
        """Retrieve, then generate. Override only if the flow differs."""
        sources = await self.retrieve(request.question, request.top_k)
        answer = await generate_answer(request.question, [s.text for s in sources])
        return ChatResponse(answer=answer, source=self.name, sources=sources)
