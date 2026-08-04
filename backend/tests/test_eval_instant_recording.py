"""Every journal row the engine writes must say WHEN the engine looked.

The live engine evaluates every 60 seconds counted from whenever the scheduler
last started, so its looks land at an arbitrary second past the minute (:17, :18
and :44 have all been observed). A replay evaluates at bar close, on the minute.
Nothing recorded the engine's phase, so the fidelity report had to quote an
irreducible "poll phase floor" for a difference that was not structural at all —
it was a fact nobody wrote down.

These tests pin the writing-down: on a fill, on a rail rejection, on an order
that never filled, and on an exit. The rejection cases matter most — "live passed
on this, the replay bought it" is the argument the comparison exists to settle.

They also pin the two things that make the record honest rather than decorative:
the recorded price is the price the engine SAW, not the price it got, and a
decision with no known observation instant stays NULL instead of being back-
filled with now().
"""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from qt.db import session_scope
from qt.models import Strategy, Trade
from qt.services import engine, execution, persistence

SYMBOL = "EVALREC/USD"  # unique to this module: the "already open" rail is account-wide
# A distinctive instant in the past: 14:01:17 is exactly the off-the-minute phase
# the fidelity note complains about, and nothing that quietly substitutes now()
# can produce it.
LOOKED_AT = datetime(2026, 8, 4, 14, 1, 17, tzinfo=timezone.utc)
SEEN_PRICE = 2.50   # what the snapshot showed the engine
FILL_PRICE = 2.61   # what the order actually got — deliberately different


def _utc(value: datetime | None) -> datetime | None:
    """SQLite hands datetimes back naive; the engine stores UTC."""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


@pytest.fixture()
def strategy_row():
    # Crypto on a custom universe: no market-hours gate, no regime gate and no
    # wash-sale lookup, so the run reaches the buy branch on its own merits.
    with session_scope() as s:
        strat = Strategy(
            name="eval instant test", enabled=True, asset_class="crypto", universe="custom",
            symbols=json.dumps([SYMBOL]), preset="custom",
            params='{"entry":{},"exit":{"stop_loss_pct":4}}',
            sizing_usd=200, sleeve_usd=1000, max_positions=3, swing_mode=True,
            ignore_regime=False,
        )
        s.add(strat)
        s.flush()
        sid = strat.id
    yield sid
    engine._last_run.pop(sid, None)
    with session_scope() as s:
        s.query(Trade).filter(Trade.strategy_id == sid).delete()
        s.query(Strategy).filter(Strategy.id == sid).delete()


def _run(monkeypatch, sid: int, open_trade, *, observed_at=LOOKED_AT, check_rails=None):
    """Drive one entry cycle with exactly ONE candidate, carrying a known
    observation instant, and `open_trade` deciding the outcome."""
    monkeypatch.setattr(persistence, "boot_state", lambda: {"data_persistent": None})
    # Generous portfolio-wide limits: leftovers from other modules must not block
    # the buy and make this pass for the wrong reason.
    monkeypatch.setattr(engine, "get_risk", lambda session: {
        **engine.RISK_DEFAULTS,
        "max_daily_loss_usd": 1e9, "max_daily_loss_pct": 100.0,
        "max_total_positions": 999, "max_total_exposure_usd": 1e9,
        "max_trades_per_day": 999, "cooldown_hours_after_loss": 0,
    })
    if check_rails is not None:
        monkeypatch.setattr(engine, "check_rails", check_rails)

    cand = engine.Candidate(
        symbol=SYMBOL, asset_class="crypto", price=SEEN_PRICE, change_pct=6.0, vwap=2.40,
        observed_at=observed_at,
    )

    async def fake_candidates(session, client, strategy, scan_result):
        return ([cand] if strategy.id == sid else []), scan_result

    monkeypatch.setattr(engine, "_candidates_for", fake_candidates)
    monkeypatch.setattr(execution, "open_trade", open_trade)

    with session_scope() as session:
        asyncio.run(
            engine._consider_entries(session, MagicMock(), "paper", 100_000.0, True, False)
        )


def _rows(sid: int) -> list[Trade]:
    with session_scope() as s:
        return s.query(Trade).filter(Trade.strategy_id == sid).order_by(Trade.id).all()


async def _fills(session, client, strategy, version_id, mode, cand, reason, sizing_usd=None):
    trade = Trade(
        strategy_id=strategy.id, mode=mode, symbol=cand.symbol,
        asset_class=cand.asset_class, qty=80, notional=FILL_PRICE * 80, status="open",
        entry_price=FILL_PRICE, entry_at=datetime.now(timezone.utc), entry_reason=reason,
    )
    session.add(trade)
    return trade


async def _declines(session, client, strategy, version_id, mode, cand, reason, sizing_usd=None):
    # Exactly what the real open_trade does on all five of its failure paths:
    # journal a rejected Trade carrying the obstacle, then return None.
    session.add(
        Trade(
            strategy_id=strategy.id, mode=mode, symbol=cand.symbol,
            asset_class=cand.asset_class, qty=0, notional=0, status="rejected",
            entry_reason="order did not fill in time — cancelled",
        )
    )
    return None


# --------------------------------------------------------------------------
# Entries
# --------------------------------------------------------------------------


def test_a_fill_records_the_instant_the_engine_looked(monkeypatch, strategy_row):
    _run(monkeypatch, strategy_row, _fills)

    rows = _rows(strategy_row)
    assert len(rows) == 1 and rows[0].status == "open", "test setup: expected one filled row"
    assert _utc(rows[0].entry_eval_at) == LOOKED_AT, (
        "the fill does not say which second the engine was looking at, so a replay "
        "still cannot align its clock to the engine's"
    )


def test_a_fill_records_the_price_the_engine_saw_not_the_price_it_got(monkeypatch, strategy_row):
    """Separate claim from the instant, and not the same number as entry_price:
    entry_price is the FILL (slippage, limit, partial), while the replay reads a
    bar close and needs the figure live actually decided on."""
    _run(monkeypatch, strategy_row, _fills)

    row = _rows(strategy_row)[0]
    assert row.entry_price == FILL_PRICE, "test setup: the fill price should differ"
    assert row.entry_eval_price == SEEN_PRICE, (
        "the row records what the order got but not what the engine saw, so the "
        "live-vs-replay price gap stays a mystery instead of a measured number"
    )


def test_a_rail_rejection_records_the_instant_and_the_price(monkeypatch, strategy_row):
    """The case the comparison argues about: live declined here. Without the
    instant there is no way to tell whether the replay was even looking at the
    same tape, and a rejected row records no price at all otherwise."""
    _run(
        monkeypatch, strategy_row, _fills,
        # `now` is check_rails' explicit clock — the engine passes the wall clock.
        check_rails=lambda cfg, sizing, ctx, now=None: (False, "the sleeve is full"),
    )

    rows = _rows(strategy_row)
    assert len(rows) == 1 and rows[0].status == "rejected", "test setup: expected a rail rejection"
    assert _utc(rows[0].entry_eval_at) == LOOKED_AT
    assert rows[0].entry_eval_price == SEEN_PRICE


def test_an_order_that_never_filled_records_them_too(monkeypatch, strategy_row):
    """execution.open_trade journals its own rejected row and returns None — the
    engine never sees that object, so it has to find it in the session."""
    _run(monkeypatch, strategy_row, _declines)

    rows = _rows(strategy_row)
    assert len(rows) == 1 and rows[0].status == "rejected", "test setup: expected a non-fill row"
    assert _utc(rows[0].entry_eval_at) == LOOKED_AT, (
        "an order that never filled carries no observation instant, so the busiest "
        "class of rejected row is exactly the one that cannot be compared"
    )
    assert rows[0].entry_eval_price == SEEN_PRICE


def test_an_unknown_instant_stays_null_rather_than_pretending(monkeypatch, strategy_row):
    """A candidate whose price came from somewhere with no recorded observation
    instant must leave the column NULL. "We do not know when the engine looked"
    has to stay distinguishable from "the engine looked at 14:01:17" — a
    confident wrong timestamp under the feature that measures accuracy is worse
    than a blank."""
    _run(monkeypatch, strategy_row, _fills, observed_at=None)

    row = _rows(strategy_row)[0]
    assert row.entry_eval_at is None, (
        "a decision with no known observation instant was stamped anyway — every "
        "row now looks recorded and none of them can be trusted"
    )
    # The price was still genuinely seen, so it is still recorded.
    assert row.entry_eval_price == SEEN_PRICE


def test_two_strategies_in_one_tick_keep_their_own_instants(monkeypatch, strategy_row):
    """The granularity claim. A tick evaluates every enabled strategy in
    sequence, each issuing its own snapshot fetch, so one instant per TICK would
    be seconds wrong for the later ones — re-creating the ambiguity this exists to
    remove. Two candidates looked at 3 seconds apart must record 3 seconds apart."""
    with session_scope() as s:
        second = Strategy(
            name="eval instant test B", enabled=True, asset_class="crypto", universe="custom",
            symbols=json.dumps([SYMBOL + "2"]), preset="custom",
            params='{"entry":{},"exit":{}}',
            sizing_usd=200, sleeve_usd=1000, max_positions=3, swing_mode=True,
        )
        s.add(second)
        s.flush()
        sid_b = second.id

    later = LOOKED_AT + timedelta(seconds=3)
    cand_a = engine.Candidate(symbol=SYMBOL, asset_class="crypto", price=SEEN_PRICE,
                              change_pct=6.0, vwap=2.40, observed_at=LOOKED_AT)
    cand_b = engine.Candidate(symbol=SYMBOL + "2", asset_class="crypto", price=SEEN_PRICE,
                              change_pct=6.0, vwap=2.40, observed_at=later)

    async def fake_candidates(session, client, strategy, scan_result):
        if strategy.id == strategy_row:
            return [cand_a], scan_result
        if strategy.id == sid_b:
            return [cand_b], scan_result
        return [], scan_result

    monkeypatch.setattr(persistence, "boot_state", lambda: {"data_persistent": None})
    monkeypatch.setattr(engine, "get_risk", lambda session: {
        **engine.RISK_DEFAULTS,
        "max_daily_loss_usd": 1e9, "max_daily_loss_pct": 100.0,
        "max_total_positions": 999, "max_total_exposure_usd": 1e9,
        "max_trades_per_day": 999, "cooldown_hours_after_loss": 0,
    })
    monkeypatch.setattr(engine, "_candidates_for", fake_candidates)
    monkeypatch.setattr(execution, "open_trade", _fills)

    try:
        with session_scope() as session:
            asyncio.run(
                engine._consider_entries(session, MagicMock(), "paper", 100_000.0, True, False)
            )
        a = _rows(strategy_row)[0]
        b = _rows(sid_b)[0]
        assert _utc(a.entry_eval_at) == LOOKED_AT
        assert _utc(b.entry_eval_at) == later, (
            "both strategies recorded the same instant, so the record is per-tick — "
            "which is precisely the resolution that was missing"
        )
    finally:
        engine._last_run.pop(sid_b, None)
        with session_scope() as s:
            s.query(Trade).filter(Trade.strategy_id == sid_b).delete()
            s.query(Strategy).filter(Strategy.id == sid_b).delete()


def test_a_recording_failure_cannot_abort_a_trade(monkeypatch, strategy_row):
    """The engine runs every 60 seconds against a real broker. The order is
    already placed by the time this is written and the row is already correct
    without it, so a fault in the bookkeeping must never propagate."""

    class Exploding:
        symbol = SYMBOL
        price = SEEN_PRICE

        @property
        def observed_at(self):
            raise RuntimeError("clock exploded")

    with session_scope() as session:
        trade = Trade(
            strategy_id=strategy_row, mode="paper", symbol=SYMBOL, asset_class="crypto",
            qty=1, notional=SEEN_PRICE, status="open", entry_price=SEEN_PRICE,
        )
        # Must not raise.
        engine._stamp_entry_eval(session, strategy_row, Exploding(), trade)
        assert trade.status == "open", "the trade itself was disturbed by the recording step"


# --------------------------------------------------------------------------
# Exits — the residual the fidelity report calls a "poll phase floor" is an EXIT
# residual: the replay checks the stop at bar close, the engine checked it at :17.
# --------------------------------------------------------------------------


EXIT_MODE = "evalrec"  # a mode of its own so other modules' open trades stay out


# --------------------------------------------------------------------------
# End to end, through a real tick — no stub anywhere in the recording chain.
# The DCA sleeve is its own entry path (it bypasses one rail and calls
# open_trade directly), so it can lose the instant independently of everything
# above; and a full tick is the only thing that proves the snapshot fetch, the
# candidate builder, the entry loop and the journal are actually joined up.
# --------------------------------------------------------------------------


DCA_SNAPSHOT = {
    "SPY": {"latestTrade": {"p": 50.0}, "dailyBar": {"c": 50.0, "vw": 49.5},
            "prevDailyBar": {"c": 49.0}},
}
_DCA_BROKER = dict(
    account=AsyncMock(return_value={"equity": "5000", "cash": "5000"}),
    clock=AsyncMock(return_value={"is_open": True, "next_close": "2099-01-01T21:00:00Z"}),
    stock_movers=AsyncMock(return_value={"gainers": [], "losers": []}),
    crypto_assets=AsyncMock(return_value=[]),
    crypto_snapshots=AsyncMock(return_value={}),
    stock_bars=AsyncMock(return_value={"SPY": [{"c": 500.0 - i * 0.1} for i in range(210)]}),
    historical_bars=AsyncMock(return_value={}),
    stock_snapshots=AsyncMock(return_value=DCA_SNAPSHOT),
)


async def test_a_real_tick_stamps_the_row_it_writes(client):
    from unittest.mock import patch

    from qt import security
    from qt.broker.alpaca import SECRET_KEY_ID, SECRET_KEY_SECRET, AlpacaClient
    from qt.services import regime, scanner
    from qt.settings_service import set_setting

    scanner.invalidate_cache()
    regime.invalidate_cache()
    with session_scope() as s:
        security.set_secret(s, SECRET_KEY_ID, "k")
        security.set_secret(s, SECRET_KEY_SECRET, "s")
        set_setting(s, "engine_mode", "shadow")
        set_setting(s, "risk_config", {})
    sid = client.post("/api/strategies", json={
        "name": "eval instant DCA", "asset_class": "stock", "universe": "custom",
        "symbols": ["SPY"], "preset": "dca_sleeve",
        "params": {
            "entry": {"min_day_gain_pct": 0, "require_above_vwap": False,
                      "entry_window_start": None, "entry_window_end": None},
            "exit": {"trailing_stop_pct": 0, "stop_loss_pct": 0, "take_profit_pct": 0,
                     "max_holding_hours": 0, "flatten_before_close": False,
                     "exit_below_vwap": False},
            "dca": {"interval_days": 7},
        },
        "sizing_usd": 200, "sleeve_usd": 1000, "max_positions": 5,
        "swing_mode": True, "ignore_regime": False,
    }).json()["id"]
    client.post(f"/api/strategies/{sid}/toggle")

    try:
        before = datetime.now(timezone.utc)
        with patch.multiple(AlpacaClient, **_DCA_BROKER):
            await engine.tick(leverage_unlocked=False)
        after = datetime.now(timezone.utc)

        with session_scope() as s:
            rows = s.query(Trade).filter(Trade.strategy_id == sid).all()
        assert len(rows) == 1, "test setup: expected exactly one DCA lot"
        stamped = _utc(rows[0].entry_eval_at)
        assert stamped is not None, (
            "a row written by a real tick carries no observation instant — the "
            "feature is wired up only in the tests"
        )
        # Bracketed by the tick itself: a hard-coded constant fails here too.
        assert before <= stamped <= after
        assert rows[0].entry_eval_price == 50.0, (
            "the price the engine saw in the snapshot never reached the journal"
        )
    finally:
        with session_scope() as s:
            set_setting(s, "engine_mode", "off")
            s.query(Trade).filter(Trade.strategy_id == sid).delete()
            security.delete_secret(s, SECRET_KEY_ID)
            security.delete_secret(s, SECRET_KEY_SECRET)
        client.delete(f"/api/strategies/{sid}")
        engine._last_run.pop(sid, None)
        scanner.invalidate_cache()
        regime.invalidate_cache()


@pytest.fixture()
def open_position(strategy_row):
    with session_scope() as s:
        trade = Trade(
            strategy_id=strategy_row, mode=EXIT_MODE, symbol=SYMBOL, asset_class="crypto",
            qty=10, notional=1000.0, status="open", entry_price=100.0,
            entry_at=datetime.now(timezone.utc) - timedelta(days=2), high_water=100.0,
        )
        s.add(trade)
        s.flush()
        tid = trade.id
    return tid


def _run_exits(monkeypatch, observed_price: float):
    """One exit cycle: the snapshot shows `observed_price`, which is far enough
    below the entry to trip the 4% stop. close_trade is stubbed — the broker
    round trip is not what is under test."""
    snap = {SYMBOL: {"latestTrade": {"p": observed_price}, "dailyBar": {"c": observed_price, "vw": observed_price}}}

    async def fake_quotes(client, trades):
        return snap, {SYMBOL: LOOKED_AT}

    closed = AsyncMock(return_value=True)
    monkeypatch.setattr(engine, "_quotes_for", fake_quotes)
    monkeypatch.setattr(execution, "close_trade", closed)
    with session_scope() as session:
        asyncio.run(engine._manage_exits(session, MagicMock(), EXIT_MODE, True, False))
    return closed


def test_an_exit_records_the_instant_the_engine_looked(monkeypatch, strategy_row, open_position):
    closed = _run_exits(monkeypatch, 90.0)
    assert closed.await_count == 1, "test setup: the stop should have fired"

    with session_scope() as s:
        trade = s.get(Trade, open_position)
        assert _utc(trade.exit_eval_at) == LOOKED_AT, (
            "the exit does not say which second it was decided on — and the exit is "
            "where the poll-phase residual actually lives"
        )


def test_an_exit_records_the_price_it_was_decided_on(monkeypatch, strategy_row, open_position):
    """Separate claim: exit_price is the fill, this is what the engine saw. The
    replay reads an IEX bar close and live reads a consolidated snapshot trade;
    comparing them directly is the whole point."""
    _run_exits(monkeypatch, 90.0)

    with session_scope() as s:
        trade = s.get(Trade, open_position)
        assert trade.exit_eval_price == 90.0, (
            "the observed exit price was not recorded, so a live-vs-replay exit "
            "difference cannot be attributed to the tape rather than the rules"
        )


def test_a_position_that_is_not_exiting_is_not_stamped(monkeypatch, strategy_row, open_position):
    """Only the look that DECIDED an exit is recorded. Stamping every look would
    dirty every open row every 60 seconds for no new information."""
    closed = _run_exits(monkeypatch, 99.5)  # a 0.5% dip: the 4% stop must not fire
    assert closed.await_count == 0, "test setup: no exit should have been taken"

    with session_scope() as s:
        trade = s.get(Trade, open_position)
        assert trade.exit_eval_at is None
        assert trade.exit_eval_price is None
