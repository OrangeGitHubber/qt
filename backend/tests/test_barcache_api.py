"""The bar-cache trigger endpoints: reconstruct-only (re-rank without a
re-download) and the shared 'one at a time' guard."""

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from qt.api import barcache as barcache_api
from qt.services import barcache


def _mem_cache(monkeypatch):
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    barcache.CacheBase.metadata.create_all(eng)
    monkeypatch.setattr(barcache, "_engine", eng)
    monkeypatch.setattr(barcache, "_Session", sessionmaker(bind=eng, expire_on_commit=False))


def test_reconstruct_only_starts_without_a_download(client, monkeypatch):
    _mem_cache(monkeypatch)
    monkeypatch.setattr(barcache_api._progress, "running", False)
    r = client.post("/api/barcache/reconstruct")
    assert r.status_code == 200
    body = r.json()
    assert body["started"] is True and body["reconstruct_only"] is True


def test_reconstruct_refuses_while_a_run_is_in_progress(client, monkeypatch):
    # The sweep and reconstruct share one worker slot — no concurrent runs.
    monkeypatch.setattr(barcache_api._progress, "running", True)
    r = client.post("/api/barcache/reconstruct")
    assert r.status_code == 409
