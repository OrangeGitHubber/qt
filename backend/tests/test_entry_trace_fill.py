"""The "last run" trace must report what the ORDER did, not what the rails allowed.

The trace used to record a candidate as "bought" BEFORE execution.open_trade was
called. That function declines in five ways — below Alpaca's $1 minimum, zero
whole shares, a broker rejection, an order that never filled, a fill poll that
timed out — and each returns None after journalling a rejected Trade with the
real reason. The trace never heard about any of it.

So a symbol whose order kept failing showed "bought · all rails passed" on every
run, forever: it never became a position, so the "already holds this symbol"
rail never blocked it, so it was retried and mis-reported a minute later
(reported against RENDER/USD). The summary line counted it as a buy too.

Both directions are pinned below — a trace that always says "not filled" is
exactly as wrong as one that always says "bought", and only the pair rules out
both.
"""

import asyncio
import json
from unittest.mock import MagicMock

import pytest

from qt.db import session_scope
from qt.models import Strategy, Trade
from qt.services import engine, execution, persistence

SYMBOL = "TRACEFILL/USD"  # unique to this module: the "already open" rail is account-wide


@pytest.fixture()
def strategy_row():
    # Crypto on a custom universe: no market-hours gate, no regime gate and no
    # wash-sale lookup, so the run reaches the buy branch on its own merits
    # rather than on mocks of the surrounding rails.
    with session_scope() as s:
        strat = Strategy(
            name="trace fill test", enabled=True, asset_class="crypto", universe="custom",
            symbols=json.dumps([SYMBOL]), preset="custom",
            params='{"entry":{},"exit":{"stop_loss_pct":4}}',
            sizing_usd=200, sleeve_usd=1000, max_positions=3, swing_mode=True, ignore_regime=False,
        )
        s.add(strat)
        s.flush()
        sid = strat.id
    yield sid
    engine._last_run.pop(sid, None)
    with session_scope() as s:
        s.query(Trade).filter(Trade.strategy_id == sid).delete()
        s.query(Strategy).filter(Strategy.id == sid).delete()


def _run(monkeypatch, sid: int, open_trade) -> dict:
    """Drive one entry cycle with exactly ONE candidate reaching the buy branch
    with every rail passed, and `open_trade` deciding the outcome. Returns the
    trace the engine stored for this strategy."""
    monkeypatch.setattr(persistence, "boot_state", lambda: {"data_persistent": None})

    # The rails read the WHOLE journal (every open paper trade, the day's entry
    # count, the day's realised loss), so leftovers from other modules could
    # block the buy and make this test pass for the wrong reason. Generous
    # limits keep the portfolio-wide rails inert; the rail logic itself is
    # covered by test_engine_rules / test_concurrent_symbol.
    monkeypatch.setattr(engine, "get_risk", lambda session: {
        **engine.RISK_DEFAULTS,
        "max_daily_loss_usd": 1e9, "max_daily_loss_pct": 100.0,
        "max_total_positions": 999, "max_total_exposure_usd": 1e9,
        "max_trades_per_day": 999, "cooldown_hours_after_loss": 0,
    })

    cand = engine.Candidate(
        symbol=SYMBOL, asset_class="crypto", price=2.50, change_pct=6.0, vwap=2.40
    )

    async def fake_candidates(session, client, strategy, scan_result):
        # Only OUR strategy gets the candidate — another enabled strategy left
        # behind by an earlier test must not be handed it and start trading.
        return ([cand] if strategy.id == sid else []), scan_result

    monkeypatch.setattr(engine, "_candidates_for", fake_candidates)
    monkeypatch.setattr(execution, "open_trade", open_trade)

    with session_scope() as session:
        asyncio.run(
            engine._consider_entries(session, MagicMock(), "paper", 100_000.0, True, False)
        )
    return engine._last_run[sid]


def test_a_declined_order_is_traced_as_not_filled_with_the_real_reason(monkeypatch, strategy_row):
    async def declines(session, client, strategy, version_id, mode, cand, reason, sizing_usd=None):
        # Exactly what the real open_trade does on all five failure paths:
        # journal a rejected Trade carrying the obstacle, then return None.
        session.add(
            Trade(
                strategy_id=strategy.id, mode=mode, symbol=cand.symbol,
                asset_class=cand.asset_class, qty=0, notional=0, status="rejected",
                entry_reason="order did not fill in time — cancelled",
            )
        )
        return None

    trace = _run(monkeypatch, strategy_row, declines)

    row = trace["candidates"][0]
    assert row["symbol"] == SYMBOL
    assert row["decision"] == "not filled", "an order that never filled was reported as bought"
    # The panel must name the actual obstacle, not just "the order didn't complete".
    assert "did not fill" in row["reason"]
    # The summary counted rail-passers, not fills — same bug, second symptom.
    assert trace["outcome"] == "Evaluated 1 candidate(s); bought 0."


def test_a_filled_order_is_still_traced_as_bought(monkeypatch, strategy_row):
    """The other direction. Without this, hard-coding "not filled" would pass —
    and the trace would be just as untrue, in the opposite way."""

    async def fills(session, client, strategy, version_id, mode, cand, reason, sizing_usd=None):
        trade = Trade(
            strategy_id=strategy.id, mode=mode, symbol=cand.symbol,
            asset_class=cand.asset_class, qty=80, notional=200.0, status="open",
            entry_price=cand.price, entry_reason=reason,
        )
        session.add(trade)
        return trade

    trace = _run(monkeypatch, strategy_row, fills)

    row = trace["candidates"][0]
    assert row["decision"] == "bought"
    assert "all rails passed" in row["reason"]
    assert trace["outcome"] == "Evaluated 1 candidate(s); bought 1."
