"""Confluence sync bookkeeping that needs no network, Chroma or Ollama.

Run: `pytest tests/test_confluence_sync.py`
The service's I/O seams (loader, vector store) are swapped for in-memory
fakes; the sync-state file is real, in a temp dir.
"""

import pytest

from app.schemas.ingest import IngestRequest
from app.services.confluence import service as confluence_service
from app.services.confluence import sync_state
from app.services.confluence.loader import _fixture


class FakeStore:
    def __init__(self):
        self.added: list[str] = []

    def add_texts(self, texts, metadatas, ids):
        self.added.extend(ids)


@pytest.fixture
def svc(tmp_path, monkeypatch):
    """A ConfluenceService whose spaces are {"REAL": one page, anything else: none}."""
    monkeypatch.setattr(sync_state, "_state_path", lambda: tmp_path / "state.json")
    body = "Open a ticket in the service desk portal to request a laptop. " * 20
    pages = {"REAL": [_fixture("1", "Laptop setup", "Home", [], body, space="REAL")]}
    store = FakeStore()
    monkeypatch.setattr(
        confluence_service, "load_pages",
        lambda keys, since=None: [p for k in keys for p in pages.get(k, [])],
    )
    monkeypatch.setattr(
        confluence_service, "load_page_ids",
        lambda keys: {p.metadata["page_id"] for k in keys for p in pages.get(k, [])},
    )
    monkeypatch.setattr(confluence_service, "get_vectorstore", lambda _: store)
    monkeypatch.setattr(
        confluence_service, "field_maps",
        lambda *_: {"version": {}, "space_key": {}},
    )
    monkeypatch.setattr(confluence_service, "delete_by_metadata", lambda *_: 0)
    monkeypatch.setattr(confluence_service, "count", lambda _: len(store.added))
    monkeypatch.setattr(confluence_service, "invalidate_space_cache", lambda: None)
    return confluence_service.ConfluenceService()


def ingest(svc, *keys, full=False):
    return svc._ingest_blocking(
        IngestRequest(full_refresh=full, options={"space_keys": list(keys)})
    )


def watermarked() -> set[str]:
    return set(sync_state.read_state().get("spaces", {}))


def test_space_with_pages_gets_a_watermark(svc):
    result = ingest(svc, "REAL", full=True)
    assert result.documents_ingested == 1
    assert watermarked() == {"REAL"}


def test_unknown_space_gets_no_watermark(svc):
    # a mistyped key returns nothing from the beginning of time; recording it
    # as synced would hide every page of a space later created under that key
    result = ingest(svc, "REAL", "NOPE", full=True)
    assert result.documents_ingested == 1
    assert watermarked() == {"REAL"}
    assert sync_state.since_watermark(sync_state.read_state(), "NOPE") is None


def test_quiet_incremental_still_advances_the_watermark(svc, monkeypatch):
    # zero pages on a *delta* window means "nothing changed", which is a
    # clean pass -- unlike zero pages on a full window
    ingest(svc, "REAL", full=True)
    before = sync_state.read_state()["spaces"]["REAL"]["last_synced_at"]
    full_window = confluence_service.load_pages
    monkeypatch.setattr(
        confluence_service, "load_pages",
        lambda keys, since=None: [] if since else full_window(keys),
    )
    result = ingest(svc, "REAL")
    assert result.documents_ingested == 0
    assert "since=last sync" in result.detail
    assert sync_state.read_state()["spaces"]["REAL"]["last_synced_at"] >= before


def test_blank_request_ingests_every_listed_space(svc, monkeypatch):
    # no space list in config any more: naming no spaces means "all of them",
    # asked of Confluence at run time
    monkeypatch.setattr(
        confluence_service, "list_spaces",
        lambda: [{"key": "REAL", "name": "Real"}, {"key": "EMPTY", "name": "Empty"}],
    )
    result = svc._ingest_blocking(IngestRequest(full_refresh=True))
    assert "spaces=REAL,EMPTY" in result.detail
    assert result.documents_ingested == 1


def test_blank_request_with_no_visible_spaces_is_an_error(svc, monkeypatch):
    monkeypatch.setattr(confluence_service, "list_spaces", lambda: [])
    with pytest.raises(ValueError, match="No Confluence spaces"):
        svc._ingest_blocking(IngestRequest())
