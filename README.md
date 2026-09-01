# OnboardAI

RAG backend over internal onboarding knowledge. Each knowledge source
(Confluence, Slack, ...) is its own service package behind a shared contract.

Current state: **bootstrapped scaffold**. Every route works end to end, but
ingestion, embedding and retrieval return stub data — the real LangChain /
Ollama / vector-store calls are marked with commented-out lines at each seam.

## Layout

```
app/
├── main.py                  FastAPI app, CORS, lifespan; mounts /api
├── core/
│   ├── config.py            Settings (pydantic-settings, reads .env)
│   └── logging.py
├── schemas/
│   ├── chat.py              ChatRequest / ChatResponse / SourceDocument
│   └── ingest.py            IngestRequest / IngestResponse
├── api/routes/
│   ├── __init__.py          aggregates every router
│   ├── health.py
│   ├── chat.py              source-agnostic: /api/chat/{source}
│   ├── confluence.py
│   └── slack.py
└── services/
    ├── llm_service.py       shared Ollama LLM + embeddings
    ├── common/
    │   ├── base.py          BaseRAGService — the per-service contract
    │   └── vectorstore.py   Chroma (proto) / Qdrant (prod) factory
    ├── confluence/
    │   ├── loader.py        atlassian-python-api / ConfluenceLoader
    │   └── service.py
    └── slack/
        ├── loader.py        export reader now, slack-sdk sync later
        └── service.py
```

## Run

```bash
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # then fill in tokens
uvicorn app.main:app --reload
```

Docs at http://localhost:8000/docs

## Endpoints

| Method | Path                      | Purpose                          |
|--------|---------------------------|----------------------------------|
| GET    | `/api/health`             | status + active config           |
| GET    | `/api/chat/sources`       | list registered services         |
| POST   | `/api/chat/{source}`      | chat against any source by name  |
| POST   | `/api/confluence/ingest`  | index Confluence spaces          |
| POST   | `/api/confluence/chat`    | chat over Confluence             |
| POST   | `/api/slack/ingest`       | index Slack export / incremental |
| POST   | `/api/slack/chat`         | chat over Slack                  |

```bash
curl -X POST localhost:8000/api/confluence/chat \
  -H 'content-type: application/json' \
  -d '{"question":"How do I get laptop access?","top_k":4}'
```

## Adding a third service

1. `app/services/<name>/` with `loader.py` + `service.py`; subclass
   `BaseRAGService` and set `name` / `collection`.
2. `app/api/routes/<name>.py` — copy `slack.py`, swap the dependency.
3. Register the router in `app/api/routes/__init__.py` and add the service to
   the registry in `app/api/routes/chat.py`.

## Next steps

- Wire `llm_service.get_llm` / `get_embeddings` to `langchain-ollama`.
- Wire `common/vectorstore.get_vectorstore` to Chroma, then Qdrant.
- Real Confluence loader + chunking; real Slack export parser.
- Contextual Retrieval (context-augmented chunks, hybrid search, rank fusion)
  in `common/` so both services get it.
- Streamlit testing UI; Ragas eval harness.
