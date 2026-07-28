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
        rank_enabled=True,  # a basket is always ranked; rotation needs a top-N to fall out of
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
    async def fake_top(session, client, s):
        return {"AAA", "BBB"}

    monkeypatch.setattr(engine, "_ranked_symbols_now", fake_top)
    with session_scope() as s:
        held, kept = _seed(s, rotate=True, symbols=("CCC", "AAA"))
        reasons = asyncio.run(engine._rotation_dropout_reasons(s, MagicMock(), [held, kept]))
        assert held.id in reasons and kept.id not in reasons
        assert "top 2" in reasons[held.id]
        _cleanup(s)


def test_rotation_left_alone_when_flag_off(monkeypatch):
    async def boom(*a, **k):
        raise AssertionError("must not rank a strategy without the rotate flag")

    monkeypatch.setattr(engine, "_ranked_symbols_now", boom)
    with session_scope() as s:
        (t,) = _seed(s, rotate=False, symbols=("CCC",))
        reasons = asyncio.run(engine._rotation_dropout_reasons(s, MagicMock(), [t]))
        assert reasons == {}
        _cleanup(s)


def test_rotation_holds_when_ranking_unavailable(monkeypatch):
    # If the live ranking can't be computed, never force a blind exit.
    async def empty(session, client, s):
        return set()

    monkeypatch.setattr(engine, "_ranked_symbols_now", empty)
    with session_scope() as s:
        (t,) = _seed(s, rotate=True, symbols=("CCC",))
        reasons = asyncio.run(engine._rotation_dropout_reasons(s, MagicMock(), [t]))
        assert reasons == {}
        _cleanup(s)


def test_rotation_now_works_for_ranked_watchlist(monkeypatch):
    # Rotation is no longer basket-only: a ranked WATCHLIST strategy also rotates
    # out a holding that fell out of its top-N.
    from qt.models import Strategy, Trade

    async def fake_top(session, client, s):
        return {"AAA"}

    monkeypatch.setattr(engine, "_ranked_symbols_now", fake_top)
    with session_scope() as s:
        s.query(Trade).delete()
        s.query(Strategy).delete()
        strat = Strategy(
            name="WLRot", asset_class="stock", universe="watchlist", top_n=1,
            rank_by="relative_strength", rank_enabled=True,
            params=json.dumps({"entry": {}, "exit": {"rotate_on_rank_dropout": True}}),
        )
        s.add(strat)
        s.flush()
        held = Trade(strategy_id=strat.id, mode="paper", symbol="ZZZ", asset_class="stock",
                     status="open", qty=1, notional=10)
        s.add(held)
        s.commit()
        reasons = asyncio.run(engine._rotation_dropout_reasons(s, MagicMock(), [held]))
        assert held.id in reasons
        _cleanup(s)
