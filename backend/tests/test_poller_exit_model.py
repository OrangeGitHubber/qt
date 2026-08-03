"""The replay may only exit on a price the LIVE engine could have seen.

THE FAULT. QT's live engine places no resting stop or limit orders. It polls —
`IntervalTrigger(seconds=60)` in qt/main.py runs a tick; the tick reads one
snapshot per open symbol (engine.py `_manage_exits`, `_price_from_snapshot`) and
sells AT MARKET when a rule fires (`execution.close_trade`). Its whole view of
the tape is one price per minute. The backtester, meanwhile, judged exits against
each bar's intra-bar HIGH/LOW and filled at the trigger level — the correct model
for a resting order, and for any bar long enough that a 60-second poller would
have sampled the breach, but a fantasy at 1-minute resolution where the poller
gets exactly ONE look per bar.

Measured on the owner's instance, a 2-hour minute-bar comparison of a real
strategy after every other known fault was fixed: 88.9% match, and the replay's
exits 0.73% BETTER than reality. Three of the four residual rows were the replay
taking a take-profit, just above the strategy's 1.2% threshold, that live never
got.

THE ERROR IS NOT ONE-DIRECTIONAL, and that is pinned below. A take-profit wick
flatters the replay; a stop-loss wick punishes it (booking a loss live never
took). Which rule fires first decides the sign, and the stop is checked first, so
the aggregate sign depends entirely on the strategy.

THE MODEL. `backtest._apply_poller_view` flattens high and low onto the close
when the bars are no coarser than the poll itself, so exits are judged and filled
on the close — exactly what live does. On coarser bars nothing changes: a
60-second poller took fifteen looks inside a 15-minute bar and hundreds inside a
daily one, so a genuine breach really was on offer, and a backtest that ignored
a stop breached for a quarter of an hour would be a worse lie than the one being
fixed. That guarantee is pinned here too.

WHAT IS PINNED, claim by claim:
  * a take-profit touched only intra-bar is NOT taken at minute resolution, and
    the eventual exit fills at the CLOSE, not the trigger level;
  * a stop-loss touched only intra-bar is likewise not taken — the other
    direction of the same error;
  * a stop genuinely breached still fires, on the first bar that CLOSES through
    it, without a bar of extra delay;
  * the trailing stop's high-water mark advances close-to-close (live raises it
    from the polled price), and still fires on a real close-to-close drop;
  * at 15-minute bars the intra-bar model survives intact, fill level and all;
  * the bar size is measured off the bars, robustly (median, not minimum), and an
    unmeasurable one is treated as coarse — "I don't know" must never be the
    answer that makes a stop lenient;
  * the portfolio replay is under the same model, and both report which one they
    used.
"""

from datetime import datetime, timedelta, timezone

import pytest

from qt.services.backtest import (
    LIVE_POLL_SECONDS,
    _apply_poller_view,
    _bar_seconds,
    run_backtest,
    run_portfolio_backtest,
)
from qt.services.engine import RISK_DEFAULTS

UTC = timezone.utc

# The first ACTION bar. Everything before it is a flat lead-in whose only job is
# to give the rolling-24h day-gain something to measure against.
ACTION_AT = datetime(2026, 8, 2, 13, 0, tzinfo=UTC)
LEAD_IN_MINUTES = 25 * 60  # > 24h, so the day-gain baseline exists on bar 0
FLAT = 100.0


def _params(**exit_rules) -> dict:
    rules = {"stop_loss_pct": 0, "trailing_stop_pct": 0, "take_profit_pct": 0}
    rules.update(exit_rules)
    return {"entry": {"min_day_gain_pct": 3.0, "require_above_vwap": False}, "exit": rules}


def _bar(ts: datetime, close: float, high: float, low: float) -> dict:
    return {
        "t": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "o": close, "h": high, "l": low, "c": close, "v": 1e5, "vw": close,
    }


def _series(action: list[tuple[float, float, float]], step_minutes: int = 1) -> list[dict]:
    """A flat lead-in at 100 followed by the given (close, high, low) action bars,
    spaced `step_minutes` apart. The lead-in is flat so day-gain is 0% through it
    and the strategy cannot enter before the window opens."""
    step = timedelta(minutes=step_minutes)
    lead = LEAD_IN_MINUTES // step_minutes
    bars = [
        _bar(ACTION_AT - step * (lead - i), FLAT, FLAT, FLAT) for i in range(lead)
    ]
    bars += [
        _bar(ACTION_AT + step * i, c, h, lo) for i, (c, h, lo) in enumerate(action)
    ]
    return bars


def _replay(action, exit_rules: dict, step_minutes: int = 1) -> dict:
    """One crypto strategy over one symbol. `max_trades_per_day: 1` so the run is
    exactly one round trip and every assertion below is about THAT trade, not
    about whatever the strategy did next."""
    strategy = {
        "asset_class": "crypto", "swing_mode": True, "sizing_usd": 400.0,
        "sleeve_usd": 1000.0, "max_positions": 5, "params": _params(**exit_rules),
    }
    return run_backtest(
        strategy,
        {"FIL/USD": _series(action, step_minutes)},
        {**RISK_DEFAULTS, "max_trades_per_day": 1},
        starting_cash=1000, spread_pct=0, market="crypto",
        sim_start=ACTION_AT,
    )


# Entry is on the first action bar at 104 — a +4% day-gain over the flat 100
# lead-in, clearing the 3% minimum.
ENTER = (104.0, 104.0, 104.0)
TP_PCT = 1.2                       # the owner's real threshold
TP_LEVEL = 104.0 * (1 + TP_PCT / 100)   # 105.248
STOP_PCT = 4.0
STOP_LEVEL = 104.0 * (1 - STOP_PCT / 100)  # 99.84


# ---------------------------------------------------------------------------
# 1. THE MEASURED FAULT — a take-profit the poller never had
# ---------------------------------------------------------------------------


def test_a_take_profit_touched_only_inside_a_minute_bar_is_not_taken():
    """XTZ, AAVE and FIL in the owner's report: the replay booked a take-profit
    just above the 1.2% threshold that live never got. Here the price pokes to
    106 — through the 105.248 target — inside one minute and closes back at 104.
    A 60-second poller that sampled at the close saw 104 and held."""
    res = _replay(
        [ENTER,
         (104.0, 106.0, 104.0),   # the wick: high clears the target, close does not
         (104.0, 104.0, 104.0)],
        {"take_profit_pct": TP_PCT},
    )
    assert res["trades"] == 0, res["trade_list"]
    assert len(res["open_positions"]) == 1


def test_the_take_profit_that_does_arrive_fills_at_the_close_not_the_target():
    """Two claims in one run, because they are the same event: the exit waits for
    a bar that CLOSES through the target (not the wick two bars earlier), and it
    fills where the poller's market sell would have — at that close, 106 — rather
    than at the 105.248 trigger level a resting limit order would have got."""
    res = _replay(
        [ENTER,
         (104.0, 106.0, 104.0),   # wick through the target: ignored
         (104.0, 104.0, 104.0),
         (106.0, 106.0, 106.0)],  # closes through it: this is the exit
        {"take_profit_pct": TP_PCT},
    )
    assert res["trades"] == 1
    trade = res["trade_list"][0]
    assert "take-profit" in trade["exit_reason"]
    # The exit is on the FOURTH action bar, not the second.
    assert trade["exit_at"] == (ACTION_AT + timedelta(minutes=3)).isoformat()
    assert trade["exit_price"] == pytest.approx(106.0, abs=0.001)
    assert trade["exit_price"] > TP_LEVEL, "filling at the target is the resting-order model"


# ---------------------------------------------------------------------------
# 2. THE OTHER DIRECTION — the bias is not simply "optimistic"
# ---------------------------------------------------------------------------


def test_a_stop_touched_only_inside_a_minute_bar_is_not_taken_either():
    """The same model, the opposite sign. Judging on the low booked a -4% loss on
    a wick the poller never sampled, so the replay reported a stop-out live did
    not take. Whether the old model flattered or punished a given strategy came
    down to which rule its wicks hit first."""
    res = _replay(
        [ENTER,
         (104.0, 104.0, 98.0),    # dips through the 99.84 stop, closes back at 104
         (104.0, 104.0, 104.0)],
        {"stop_loss_pct": STOP_PCT},
    )
    assert res["trades"] == 0, res["trade_list"]
    assert len(res["open_positions"]) == 1


# ---------------------------------------------------------------------------
# 3. THE CONSTRAINT — this must not neuter the stop
# ---------------------------------------------------------------------------


def test_a_stop_breached_for_fifteen_minutes_still_fires_on_the_first_bar():
    """The line that bounds the whole change. A backtest that ignores a stop
    clearly breached for a quarter of an hour is a worse lie than the one being
    fixed. At minute resolution close-only costs nothing here: fifteen minutes
    below the stop is fifteen consecutive bars CLOSING below it, and the exit
    lands on the first of them — 60 seconds of latency, exactly the live
    engine's."""
    res = _replay(
        [ENTER] + [(98.0, 98.0, 98.0)] * 15,
        {"stop_loss_pct": STOP_PCT},
    )
    assert res["trades"] == 1
    trade = res["trade_list"][0]
    assert "stop-loss" in trade["exit_reason"]
    assert trade["exit_at"] == (ACTION_AT + timedelta(minutes=1)).isoformat()
    assert trade["exit_price"] == pytest.approx(98.0, abs=0.001)


def test_fifteen_minute_bars_keep_the_intra_bar_model_and_the_trigger_fill():
    """Above the poll cadence nothing changes. A 60-second poller took fifteen
    looks inside this bar, so a stop breached within it really was on offer —
    and a resting-order-style fill at the trigger level is the fair reading of
    what those looks would have got. Same shape as the minute test above, which
    holds; only the bar size differs."""
    res = _replay(
        [ENTER,
         (104.0, 104.0, 98.0),
         (104.0, 104.0, 104.0)],
        {"stop_loss_pct": STOP_PCT},
        step_minutes=15,
    )
    assert res["trades"] == 1
    trade = res["trade_list"][0]
    assert "stop-loss" in trade["exit_reason"]
    assert trade["exit_price"] == pytest.approx(STOP_LEVEL, abs=0.01)


# ---------------------------------------------------------------------------
# 4. THE TRAILING STOP — high water is what live watched it rise to
# ---------------------------------------------------------------------------


def test_the_high_water_mark_does_not_advance_to_an_intra_bar_spike():
    """Live raises `high_water` from the polled price (engine.py `_manage_exits`).
    Taking it from the bar's high instead sets the trailing stop against a peak
    the engine never recorded, and then fires it — an exit at 117.60 out of a
    position the engine still saw sitting at 105.

    The spike bar's LOW is above the entry, deliberately: the trailing rule is
    guarded by `low > entry_price * (1 - stop/100)`, so a bar whose low sits ON
    the entry price cannot fire it under any model, and the test would pass while
    proving nothing."""
    res = _replay(
        [ENTER,
         (105.0, 120.0, 105.0),   # a one-minute spike to 120, closes back at 105
         (105.0, 105.0, 105.0)],
        {"trailing_stop_pct": 2.0},
    )
    assert res["trades"] == 0, res["trade_list"]
    assert len(res["open_positions"]) == 1


def test_at_fifteen_minutes_the_high_water_mark_does_follow_the_intra_bar_high():
    """The other half of the boundary, and it was pinned by NOTHING before this:
    mutating `high_water = max(high_water, bar["high"])` to use the close instead
    left all 917 tests green, even though advancing it from the high was one of
    the four bullets the intra-bar commit claimed. At 15 minutes a 60-second
    poller took fifteen looks, so a peak inside the bar really was recorded and
    the trail really does hang off it — same prices as the minute test above,
    which correctly does NOT exit."""
    res = _replay(
        [ENTER,
         (105.0, 120.0, 105.0),
         (105.0, 105.0, 105.0)],
        {"trailing_stop_pct": 2.0},
        step_minutes=15,
    )
    assert res["trades"] == 1
    trade = res["trade_list"][0]
    assert "trailing stop" in trade["exit_reason"]
    # 2% below the intra-bar high of 120 — a level the close-to-close mark
    # (105) could never have produced.
    assert trade["exit_price"] == pytest.approx(120.0 * 0.98, abs=0.01)


def test_the_trailing_stop_still_fires_on_a_close_to_close_drop():
    """The guard on the test above: the trailing stop is not simply dead under
    the poller view. A peak the poller DID see, followed by a close 2% under it,
    exits — at the close, which is where the market sell landed."""
    res = _replay(
        [ENTER,
         (110.0, 110.0, 110.0),   # a peak live's poll would have recorded
         (107.0, 107.0, 107.0)],  # 2.7% off the high, past the 2% trail
        {"trailing_stop_pct": 2.0},
    )
    assert res["trades"] == 1
    trade = res["trade_list"][0]
    assert "trailing stop" in trade["exit_reason"]
    assert trade["exit_price"] == pytest.approx(107.0, abs=0.001)


# ---------------------------------------------------------------------------
# 5. MEASURING THE BAR SIZE — and which way "I don't know" must fall
# ---------------------------------------------------------------------------


def _stamps(*offsets_seconds: float) -> list[datetime]:
    base = datetime(2026, 8, 2, tzinfo=UTC)
    return [base + timedelta(seconds=s) for s in offsets_seconds]


def test_the_bar_size_is_the_median_gap_so_one_odd_stamp_cannot_shrink_it():
    """The minimum gap would let a single near-duplicate timestamp declare a
    quarter-hour series to be minute bars — and that is the one error that makes
    a stop lenient. The median cannot be moved by one stamp."""
    quarter_hours = [900.0 * i for i in range(10)]
    assert _bar_seconds(_stamps(*quarter_hours)) == 900
    glitched = sorted(quarter_hours + [900.0 * 4 + 1])
    assert _bar_seconds(_stamps(*glitched)) == 900


def test_an_overnight_gap_does_not_inflate_a_minute_series():
    """The mean would be dragged up by a session break and the series would keep
    the intra-bar model it should not have. In-session gaps outnumber day
    boundaries by orders of magnitude, so the median is unmoved."""
    minutes = [60.0 * i for i in range(120)]
    overnight = minutes + [60.0 * 119 + 17 * 3600 + 60.0 * i for i in range(120)]
    assert _bar_seconds(_stamps(*overnight)) == 60


def test_an_unmeasurable_bar_size_is_treated_as_coarse():
    """Fewer than two distinct timestamps says nothing about the resolution. The
    safe reading is the strict one — keep the intra-bar stop — because the
    alternative silently disables it."""
    assert _bar_seconds(_stamps(0.0)) is None
    prepared = {"X": [{"close": 100.0, "high": 105.0, "low": 95.0}]}
    assert _apply_poller_view(prepared, None) is False
    assert prepared["X"][0]["high"] == 105.0  # untouched


def test_the_poller_view_applies_at_the_poll_cadence_and_not_above_it():
    """The crossover is the live engine's own 60-second tick, not a tuning
    constant. At exactly one poll per bar the extremes are information live never
    had; one second coarser and it had two looks."""
    def flattened(bar_seconds: float) -> bool:
        prepared = {"X": [{"close": 100.0, "high": 105.0, "low": 95.0}]}
        applied = _apply_poller_view(prepared, bar_seconds)
        assert applied == (prepared["X"][0]["high"] == 100.0)
        return applied

    assert flattened(LIVE_POLL_SECONDS) is True
    assert flattened(LIVE_POLL_SECONDS + 1) is False


# ---------------------------------------------------------------------------
# 6. BOTH REPLAYS, AND THEY SAY WHICH MODEL GRADED THE TRADES
# ---------------------------------------------------------------------------


def test_each_run_reports_the_exit_model_it_used():
    """Every historical number moved with this change. A run that cannot say
    which model produced it leaves the difference to be guessed at."""
    fine = _replay([ENTER, (104.0, 106.0, 104.0)], {"take_profit_pct": TP_PCT})
    coarse = _replay([ENTER, (104.0, 106.0, 104.0)], {"take_profit_pct": TP_PCT},
                     step_minutes=15)
    assert (fine["exit_model"], fine["bar_seconds"]) == ("poller", 60)
    assert (coarse["exit_model"], coarse["bar_seconds"]) == ("intrabar", 900)


def _portfolio(action, exit_rules: dict, step_minutes: int = 1) -> dict:
    strategy = {
        "id": 1, "name": "wick", "asset_class": "crypto", "swing_mode": True,
        "sizing_usd": 400.0, "sleeve_usd": 1000.0, "max_positions": 5,
        "params": _params(**exit_rules),
    }
    return run_portfolio_backtest(
        [strategy], {1: {"FIL/USD": _series(action, step_minutes)}},
        {**RISK_DEFAULTS, "max_trades_per_day": 1},
        starting_cash=1000, spread_pct=0, market="crypto", sim_start=ACTION_AT,
    )


def test_the_portfolio_trailing_stop_also_trails_the_intra_bar_high_when_coarse():
    """The portfolio loop keeps its OWN copy of the high-water update, and it was
    as unpinned as the single-strategy one: mutating it to the close left the
    whole suite green. Both halves of the boundary are now held on both loops."""
    res = _portfolio(
        [ENTER, (105.0, 120.0, 105.0), (105.0, 105.0, 105.0)],
        {"trailing_stop_pct": 2.0}, step_minutes=15,
    )
    assert res["exit_model"] == "intrabar"
    assert res["trades"] == 1
    trade = res["trade_list"][0]
    assert "trailing stop" in trade["exit_reason"]
    assert trade["exit_price"] == pytest.approx(120.0 * 0.98, abs=0.01)
    # The minute-resolution twin of the same prices does NOT exit — the poller
    # never recorded the 120.
    assert _portfolio(
        [ENTER, (105.0, 120.0, 105.0), (105.0, 105.0, 105.0)],
        {"trailing_stop_pct": 2.0},
    )["trades"] == 0


def test_the_portfolio_replay_is_under_the_same_model():
    """The portfolio run is a second, independent replay loop over the same
    primitives. It read the bar extremes exactly the way the single-strategy one
    did, so it needed the same fix — and it takes the bar size from the SHARED
    timeline, since every strategy in a portfolio replays one stream."""
    strategy = {
        "id": 1, "name": "wick", "asset_class": "crypto", "swing_mode": True,
        "sizing_usd": 400.0, "sleeve_usd": 1000.0, "max_positions": 5,
        "params": _params(take_profit_pct=TP_PCT),
    }
    bars = _series([ENTER, (104.0, 106.0, 104.0), (104.0, 104.0, 104.0)])
    res = run_portfolio_backtest(
        [strategy], {1: {"FIL/USD": bars}},
        {**RISK_DEFAULTS, "max_trades_per_day": 1},
        starting_cash=1000, spread_pct=0, market="crypto", sim_start=ACTION_AT,
    )
    assert res["exit_model"] == "poller"
    assert res["trades"] == 0, res["trade_list"]
    assert len(res["open_positions"]) == 1
