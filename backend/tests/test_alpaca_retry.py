"""Reads survive a rate limit; orders are never retried.

A 429 killed a comparison backtest that had already run for minutes. Alpaca's
free plan allows 200 requests/minute and a long two-strategy run brushes it —
that's "wait a moment", not a failure.

The other half matters more: _post places ORDERS. Retrying one after an
ambiguous response is how you end up holding a position twice, so the retry
lives on _get alone and this file pins that.
"""

import asyncio

import httpx
import pytest

from qt.broker.alpaca import AlpacaClient, AlpacaError


class _Fake:
    """Stands in for httpx.AsyncClient, replaying a scripted list of responses."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def _respond(self, *a, **kw):
        self.calls += 1
        status, headers = self.script.pop(0) if self.script else (200, {})
        return httpx.Response(status, json={"ok": True}, headers=headers,
                              request=httpx.Request("GET", "https://x"))

    get = _respond
    post = _respond


@pytest.fixture()
def no_sleep(monkeypatch):
    """Backoff without the wait — the schedule is asserted, not endured."""
    slept: list[float] = []

    async def fake_sleep(s):
        slept.append(s)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    return slept


def _patch(monkeypatch, fake):
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: fake)


def test_a_rate_limited_read_retries_and_succeeds(monkeypatch, no_sleep):
    fake = _Fake([(429, {}), (429, {}), (200, {})])
    _patch(monkeypatch, fake)
    out = asyncio.run(AlpacaClient("k", "s")._get("/v2/clock"))
    assert out == {"ok": True}
    assert fake.calls == 3
    assert no_sleep == [1.0, 3.0]  # widening backoff


def test_alpacas_own_retry_after_wins_over_our_backoff(monkeypatch, no_sleep):
    fake = _Fake([(429, {"Retry-After": "7"}), (200, {})])
    _patch(monkeypatch, fake)
    asyncio.run(AlpacaClient("k", "s")._get("/v2/clock"))
    assert no_sleep == [7.0]


def test_a_nonsense_retry_after_falls_back_to_the_backoff(monkeypatch, no_sleep):
    """The header may be an HTTP date. Don't crash on it."""
    fake = _Fake([(429, {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}), (200, {})])
    _patch(monkeypatch, fake)
    asyncio.run(AlpacaClient("k", "s")._get("/v2/clock"))
    assert no_sleep == [1.0]


def test_giving_up_explains_what_to_do(monkeypatch, no_sleep):
    fake = _Fake([(429, {})] * 6)
    _patch(monkeypatch, fake)
    with pytest.raises(AlpacaError) as exc:
        asyncio.run(AlpacaClient("k", "s")._get("/v2/clock"))
    assert exc.value.status_code == 429
    msg = str(exc.value).lower()
    assert "200 requests a minute" in msg and "shorter period" in msg
    assert fake.calls == 4  # bounded, not forever


def test_a_client_error_is_not_retried(monkeypatch, no_sleep):
    """A 422 is our bug, not a hiccup. Retrying it just wastes the budget."""
    fake = _Fake([(422, {}), (200, {})])
    _patch(monkeypatch, fake)
    with pytest.raises(AlpacaError):
        asyncio.run(AlpacaClient("k", "s")._get("/v2/clock"))
    assert fake.calls == 1


def test_orders_are_never_retried(monkeypatch, no_sleep):
    """THE important one. _post submits orders; a retry after an ambiguous
    response is how a single intent becomes two positions."""
    fake = _Fake([(429, {}), (200, {})])
    _patch(monkeypatch, fake)
    with pytest.raises(AlpacaError):
        asyncio.run(AlpacaClient("k", "s")._post("/v2/orders", {"symbol": "AAPL"}))
    assert fake.calls == 1, "an order was retried"
    assert no_sleep == []
