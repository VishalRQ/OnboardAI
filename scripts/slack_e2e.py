"""End-to-end check for the Slack RAG flow: ingest -> chat -> incremental sync.

    python scripts/slack_e2e.py

Needs Ollama running with the configured models (see `/api/health`). With no
SLACK_BOT_TOKEN / SLACK_EXPORT_PATH it runs against the built-in fixture
threads, so it works offline. Point it at a real workspace by setting those
in .env; the flow is identical.
"""

import asyncio
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import get_settings
from app.schemas.chat import ChatRequest
from app.schemas.ingest import IngestRequest
from app.services.slack.service import get_slack_service

QUESTIONS = [
    "how do I fix `make dev` failing on an M-series Mac?",
    "why did we move off Stripe Connect for payouts?",
    "how do I get staging database access?",
    "what is the deploy rollback procedure?",
    "what is the capital of France?",  # must refuse: not in Slack
]


async def main() -> None:
    settings = get_settings()
    source = (
        "live Web API" if settings.slack_bot_token
        else f"export at {settings.slack_export_path}" if settings.slack_export_path
        else "built-in fixtures"
    )
    print(f"Slack source: {source}\n")

    # start clean so numbers are reproducible
    shutil.rmtree(settings.chroma_path, ignore_errors=True)
    svc = get_slack_service()

    print("1. full ingest")
    print("  ", (await svc.ingest(IngestRequest(full_refresh=True))).detail)

    print("2. re-ingest (incremental, nothing changed -> all skipped)")
    print("  ", (await svc.ingest(IngestRequest())).detail)

    print("\n3. questions")
    for q in QUESTIONS:
        resp = await svc.chat(ChatRequest(question=q, top_k=4))
        cites = ", ".join(
            f"{s.metadata.get('channel')} ({s.metadata.get('url') or 'no permalink'})"
            for s in resp.sources
        )
        print(f"\nQ: {q}\nA: {resp.answer}\n   sources: {cites or 'none'}")


if __name__ == "__main__":
    asyncio.run(main())
