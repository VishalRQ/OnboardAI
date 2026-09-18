from fastapi import APIRouter, Depends, HTTPException

from app.schemas.chat import ChatRequest, ChatResponse
from app.schemas.ingest import IngestRequest, IngestResponse
from app.services.common.vectorstore import list_chunks
from app.services.slack import SlackService, get_slack_service
from slack_sdk.errors import SlackApiError

from app.services.slack.client import SlackAuthError

router = APIRouter(prefix="/slack", tags=["slack"])


@router.get("/channels")
async def channels(service: SlackService = Depends(get_slack_service)) -> dict:
    """Channels the UI can offer, each with its indexed chunk count."""
    try:
        return {"channels": service.channels()}
    except SlackAuthError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/chunks")
async def chunks(
    limit: int = 50,
    offset: int = 0,
    service: SlackService = Depends(get_slack_service),
) -> dict:
    """Page through what is actually indexed. Chroma has no UI of its own."""
    return list_chunks(service.collection, limit=limit, offset=offset)


@router.post("/ingest", response_model=IngestResponse)
async def ingest(
    request: IngestRequest,
    service: SlackService = Depends(get_slack_service),
) -> IngestResponse:
    """Index Slack messages.

    Body `options`: `channels` (list of names/ids), `since` (Slack ts, live
    path only), `export_path` (override SLACK_EXPORT_PATH). `full_refresh`
    re-indexes every unit and reconciles deletions for the covered channels.
    """
    try:
        return await service.ingest(request)
    except SlackAuthError as exc:
        # bad token or a missing OAuth scope: the workspace refused us, and
        # nothing the reader retries will change that until the app is fixed
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except ValueError as exc:
        # e.g. no channel matched: the request asked for something that is not
        # there, which is a 400 the UI can show verbatim -- not a crash
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except SlackApiError as exc:
        # the workspace refused a call (not_in_channel, rate limits, ...).
        # Surface Slack's own error code: it names the fix, a 500 does not.
        code = (exc.response or {}).get("error", "slack_api_error")
        detail = f"Slack refused the request: {code}"
        if code == "not_in_channel":
            detail += ". Invite the bot with /invite @<app> in that channel."
        raise HTTPException(status_code=502, detail=detail) from exc


@router.post("/chat", response_model=ChatResponse)
async def chat(
    request: ChatRequest,
    service: SlackService = Depends(get_slack_service),
) -> ChatResponse:
    return await service.chat(request)
