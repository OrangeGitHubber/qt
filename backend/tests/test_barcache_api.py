"""The bar-cache trigger endpoints: reconstruct-only (re-rank without a
re-download) and the shared 'one at a time' guard."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from qt import security
from qt.api import barcache as barcache_api
from qt.broker.alpaca import SECRET_KEY_ID, SECRET_KEY_SECRET
from qt.db import session_scope
from qt.services import barcache


@pytest.fixture()
def configured(client):
    with session_scope() as s:
        security.set_secret(s, SECRET_KEY_ID, "k")
        security.set_secret(s, SECRET_KEY_SECRET, "s")
    yield
    with session_scope() as s:
        security.delete_secret(s, SECRET_KEY_ID)
        security.delete_secret(s, SECRET_KEY_SECRET)


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


def test_intraday_sweep_refuses_while_a_run_is_in_progress(client, configured, monkeypatch):
    monkeypatch.setattr(barcache_api._progress, "running", True)
    r = client.post("/api/barcache/sweep-intraday")
    assert r.status_code == 409


def test_status_reports_has_intraday(client, monkeypatch):
    _mem_cache(monkeypatch)
    monkeypatch.setattr(barcache_api._progress, "running", False)
    assert client.get("/api/barcache/status").json()["has_intraday"] is False
    with barcache.session() as s:
        barcache.save_intraday_bars(s, "AAA", [
            {"t": "2026-06-01T14:00:00Z", "o": 10, "h": 10, "l": 10, "c": 10, "v": 1e5, "vw": 10},
        ])
        s.commit()
    assert client.get("/api/barcache/status").json()["has_intraday"] is True


def test_crypto_sweep_refuses_while_a_run_is_in_progress(client, configured, monkeypatch):
    """Crypto shares the same single worker slot as the stock sweeps."""
    monkeypatch.setattr(barcache_api._progress, "running", True)
    assert client.post("/api/barcache/sweep-crypto").status_code == 409
    assert client.post("/api/barcache/sweep-crypto-intraday").status_code == 409


def test_status_reports_crypto_cache_separately(client, monkeypatch):
    """/status surfaces the crypto cache independently of the stock cache."""
    _mem_cache(monkeypatch)
    monkeypatch.setattr(barcache_api._progress, "running", False)
    body = client.get("/api/barcache/status").json()
    assert body["crypto_has_intraday"] is False
    assert body["crypto_cache"]["daily_symbols"] == 0
    with barcache.session() as s:
        barcache.save_daily_bars(s, "BTC/USD", [
            {"t": "2026-06-01T00:00:00Z", "o": 10, "h": 10, "l": 10, "c": 10, "v": 1e5, "vw": 10},
        ], model=barcache.CryptoDailyBar)
        barcache.save_intraday_bars(s, "BTC/USD", [
            {"t": "2026-06-01T12:00:00Z", "o": 10, "h": 10, "l": 10, "c": 10, "v": 1e5, "vw": 10},
        ], model=barcache.CryptoIntradayBar)
        s.commit()
    body = client.get("/api/barcache/status").json()
    assert body["crypto_has_intraday"] is True
    assert body["crypto_cache"]["daily_symbols"] == 1
    assert body["has_intraday"] is False  # stock cache still empty
