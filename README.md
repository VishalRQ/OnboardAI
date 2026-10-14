# OnboardAI

Ask questions about your team's onboarding knowledge and get answers grounded
in **Confluence** pages and **Slack** threads, with a link to every source.

- **Backend:** FastAPI (`app/`): ingestion, retrieval, answers
- **UI:** Streamlit (`streamlit_app.py`)
- **Models:** Ollama, local LLM + embeddings
- **Index:** Chroma, stored in `data/`

## Setup

You need Python 3.11 and [Ollama](https://ollama.com).

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

ollama pull llama3.2:1b
ollama pull nomic-embed-text

cp .env.example .env
```

Then fill in `.env`:

| Setting                | What to put                              |
| ---------------------- | ---------------------------------------- |
| `CONFLUENCE_URL`       | `https://<your-site>.atlassian.net/wiki` |
| `CONFLUENCE_USERNAME`  | the email you log in to Confluence with  |
| `CONFLUENCE_API_TOKEN` | an Atlassian API token                   |
| `SLACK_BOT_TOKEN`      | the bot token (`xoxb-…`)                 |

Without credentials, each source falls back to built-in sample data, so you
can try the app offline.

## Run

Start each in its own terminal (skip `ollama serve` if the Ollama app is
already running):

```bash
ollama serve
uvicorn app.main:app --reload --reload-dir app
streamlit run streamlit_app.py
```

- UI: http://localhost:8501
- API docs: http://localhost:8000/docs

## Using the app

1. **Search in:** pick Confluence, Slack, or both. Optionally narrow the
   search to some Confluence spaces or Slack channels.
2. **Update index:** tick the sources to refresh, optionally pick which
   spaces or channels (blank = all), and press **Update**.
   - Slack only offers channels the bot has been invited to
     (`/invite @<bot>` in the channel). It is rate-limited, so picking just
     the channels that changed is much faster than updating all of them.
   - Updates run in the background: keep chatting, and a popup says when
     each source is done.
   - Updates only fetch what changed. Tick **Full refresh** to re-index
     everything.
3. **Ask** in the chat box. Each answer lists its sources.

## Useful settings

All settings live in `.env`; see `.env.example` for the full list.

| Setting                      | Default       | Effect                                                                     |
| ---------------------------- | ------------- | -------------------------------------------------------------------------- |
| `LLM_MODEL`                  | `llama3.2:1b` | answer model; `llama3.1` writes better answers but is slower               |
| `TOP_K`                      | `4`           | chunks retrieved per source for each question                              |
| `MAX_RETRIEVAL_DISTANCE`     | `0.7`         | relevance cutoff; raise it slightly if good questions get "could not find" |
| `SLACK_MIN_REQUEST_INTERVAL` | `0`           | seconds between Slack calls; set `60` for a new Slack app                  |

## API

| Method | Path                             | Purpose                                 |
| ------ | -------------------------------- | --------------------------------------- |
| GET    | `/api/health`                    | backend and Ollama status               |
| POST   | `/api/chat`                      | ask across several sources              |
| POST   | `/api/{confluence,slack}/chat`   | ask one source                          |
| POST   | `/api/{confluence,slack}/ingest` | update a source, waiting for the result |
| POST   | `/api/jobs/ingest/{source}`      | start an update in the background       |
| GET    | `/api/jobs`, `/api/jobs/{id}`    | background update status                |
| GET    | `/api/confluence/spaces`         | spaces, with indexed chunk counts       |
| GET    | `/api/confluence/status`         | what is indexed, last sync times        |
| GET    | `/api/slack/channels`            | channels, with bot membership           |
| GET    | `/api/{confluence,slack}/chunks` | browse the index                        |

```bash
curl -X POST localhost:8000/api/chat \
  -H 'content-type: application/json' \
  -d '{"question":"What is agentic AI?","sources":["confluence","slack"]}'
```

## Project layout

```
app/
├── api/routes/     HTTP endpoints, one file per source + chat + health
├── core/           settings (.env) and logging
├── schemas/        request / response models
└── services/
    ├── common/     shared chunking, vector store, service contract
    ├── confluence/ Confluence client, loader, incremental sync
    ├── slack/      Slack client, loader, message grouping
    └── llm_service.py
streamlit_app.py    the UI
tests/              unit tests (no network needed)
```

To add a new source, create `app/services/<name>/` subclassing
`BaseRAGService`, add it to `app/services/registry.py`, and add a route file
registered in `app/api/routes/__init__.py`.
