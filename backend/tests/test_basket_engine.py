"""Engine candidate selection from a basket: snapshot → rank → top-N."""

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from qt.db import session_scope
from qt.models import Basket, BasketItem, Strategy
from qt.services.engine import _basket_candidates

PARAMS = json.dumps(
    {
        "entry": {"min_day_gain_pct": 0, "require_above_vwap": False,
                  "entry_window_start": None, "entry_window_end": None},
        "exit": {"trailing_stop_pct": 5, "stop_loss_pct": 4, "take_profit_pct": 0,
                 "max_holding_hours": 0, "flatten_before_close": False, "exit_below_vwap": False},
    }
)


def _snap(price, prev):
    return {"latestTrade": {"p": price}, "dailyBar": {"c": price, "vw": price * 0.99},
            "prevDailyBar": {"c": prev}}


def _make_basket_strategy(rank_by, top_n, symbols):
    with session_scope() as s:
        basket = Basket(name=f"engine-{rank_by}-{datetime.now().timestamp()}", builtin=False)
        s.add(basket)
        s.flush()
        for sym in symbols:
            s.add(BasketItem(basket_id=basket.id, symbol=sym, asset_class="stock"))
        strat = Strategy(
            name="engine basket", enabled=True, asset_class="stock", universe="basket",
            basket_id=basket.id, rank_by=rank_by, top_n=top_n, preset="custom",
            params=PARAMS, sizing_usd=200, sleeve_usd=1000, max_positions=3,
            swing_mode=True, ignore_regime=True,
        )
        s.add(strat)
        s.flush()
        return strat.id, basket.id


async def test_momentum_ranking_takes_top_n_no_bars_fetched():
    sid, bid = _make_basket_strategy("momentum_today", 2, ["AAA", "BBB", "CCC"])
    snaps = {
        "AAA": _snap(10.0, 9.8),   # +2.04%
        "BBB": _snap(10.0, 9.0),   # +11.1% (leader)
        "CCC": _snap(10.0, 9.5),   # +5.26%
    }
    client = SimpleNamespace(
        stock_snapshots=AsyncMock(return_value=snaps),
        historical_bars=AsyncMock(return_value={}),  # must NOT be called
    )
    with session_scope() as s:
        strat = s.get(Strategy, sid)
        cands = await _basket_candidates(s, client, strat)

    assert [c.symbol for c in cands] == ["BBB", "CCC"]  # top 2 by today's %
    client.historical_bars.assert_not_awaited()

    with session_scope() as s:
        s.query(BasketItem).filter(BasketItem.basket_id == bid).delete()
        s.query(Strategy).filter(Strategy.id == sid).delete()
        s.query(Basket).filter(Basket.id == bid).delete()


async def test_return_30d_ranking_uses_daily_bars():
    sid, bid = _make_basket_strategy("return_30d", 1, ["AAA", "BBB"])
    snaps = {"AAA": _snap(12.0, 11.9), "BBB": _snap(20.0, 19.9)}

    def bars(base_now, base_old):
        old_t = (datetime.now(timezone.utc) - timedelta(days=40)).strftime("%Y-%m-%dT00:00:00Z")
        new_t = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT00:00:00Z")
        return [{"t": old_t, "c": base_old, "h": base_old, "l": base_old},
                {"t": new_t, "c": base_now, "h": base_now, "l": base_now}]

    # AAA: 10 -> 12 (+20% over 30d); BBB: 19 -> 20 (+5.26%). AAA should win.
    hist = {"AAA": bars(12.0, 10.0), "BBB": bars(20.0, 19.0)}
    client = SimpleNamespace(
        stock_snapshots=AsyncMock(return_value=snaps),
        historical_bars=AsyncMock(return_value=hist),
    )
    with session_scope() as s:
        strat = s.get(Strategy, sid)
        cands = await _basket_candidates(s, client, strat)

    client.historical_bars.assert_awaited_once()
    assert [c.symbol for c in cands] == ["AAA"]

    with session_scope() as s:
        s.query(BasketItem).filter(BasketItem.basket_id == bid).delete()
        s.query(Strategy).filter(Strategy.id == sid).delete()
        s.query(Basket).filter(Basket.id == bid).delete()


async def test_empty_basket_yields_no_candidates():
    sid, bid = _make_basket_strategy("momentum_today", 5, [])
    client = SimpleNamespace(stock_snapshots=AsyncMock(return_value={}), historical_bars=AsyncMock())
    with session_scope() as s:
        strat = s.get(Strategy, sid)
        cands = await _basket_candidates(s, client, strat)
    assert cands == []
    client.stock_snapshots.assert_not_awaited()
    with session_scope() as s:
        s.query(Strategy).filter(Strategy.id == sid).delete()
        s.query(Basket).filter(Basket.id == bid).delete()
