"""What the journal shows, and in what order.

Two faults, one symptom — "I sold it, it's gone from open positions, and the
journal doesn't show it":

1. Rows were ordered by Trade.id, i.e. CREATION order. A position opened last
   week and sold a minute ago sorted as a week old.
2. The "All" view applies no status filter, and the engine logs hundreds of
   REJECTED candidates a day. Those filled the row cap completely, so no
   executed trade survived to reach the browser.
"""

from datetime import datetime, timedelta, timezone

import pytest

from qt.db import session_scope
from qt.models import Strategy, StrategyConfigVersion, Trade
from qt.settings_service import set_setting

NOW = datetime.now(timezone.utc)


@pytest.fixture()
def seeded(client):
    with session_scope() as s:
        s.query(Trade).delete()
        s.query(StrategyConfigVersion).delete()
        s.query(Strategy).delete()
        set_setting(s, "engine_mode", "paper")
        set_setting(s, "current_account_id", None)
        strat = Strategy(name="S", asset_class="crypto", universe="custom", preset="custom",
                         params="{}", symbols="[]")
        s.add(strat)
        s.flush()
        sid = strat.id

        # An OLD position — opened a week ago, so it gets a LOW id...
        s.add(Trade(strategy_id=sid, mode="paper", symbol="ADA/USD", asset_class="crypto",
                    status="open", qty=564.74, notional=100, entry_price=0.17,
                    entry_at=NOW - timedelta(days=7)))
        s.flush()
        # ...then a flood of rejections, all with HIGHER ids, as a real day produces.
        for i in range(250):
            s.add(Trade(strategy_id=sid, mode="paper", symbol=f"R{i}/USD", asset_class="crypto",
                        status="rejected", qty=0, notional=0,
                        entry_reason="wanted to buy but rail: ...",
                        entry_at=NOW - timedelta(minutes=250 - i)))
        s.commit()
    yield sid
    with session_scope() as s:
        s.query(Trade).delete()
        s.query(StrategyConfigVersion).delete()
        s.query(Strategy).delete()
        set_setting(s, "engine_mode", "off")


def _sell_the_old_position(sid: int) -> None:
    with session_scope() as s:
        t = s.query(Trade).filter(Trade.symbol == "ADA/USD").one()
        t.status = "closed"
        t.exit_price = 0.18
        t.exit_at = NOW  # sold JUST NOW, though opened a week ago
        t.exit_reason = "force-closed by hand (market order)"
        t.pnl = 5.65


def test_a_just_sold_position_appears_even_behind_a_flood_of_rejections(client, seeded):
    """The reported bug. Ordering by id buried it under 250 newer rejected rows;
    ordering by ACTIVITY puts the sale where it belongs — at the top."""
    _sell_the_old_position(seeded)
    rows = client.get("/api/engine/journal?limit=100").json()
    symbols = [r["symbol"] for r in rows]
    assert "ADA/USD" in symbols, "the sale never reached the browser"
    assert symbols[0] == "ADA/USD", "the most recent activity should sort first"


def test_ordering_is_by_activity_not_creation(client, seeded):
    """An untouched OLD open position must NOT outrank newer activity just
    because it was created first — and vice versa."""
    _sell_the_old_position(seeded)
    rows = client.get("/api/engine/journal?limit=10").json()
    # The sale is newest; the rejections behind it are in reverse-time order.
    assert rows[0]["symbol"] == "ADA/USD"
    assert rows[1]["symbol"].startswith("R")


def test_the_trades_filter_still_excludes_rejections(client, seeded):
    _sell_the_old_position(seeded)
    rows = client.get("/api/engine/journal?status=trades").json()
    assert [r["symbol"] for r in rows] == ["ADA/USD"]


def test_the_limit_is_honoured_so_the_ui_can_detect_truncation(client, seeded):
    """The page says "showing the most recent N" when it gets exactly N back, so
    the cap has to be respected exactly or that message lies."""
    assert len(client.get("/api/engine/journal?limit=25").json()) == 25
