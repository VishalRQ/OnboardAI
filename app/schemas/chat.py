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
    #: None means "use TOP_K from the environment". Retrieval depth is an
    #: operator setting (Settings.top_k), not a per-question control, so the
    #: UI never sends this -- the field stays for scripted/eval callers.
    top_k: int | None = Field(default=None, ge=1, le=50)
    #: Confluence spaces to restrict retrieval to. Empty means every space.
    #: Ignored by sources that do not have spaces.
    space_keys: list[str] = Field(default_factory=list)


class MultiChatRequest(ChatRequest):
    """Ask one question across several sources at once."""

    sources: list[str] = Field(default_factory=list)


class ChatResponse(BaseModel):
    answer: str
    #: the single source that answered, or a comma-joined list for a fan-out
    source: str
    sources: list[SourceDocument] = Field(default_factory=list)
