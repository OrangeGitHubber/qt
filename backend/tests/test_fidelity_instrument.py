"""The fidelity comparison is a MEASURING INSTRUMENT, and these are the faults
that had it corrupting its own measurements.

Three separate ones, pinned separately:

  1. Serialized trade prices were rounded to four decimals. SHIB/USD trades near
     $0.00001, so `round(0.0000123, 4)` was `0.0` — and a zero simulated price
     makes fidelity._pct_delta report a clean, plausible -100%, which then lands
     in `median_entry_delta_pct`, `measured_cost_per_side_pct` and
     `suggested_spread_pct`. The last of those is the number the user is told to
     copy into the backtest's spread setting.

  2. The replay measured a crypto "24h change" from `bar_ts - 24h`. The live
     engine measures it from the open of the oldest of 24 HOURLY bars, i.e. from
     `floor_to_hour(now) - 23h` — up to an hour newer. Against a 0% gain gate,
     an hour of crypto is the whole decision, so the replay bought names the
     engine had never seen a positive number for and the report called them
     trades the backtest invented.

  3. The backtest response says which exit model produced its numbers; the
     fidelity response did not — and the fidelity report is the thing whose exit
     numbers moved when that model changed.
"""

from datetime import datetime, timedelta, timezone

import pytest

from qt import security
from qt.broker.alpaca import SECRET_KEY_ID, SECRET_KEY_SECRET
from qt.db import session_scope
from qt.models import Strategy, StrategyConfigVersion, Trade
from qt.services import fidelity
from qt.services.backtest import (
    _day_fn,
    _median_bar_move_pct,
    _prepare,
    _price,
    _rolling_ref_at,
    run_backtest,
)

DAY1 = datetime(2026, 7, 30, 10, 0, tzinfo=timezone.utc)
NOW = datetime(2026, 7, 31, 13, 51, tzinfo=timezone.utc)  # the live tick's odd phase

@pytest.fixture()
def configured(client):
    """Broker keys present, and the strategies/trades this file creates cleaned
    up after — the test database is shared."""
    with session_scope() as s:
        security.set_secret(s, SECRET_KEY_ID, "k")
        security.set_secret(s, SECRET_KEY_SECRET, "s")
    yield
    with session_scope() as s:
        s.query(Trade).delete()
        s.query(StrategyConfigVersion).delete()
        s.query(Strategy).delete()
        security.delete_secret(s, SECRET_KEY_ID)
        security.delete_secret(s, SECRET_KEY_SECRET)


RISK = {
    "max_daily_loss_usd": 1e9, "max_daily_loss_pct": 100, "max_total_positions": 50,
    "max_total_exposure_usd": 1e9, "max_trades_per_day": 200,
    "cooldown_hours_after_loss": 0, "wash_sale_guard": "off", "leverage_enabled": False,
}


def _strategy(min_gain: float = 0.0, take_profit: float = 0.0) -> dict:
    return {
        "asset_class": "crypto", "swing_mode": False,
        "sizing_usd": 100.0, "sleeve_usd": 1000.0, "max_positions": 3,
        "params": {
            "entry": {"min_day_gain_pct": min_gain, "require_above_vwap": False,
                      "entry_window_start": None, "entry_window_end": None},
            "exit": {"trailing_stop_pct": 0, "stop_loss_pct": 0,
                     "take_profit_pct": take_profit, "max_holding_hours": 0,
                     "flatten_before_close": False, "exit_below_vwap": False},
        },
    }


def _minute_bars(price_at, start=DAY1, end=None) -> list[dict]:
    """One bar per minute, each flat within its own minute so the tape's steps
    land exactly where the price function puts them."""
    end = end or (NOW + timedelta(minutes=9))
    out, ts = [], start
    while ts <= end:
        p = price_at(ts)
        out.append({"t": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "o": p, "h": p, "l": p, "c": p, "v": 1000, "vw": p})
        ts += timedelta(minutes=1)
    return out


def _hourly_from(minutes: list[dict]) -> list[dict]:
    """The same tape as the live engine sees it: hourly bars, newest last. This
    is what scanner.crypto_rolling_stats fetches."""
    buckets: dict[str, list[dict]] = {}
    for bar in minutes:
        key = bar["t"][:13]
        buckets.setdefault(key, []).append(bar)
    out = []
    for key in sorted(buckets):
        group = buckets[key]
        out.append({
            "t": f"{key}:00:00Z",
            "o": group[0]["o"], "c": group[-1]["c"],
            "h": max(b["h"] for b in group), "l": min(b["l"] for b in group),
            "v": sum(b["v"] for b in group), "vw": group[-1]["c"],
        })
    return out


# =====================================================================
# FAULT 1 — sub-cent prices survived nothing
# =====================================================================

SHIB = 0.0000123


def test_a_sub_cent_price_is_not_rounded_away_to_zero():
    """round(0.0000123, 4) == 0.0. That zero was observed in a real report as
    `sim_entry: 0` on an invented SHIB/USD entry."""
    assert _price(SHIB) == pytest.approx(SHIB, rel=1e-9)
    assert _price(SHIB) != 0


def test_an_ordinary_price_still_gets_exactly_four_decimals():
    """The rounding is not the enemy — it is what stops the UI printing fourteen
    digits of float noise. Anything from a dollar up must be untouched."""
    assert _price(818.671234567) == 818.6712
    assert _price(1.0) == 1.0


def test_a_sub_dollar_price_keeps_six_significant_figures():
    """Between a cent and a dollar, four decimals leaves only three significant
    figures — a 0.5% quantisation error on a $0.02 coin, against deltas this
    report measures in hundredths of a percent."""
    assert _price(0.0123456789) == pytest.approx(0.0123457, rel=1e-9)


def test_the_price_a_replayed_trade_reports_is_the_price_it_paid():
    """End to end through run_backtest's serialization, which is where the six
    round(..., 4) calls lived."""
    result = run_backtest(
        _strategy(min_gain=0.0), {"SHIB/USD": _minute_bars(_step_tape(SHIB))},
        dict(RISK), starting_cash=1000, spread_pct=0, market="crypto",
        sim_start=NOW.replace(minute=0),
    )
    held = result["open_positions"]
    assert held, "the tape was built to open a position; nothing to check otherwise"
    assert held[0]["entry_price"] > 0
    assert held[0]["entry_price"] == pytest.approx(SHIB * 1.02, rel=1e-6)
    assert held[0]["mark_price"] > 0


def test_a_zero_simulated_price_is_reported_as_no_comparison_not_as_minus_100pct():
    """The second lock. _pct_delta guarded a zero LIVE price and not a zero SIM
    one, which is the direction that actually occurred: (0 - live)/live * 100 is
    -100%, and -100% is a number the median will happily accept."""
    assert fidelity._pct_delta(SHIB, 0.0) is None
    assert fidelity._pct_delta(SHIB, None) is None
    assert fidelity._pct_delta(SHIB, SHIB * 1.001) == pytest.approx(0.1, abs=1e-3)


def test_a_zero_sim_price_cannot_reach_the_suggested_spread_setting():
    """Why the guard exists at all: this median is what the user is told to type
    into the backtest's spread field."""
    report = fidelity.compare(
        [{"symbol": "SHIB/USD", "entry_day": "2026-07-31", "status": "open",
          "entry_price": SHIB, "exit_price": None}],
        {"trade_list": [], "open_positions": [
            {"symbol": "SHIB/USD", "entry_day": "2026-07-31", "entry_price": 0.0}
        ]},
    )
    assert report["execution"]["suggested_spread_pct"] is None
    assert report["matched"][0]["entry_delta_pct"] is None


def test_the_rounding_never_reached_the_backtests_own_arithmetic():
    """Establishes the blast radius rather than asserting it in prose: the SAME
    price path at $10 and at $0.0000123 must return the SAME percentage, which it
    can only do if every fill, fee and P&L is computed from the unrounded value
    and rounding happens on the way out."""
    runs = [
        run_backtest(
            _strategy(min_gain=0.0, take_profit=1.0),
            {"X/USD": _minute_bars(_step_tape(base))},
            dict(RISK), starting_cash=1000, spread_pct=0, market="crypto",
            sim_start=NOW.replace(minute=0),
        )
        for base in (10.0, SHIB)
    ]
    assert runs[0]["trades"] == 1 and runs[1]["trades"] == 1
    assert runs[0]["net_pnl_pct"] == pytest.approx(runs[1]["net_pnl_pct"], abs=0.01)


def _step_tape(base: float):
    """Flat, then +2% at 13:00 on day 2, then +4% at 13:30. Enough to trip a 0%
    entry gate and then a 1% take-profit."""
    jump1 = NOW.replace(minute=0)
    jump2 = NOW.replace(minute=30)

    def price_at(ts: datetime) -> float:
        if ts >= jump2:
            return base * 1.04
        if ts >= jump1:
            return base * 1.02
        return base

    return price_at


# =====================================================================
# FAULT 3 — live and the replay read "24h ago" off different instants
# =====================================================================

# The step sits at 13:55, INSIDE the hour, so neither side's answer depends on
# which of two adjacent bars it happens to pick at a boundary.
STEP = DAY1.replace(hour=13, minute=55)
DROP = NOW.replace(minute=0)


def _reference_tape(ts: datetime) -> float:
    if ts >= DROP:
        return 100.5
    if ts >= STEP:
        return 101.0
    return 100.0


def test_the_replay_reads_its_24h_baseline_off_the_instant_live_reads_it_off():
    """The fix. scanner.rolling_24h keeps the newest 24 hourly bars and takes the
    OPEN of the oldest, so its reference is the price at floor_to_hour(now) - 23h.
    The replay must land on the same instant or the two are grading different
    tapes."""
    from qt.services.scanner import rolling_24h

    minutes = _minute_bars(_reference_tape)
    live = rolling_24h([b for b in _hourly_from(minutes)
                        if b["t"] <= NOW.strftime("%Y-%m-%dT%H:00:00Z")])
    assert live is not None
    replayed = next(
        b for b in _prepare(minutes, _day_fn("crypto"), rolling_24h=True)
        if b["ts"] == NOW
    )
    assert replayed["change_pct"] == pytest.approx(live[1], abs=0.01)


def test_the_reference_instant_is_the_hour_boundary_live_quantises_to():
    """Named directly, because the whole fault is one hour of drift and an
    off-by-one-hour here would still pass a tolerance-based comparison on a
    quiet tape."""
    assert _rolling_ref_at(NOW) == DAY1.replace(hour=14, minute=0)
    # Same reference for every minute of the hour — live holds one hourly window
    # for the whole hour too.
    assert _rolling_ref_at(NOW.replace(minute=1)) == _rolling_ref_at(NOW)


def test_the_baseline_bar_is_the_one_that_ends_on_the_reference_instant():
    """One bar out, at minute resolution, is the same class of error as one hour
    out — just smaller. Live takes the OPEN of the hourly bar starting at the
    reference instant, i.e. the price AT that instant. The replay must therefore
    take the close of the bar that ENDS there, not of the one that starts there
    and runs on for another minute."""
    from qt.services.scanner import rolling_24h

    minutes = _minute_bars(lambda ts: 100.0)
    stamp = _rolling_ref_at(NOW).strftime("%Y-%m-%dT%H:%M:%SZ")
    for bar in minutes:
        if bar["t"] == stamp:
            # The price runs away INSIDE the reference minute. Its open is still
            # the price at the instant live measures from.
            bar.update(o=100.0, h=140.0, l=100.0, c=140.0, vw=100.0)

    replayed = next(
        b for b in _prepare(minutes, _day_fn("crypto"), rolling_24h=True) if b["ts"] == NOW
    )
    live = rolling_24h([b for b in _hourly_from(minutes)
                        if b["t"] <= NOW.strftime("%Y-%m-%dT%H:00:00Z")])
    assert replayed["change_pct"] == pytest.approx(0.0, abs=1e-9)
    assert replayed["change_pct"] == pytest.approx(live[1], abs=1e-9)


def test_the_naive_24h_baseline_really_did_flip_a_zero_gain_gate():
    """Guards the premise. If `ts - 24h` and live's reference agreed, everything
    above would be passing for the wrong reason."""
    minutes = _minute_bars(_reference_tape)
    parsed = {b["t"]: float(b["c"]) for b in minutes}
    naive_ref = parsed[(NOW - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")]
    naive = (_reference_tape(NOW) / naive_ref - 1) * 100
    live_like = (_reference_tape(NOW) / 101.0 - 1) * 100
    assert naive > 0 > live_like, f"{naive=} {live_like=}"


# Any minimum above zero and below the naive baseline's fake gain works here; the
# gate just has to be able to tell a negative change from a positive one. It was
# 0.0 until 2026-08-05, when min_day_gain_pct: 0 became OFF (matching every other
# optional entry rule) and a zero gate stopped rejecting anything. The claim
# below is unchanged — only the number expressing it.
TINY_GAIN_GATE = 0.01


def test_the_replay_no_longer_buys_what_the_engine_never_saw_a_gain_on():
    """The observed symptom, end to end: a gain gate a hair above zero, a coin
    whose real 24h change was negative, and a replay that bought it anyway and
    had the trade filed as one the backtest INVENTED."""
    result = run_backtest(
        _strategy(min_gain=TINY_GAIN_GATE), {"BAT/USD": _minute_bars(_reference_tape)},
        dict(RISK), starting_cash=1000, spread_pct=0, market="crypto",
        sim_start=DROP,
    )
    # Not vacuous: the replay really did look, and really did reject on the gain.
    assert result["diagnosis"]["bars_evaluated"] > 0
    assert result["diagnosis"]["rejected_day_gain"] > 0
    assert result["trades"] == 0
    assert result["open_positions"] == []


def test_a_daily_crypto_replay_is_left_exactly_where_it_was():
    """Daily bars sit 24h apart, so both references pick the previous daily
    close. Every daily crypto backtest must be byte-identical — this change is
    about intraday resolution only."""
    days = [
        {"t": (DAY1.replace(hour=0, minute=0) + timedelta(days=n)).strftime("%Y-%m-%dT%H:%M:%SZ"),
         "o": 100 + n, "h": 100 + n, "l": 100 + n, "c": 100 + n, "v": 1000, "vw": 100 + n}
        for n in range(5)
    ]
    rows = _prepare(days, _day_fn("crypto"), rolling_24h=True)
    assert rows[0]["change_pct"] is None  # nothing before it to measure from
    assert rows[3]["change_pct"] == pytest.approx((103 / 102 - 1) * 100)


# =====================================================================
# FAULTS 2 & 4 — the report must say which model produced its numbers,
# and where the floor under them is
# =====================================================================

def test_the_backtest_measures_the_size_of_one_bars_move():
    """The scale of the poll-phase error, measured rather than asserted."""
    def zigzag(ts: datetime) -> float:
        return 100.0 * (1.001 if ts.minute % 2 else 1.0)

    prepared = {"X": _prepare(_minute_bars(zigzag), _day_fn("crypto"))}
    assert _median_bar_move_pct(prepared) == pytest.approx(0.1, abs=0.005)


def test_a_flat_tape_has_no_move_to_report():
    prepared = {"X": _prepare(_minute_bars(lambda ts: 100.0), _day_fn("crypto"))}
    assert _median_bar_move_pct(prepared) == 0.0
    assert _median_bar_move_pct({}) is None


def test_a_minute_replay_reports_its_measured_floor():
    result = run_backtest(
        _strategy(min_gain=99.0), {"X/USD": _minute_bars(_reference_tape)},
        dict(RISK), starting_cash=1000, spread_pct=0, market="crypto",
    )
    assert result["exit_model"] == "poller"
    assert result["median_bar_move_pct"] is not None


def test_the_exit_model_block_names_the_model_the_bar_size_and_the_floor():
    from qt.api.fidelity import _exit_model

    block = _exit_model([{"exit_model": "poller", "bar_seconds": 60.0,
                          "median_bar_move_pct": 0.08}])
    assert block["model"] == "poller"
    assert block["bar_seconds"] == 60.0
    assert block["poll_phase_floor_pct"] == 0.08
    assert "0.08%" in block["note"]
    assert "tick data" in block["note"]


def test_a_split_comparison_that_used_two_bar_sizes_says_mixed():
    """A segmented comparison picks a bar size per stretch, so one half can be
    poller-modelled and the other not. Claiming either one would be false for
    half the report; the coarsest bar size is reported for the same reason
    _apply_poller_view treats an unknown resolution as coarse."""
    from qt.api.fidelity import _exit_model

    block = _exit_model([
        {"exit_model": "poller", "bar_seconds": 60.0},
        {"exit_model": "intrabar", "bar_seconds": 900.0},
    ])
    assert block["model"] == "mixed"
    assert block["bar_seconds"] == 900.0


def test_the_compare_endpoint_actually_carries_the_exit_model_through(client, configured):
    """The fault, exactly: /api/backtest had reported `exit_model` and
    `bar_seconds` since the poller model landed and /api/fidelity/compare had
    not — so the numbers that moved were in the one response that could not say
    what moved them. A unit test of the helper would not have caught this."""
    from unittest.mock import AsyncMock, patch

    from qt.broker.alpaca import AlpacaClient
    from qt.db import session_scope
    from qt.models import Trade

    sid = client.post("/api/strategies", json=_endpoint_strategy()).json()["id"]
    with session_scope() as s:
        s.add(Trade(
            strategy_id=sid, mode="paper", symbol="NVDA", asset_class="stock",
            status="closed", qty=10, notional=1000, entry_price=100.0,
            exit_price=110.0, pnl=100.0, entry_reason="gain 5%",
            exit_reason="take-profit: +10%",
            entry_at=datetime.now(timezone.utc) - timedelta(days=3),
            exit_at=datetime.now(timezone.utc) - timedelta(days=2),
        ))
    bars = [
        {"t": (datetime.now(timezone.utc) - timedelta(days=n)).strftime("%Y-%m-%dT%H:%M:%SZ"),
         "o": 100, "h": 100, "l": 100, "c": 100, "v": 1e6, "vw": 100}
        for n in (10, 9, 8)
    ]
    with patch.object(AlpacaClient, "historical_bars", new=AsyncMock(return_value={"NVDA": bars})):
        body = client.post(
            "/api/fidelity/compare", json={"strategy_id": sid, "days": 30, "mode": "paper"}
        ).json()

    block = body["exit_model"]
    # Daily bars are far coarser than the engine's 60-second look, so this
    # comparison is honestly an intra-bar one and has to say so.
    assert block["model"] == "intrabar"
    assert block["bar_seconds"] == 86400.0
    assert block["poll_seconds"] == 60
    assert block["note"]


def _endpoint_strategy() -> dict:
    return {
        "name": "fid exit model", "asset_class": "stock", "universe": "custom",
        "symbols": ["NVDA"], "preset": "custom",
        "params": {
            "entry": {"min_day_gain_pct": 3, "require_above_vwap": False},
            "exit": {"trailing_stop_pct": 5, "stop_loss_pct": 4, "take_profit_pct": 0},
        },
        "sizing_usd": 1000, "sleeve_usd": 5000, "max_positions": 3,
        "swing_mode": True, "ignore_regime": True,
    }


def test_the_note_stops_the_chase_only_when_the_poller_model_ran():
    """On coarse bars the residual is NOT a poll-phase floor, and telling someone
    to stop chasing it there would be telling them to ignore a real difference."""
    from qt.api.fidelity import _exit_model

    coarse = _exit_model([{"exit_model": "intrabar", "bar_seconds": 86400.0}])
    assert "tick data" not in coarse["note"]
    assert "high and low" in coarse["note"]


# ---- the log must not paste a server-side clock into its own sentence ----

def test_a_matched_row_carries_instants_not_a_formatted_clock():
    """The row's "When" column is converted to the reader's display zone by the
    frontend; the detail sentence used to carry a clock sliced straight out of
    the UTC string. One real row read "2026-08-03 10:01" in its column and "the
    replay was 14:01 vs 14:26" in its text — the same instant, four hours apart.

    The server cannot format a clock: it does not know which zone the page is
    read in. So it ships the instants and says nothing about the time."""
    from qt.services.fidelity import compare

    live = [{
        "symbol": "AMZN", "entry_day": "2026-08-03", "status": "open",
        "entry_price": 200.0, "entry_at": "2026-08-03T14:01:00Z",
        "exit_day": None, "exit_at": None, "exit_reason": "",
    }]
    result = {
        "trade_list": [],
        "open_positions": [{
            "symbol": "AMZN", "entry_day": "2026-08-03", "entry_price": 200.5,
            "entry_at": "2026-08-03T14:26:00Z", "exit_day": None, "exit_reason": None,
        }],
    }
    row = next(r for r in compare(live, result, replayed_symbols=["AMZN"])["log"]
               if r["action"] == "bought")

    assert "14:01" not in row["detail"] and "14:26" not in row["detail"], (
        "a UTC clock in the sentence contradicts the row's own converted column"
    )
    assert row["live_at"] == "2026-08-03T14:01:00Z"
    assert row["sim_at"] == "2026-08-03T14:26:00Z"


def test_identical_instants_carry_no_clock_pair_at_all():
    """Nothing to compare when both sides acted at the same moment — the row
    would otherwise render "the replay was 14:01 vs 14:01"."""
    from qt.services.fidelity import compare

    at = "2026-08-03T14:01:00Z"
    live = [{"symbol": "AMZN", "entry_day": "2026-08-03", "status": "open",
             "entry_price": 200.0, "entry_at": at, "exit_day": None,
             "exit_at": None, "exit_reason": ""}]
    result = {"trade_list": [], "open_positions": [
        {"symbol": "AMZN", "entry_day": "2026-08-03", "entry_price": 200.0,
         "entry_at": at, "exit_day": None, "exit_reason": None}]}
    row = next(r for r in compare(live, result, replayed_symbols=["AMZN"])["log"]
               if r["action"] == "bought")
    assert "live_at" not in row and "sim_at" not in row


def test_a_daily_replay_with_no_time_of_day_offers_no_clocks():
    """A daily bar has no meaningful time. Inventing one would imply precision
    the replay does not have."""
    from qt.services.fidelity import compare

    live = [{"symbol": "AMZN", "entry_day": "2026-08-03", "status": "open",
             "entry_price": 200.0, "entry_at": "2026-08-03T14:01:00Z",
             "exit_day": None, "exit_at": None, "exit_reason": ""}]
    result = {"trade_list": [], "open_positions": [
        {"symbol": "AMZN", "entry_day": "2026-08-03", "entry_price": 200.5,
         "entry_at": "2026-08-03", "exit_day": None, "exit_reason": None}]}
    row = next(r for r in compare(live, result, replayed_symbols=["AMZN"])["log"]
               if r["action"] == "bought")
    assert "live_at" not in row and "sim_at" not in row
    assert "at the same point" in row["detail"]
