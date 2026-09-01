"""Request/response models shared by every chat endpoint."""

from pydantic import BaseModel, Field


class SourceDocument(BaseModel):
    """A retrieved chunk cited in an answer."""

    id: str
    text: str
    score: float | None = None
    metadata: dict = Field(default_factory=dict)


class ChatRequest(BaseModel):
    question: str = Field(min_length=1)
    session_id: str | None = None
    top_k: int = Field(default=4, ge=1, le=50)


class ChatResponse(BaseModel):
    answer: str
    source: str
    sources: list[SourceDocument] = Field(default_factory=list)
