"""Sector-rotation exit: a basket strategy with exit.rotate_on_rank_dropout
sells any holding that has fallen out of the current top-N ranking."""

import asyncio
import json
from unittest.mock import MagicMock

from qt.db import session_scope
from qt.services import engine


def _seed(session, *, rotate: bool, symbols: tuple[str, ...]):
    from qt.models import Strategy, Trade

    session.query(Trade).delete()
    session.query(Strategy).delete()
    strat = Strategy(
        name="Rot", asset_class="stock", universe="basket", top_n=2, rank_by="relative_strength",
        params=json.dumps({"entry": {}, "exit": {"rotate_on_rank_dropout": rotate}}),
    )
    session.add(strat)
    session.flush()
    trades = [
        Trade(strategy_id=strat.id, mode="paper", symbol=sym, asset_class="stock",
              status="open", qty=1, notional=10)
        for sym in symbols
    ]
    session.add_all(trades)
    session.commit()
    return trades


def _cleanup(session):
    from qt.models import Strategy, Trade

    session.query(Trade).delete()
    session.query(Strategy).delete()
    session.commit()


def test_rotation_flags_holdings_that_left_the_top_n(monkeypatch):
    # Current top-N ranks {AAA, BBB}; the held CCC has dropped out.
    async def fake_candidates(session, client, s):
        return [
            engine.Candidate(symbol="AAA", asset_class="stock", price=10, change_pct=1, vwap=None),
            engine.Candidate(symbol="BBB", asset_class="stock", price=10, change_pct=1, vwap=None),
        ]

    monkeypatch.setattr(engine, "_basket_candidates", fake_candidates)
    with session_scope() as s:
        held, kept = _seed(s, rotate=True, symbols=("CCC", "AAA"))
        reasons = asyncio.run(engine._rotation_dropout_reasons(s, MagicMock(), [held, kept]))
        assert held.id in reasons and kept.id not in reasons
        assert "top 2" in reasons[held.id]
        _cleanup(s)


def test_rotation_left_alone_when_flag_off(monkeypatch):
    async def boom(*a, **k):
        raise AssertionError("must not rank a strategy without the rotate flag")

    monkeypatch.setattr(engine, "_basket_candidates", boom)
    with session_scope() as s:
        (t,) = _seed(s, rotate=False, symbols=("CCC",))
        reasons = asyncio.run(engine._rotation_dropout_reasons(s, MagicMock(), [t]))
        assert reasons == {}
        _cleanup(s)


def test_rotation_holds_when_ranking_unavailable(monkeypatch):
    # If the live ranking can't be computed, never force a blind exit.
    async def empty(session, client, s):
        return []

    monkeypatch.setattr(engine, "_basket_candidates", empty)
    with session_scope() as s:
        (t,) = _seed(s, rotate=True, symbols=("CCC",))
        reasons = asyncio.run(engine._rotation_dropout_reasons(s, MagicMock(), [t]))
        assert reasons == {}
        _cleanup(s)
