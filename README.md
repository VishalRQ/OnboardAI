# OnboardAI

RAG backend over internal onboarding knowledge. Each knowledge source
(Confluence, Slack, ...) is its own service package behind a shared contract.

Current state: **Confluence works end to end** — live CQL ingestion, chunking,
Ollama embeddings, Chroma retrieval and grounded answers, with incremental
sync and deletion reconciliation. A Streamlit UI (`streamlit_app.py`) drives
the whole thing. Slack is a separate branch and is not wired up here.

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
│   └── confluence.py
└── services/
    ├── llm_service.py       shared Ollama LLM + embeddings
    ├── common/
    │   ├── base.py          BaseRAGService — the per-service contract
    │   ├── chunking.py      heading-aware splitting, shared by every source
    │   └── vectorstore.py   Chroma (proto) / Qdrant (prod) factory
    └── confluence/
        ├── client.py        Confluence Cloud REST (CQL, pagination, backoff)
        ├── html_to_text.py  storage-format XHTML → markdown-ish text
        ├── loader.py        spaces → pages → RawDocument
        ├── sync_state.py    watermark + run lock for incremental sync
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
| GET    | `/api/health`             | status + active config (incl. `top_k`) |
| GET    | `/api/chat/sources`       | list registered services         |
| POST   | `/api/chat`               | **fan-out**: one question, several sources |
| POST   | `/api/chat/{source}`      | chat against any source by name  |
| GET    | `/api/confluence/spaces`  | pickable spaces + indexed chunk counts |
| GET    | `/api/confluence/status`  | what is indexed and when it last synced |
| GET    | `/api/confluence/chunks`  | page through the index           |
| POST   | `/api/confluence/ingest`  | index Confluence spaces          |
| POST   | `/api/confluence/chat`    | chat over Confluence             |

```bash
# one source, restricted to two spaces
curl -X POST localhost:8000/api/confluence/chat \
  -H 'content-type: application/json' \
  -d '{"question":"How do I get laptop access?","space_keys":["ENG"]}'

# every selected source at once (Confluence alone, until a second one lands)
curl -X POST localhost:8000/api/chat \
  -H 'content-type: application/json' \
  -d '{"question":"How do I get laptop access?","sources":["confluence"]}'
```

Retrieval depth is `TOP_K` in `.env`, not a request field: it trades answer
breadth against context budget and latency, so it is an operator setting. A
request may still override it (`"top_k": 8`) for scripted evaluation.

## Ingestion

`POST /api/confluence/ingest` is incremental by default. It asks Confluence
only for pages modified since the last clean run (watermark in
`data/confluence_sync_state.json`, minus a 15-minute overlap), skips any page
whose version is already indexed, and about daily diffs the full page-id list
to purge pages deleted at the source. `{"full_refresh": true}` re-reads
everything. A run holds a lock file; a second concurrent run gets a `409`.

## Adding a second service

1. `app/services/<name>/` with `loader.py` + `service.py`; subclass
   `BaseRAGService` and set `name` / `collection`.
2. `app/api/routes/<name>.py` — copy `confluence.py`, swap the dependency.
3. Register the router in `app/api/routes/__init__.py` and add the service to
   the registry in `app/api/routes/chat.py`.

## UI

```bash
streamlit run streamlit_app.py     # alongside uvicorn
```

It reads the same `.env` as the backend; point it at another host with
`ONBOARDAI_API_URL`. The source picker is filled from `/api/chat/sources`, so
it lists whatever the backend registers. Narrow to specific Confluence spaces
from a dropdown, and run ingestion. Answers cite one line per source document —
name, space and link, no excerpt.

## Next steps

- Merge the Slack service (owned separately, `develop/slack`).
- Qdrant backend behind the existing `get_vectorstore` seam.
- Contextual Retrieval (context-augmented chunks, hybrid search, rank fusion)
  in `common/` so both services get it.
- launchd agent for the scheduled sync; Ragas eval harness.
