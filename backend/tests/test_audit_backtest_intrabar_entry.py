"""When the journal PROVES you opened a position, let the replay look inside the
bar for it — on that bar and no other.

The owner's argument, and it is the right one: "because the real life trade
actually did open a position there and the replay isn't seeing it, the replay
should go the extra step... This only works on the fidelity replay. On regular
backtesting it would be fine to skip it as there's nothing to measure against."

WHY THIS IS NOT "ENTER ON WICKS". A plain backtest has no ground truth, so
judging entries on a bar's HIGH would simply make it more permissive — it would
take every spike the live engine's 60-second sampling would have missed, and each
invented trade then consumes a position slot and cash that later trades do not
have. The replay's `evaluate_entry` therefore reads `bar["close"]` and nothing
else, and that stays true for every ordinary backtest.

A FIDELITY replay is a different question. There, the journal is evidence that
the engine really did open a position at a known instant, and the only thing
being asked is whether the RULES agree. A rule that was satisfied at some point
inside that minute — while the replay happened to sample the close — is a
sampling difference, not a disagreement, and reporting it as "the replay was
watching this symbol and passed" sends somebody after an entry-rule bug that does
not exist.

`seed_by_day` already does exactly this for a different artefact: it puts symbols
in the replay's universe BECAUSE the journal proves the engine acted on them, so
that a universe-reconstruction gap cannot be mistaken for a signal difference.
This is the same move applied to sampling phase, with the same obligation — it
must be declared on the row, because a comparison that quietly helps itself is
worth less than one that admits what it did.

TWO GUARDS make it a measurement rather than a licence:

  * ONLY THE BAR YOU TRADED ON. Not "any bar that day whose high qualified" —
    that is enter-on-wicks with extra steps, and it would manufacture the
    thirteen-hour timing gaps this report spent a day learning to detect.
  * IT FILLS AT THE HIGH. For a buy the high is the WORST price in the bar, so
    granting the entry on the strength of the high and then paying the high keeps
    the direction non-flattering — the rule `_fill_price` already follows for
    stops ("filling at the close after the price dipped through and recovered
    books a recovery you never got, which is the flattering direction").
"""

from datetime import datetime, timedelta, timezone

from qt.services.backtest import run_backtest
from qt.services.engine import RISK_DEFAULTS

UTC = timezone.utc
DAY1 = datetime(2026, 5, 4, 14, 0, tzinfo=UTC)
DAY2 = datetime(2026, 5, 5, 14, 0, tzinfo=UTC)
# The minute live filled on, and the one the replay must NOT be allowed to use.
# FIFTEEN-MINUTE bars, and that is load-bearing. `_apply_poller_view` flattens
# high and low onto the close whenever the bars are no coarser than the
# 60-second poll, because at 1-minute resolution the live engine gets exactly ONE
# look per bar and a price touched at 10:07:20 and gone by 10:07:40 was never
# available to it either. So at 1Min there is nothing inside the bar to find —
# correctly — and this feature is inert by construction. It only has anything to
# say at 15 minutes and coarser, where live took fifteen looks inside each bar
# and genuinely could have seen the move.
STEP = timedelta(minutes=15)
TRADED_AT = DAY2 + STEP * 2
OTHER_SPIKE = DAY2 + STEP

STRATEGY = {
    "asset_class": "stock", "swing_mode": True, "sizing_usd": 500.0,
    "sleeve_usd": 5000.0, "max_positions": 3,
    "params": {
        "entry": {"min_day_gain_pct": 2.0, "require_above_vwap": False,
                  "entry_window_start": None, "entry_window_end": None},
        "exit": {"trailing_stop_pct": 0, "stop_loss_pct": 50, "take_profit_pct": 0,
                 "max_holding_hours": 0, "flatten_before_close": False,
                 "exit_below_vwap": False},
    },
}


def _bar(at: datetime, close: float, high: float | None = None) -> dict:
    return {"t": at.strftime("%Y-%m-%dT%H:%M:%SZ"), "o": close,
            "h": high if high is not None else close, "l": close, "c": close,
            "v": 1e6, "vw": close}


def _series() -> list[dict]:
    """Day one flat at 100 for the baseline. Day two never CLOSES above +2%, but
    two separate minutes touch +3% intrabar — the one live traded on, and a
    decoy twenty minutes earlier."""
    bars = [_bar(DAY1 + STEP * n, 100.0) for n in range(4)]
    for n in range(8):
        at = DAY2 + STEP * n
        spike = at in (TRADED_AT, OTHER_SPIKE)
        bars.append(_bar(at, 101.0, high=103.0 if spike else 101.0))
    return bars


def _run(allowance: dict | None) -> dict:
    return run_backtest(
        STRATEGY, {"AAA": _series()}, dict(RISK_DEFAULTS, max_total_positions=50),
        starting_cash=5000, spread_pct=0,
        sim_start=DAY2, sim_end=DAY2 + timedelta(hours=2),
        intrabar_entry_at=allowance,
    )


def _entries(result: dict) -> list[dict]:
    return (result.get("trade_list") or []) + (result.get("open_positions") or [])


def test_an_ordinary_backtest_never_looks_inside_the_bar():
    """The control that bounds the whole feature. No allowance, no entry — every
    backtest and optimizer run behaves exactly as it did."""
    assert _entries(_run(None)) == []


def test_the_bar_you_really_traded_on_is_reconsidered():
    result = _run({"AAA": [TRADED_AT]})
    entries = _entries(result)
    assert len(entries) == 1, result.get("diagnosis", {}).get("summary")
    assert entries[0]["entry_at"].startswith(TRADED_AT.strftime("%Y-%m-%dT%H:%M"))


def test_it_fills_at_the_high_not_the_close():
    """Granted on the strength of the high, so it pays the high — the worst price
    in that bar for a buy. Filling at the close would hand the replay a better
    entry than the evidence supports, which is the flattering direction."""
    entry = _entries(_run({"AAA": [TRADED_AT]}))[0]
    assert entry["entry_price"] == 103.0, entry


def test_a_spike_on_any_other_bar_is_still_ignored():
    """The decoy. Allowing the whole DAY would be enter-on-wicks with extra
    steps, and would manufacture exactly the hours-apart timing gaps this report
    spent a day learning to detect."""
    entries = _entries(_run({"AAA": [TRADED_AT]}))
    assert len(entries) == 1
    assert not any(e["entry_at"].startswith(OTHER_SPIKE.strftime("%Y-%m-%dT%H:%M"))
                   for e in entries)


def test_the_entry_is_declared_rather_than_passed_off_as_ordinary():
    """A comparison that quietly helps itself is worth less than one that admits
    what it did — the same obligation `universe_seeded` carries."""
    entry = _entries(_run({"AAA": [TRADED_AT]}))[0]
    assert entry.get("entry_intrabar") is True, entry


def test_a_bar_whose_high_still_fails_the_rule_is_not_granted():
    """The allowance reconsiders the bar; it does not waive the rules. A minute
    that never touched the threshold is a real disagreement and must stay one."""
    quiet = [_bar(DAY1 + STEP * n, 100.0) for n in range(4)]
    quiet += [_bar(DAY2 + STEP * n, 101.0, high=101.2) for n in range(8)]
    result = run_backtest(
        STRATEGY, {"AAA": quiet}, dict(RISK_DEFAULTS, max_total_positions=50),
        starting_cash=5000, spread_pct=0,
        sim_start=DAY2, sim_end=DAY2 + timedelta(hours=2),
        intrabar_entry_at={"AAA": [TRADED_AT]},
    )
    assert _entries(result) == []


def test_at_one_minute_bars_there_is_nothing_inside_to_find():
    """AND THAT IS CORRECT, not a limitation. `_apply_poller_view` flattens the
    extremes onto the close at any resolution the 60-second poll can't see
    inside, because live gets ONE look per minute bar — measured: keeping the
    extremes ran the replay's exits 0.73% better than reality.

    So the replay and the engine are ALIGNED at 1Min, and a "replay missed it"
    there is not a sampling artefact. This feature switching itself off is the
    two models agreeing, and a report that offered the intrabar excuse at that
    resolution would be inventing a reason."""
    minute = [_bar(DAY1 + timedelta(minutes=n), 100.0) for n in range(4)]
    at = None
    for n in range(60):
        at = DAY2 + timedelta(minutes=n)
        minute.append(_bar(at, 101.0, high=103.0 if n == 30 else 101.0))
    result = run_backtest(
        STRATEGY, {"AAA": minute}, dict(RISK_DEFAULTS, max_total_positions=50),
        starting_cash=5000, spread_pct=0,
        sim_start=DAY2, sim_end=DAY2 + timedelta(hours=1),
        intrabar_entry_at={"AAA": [DAY2 + timedelta(minutes=30)]},
    )
    assert _entries(result) == []


# ---------------------------------------------------------------------------
# …and it has to REACH the replay. Every fault found across this work lived in
# the wiring between two correct halves; a feature only tested by calling
# run_backtest directly is a feature that has not been tested.
# ---------------------------------------------------------------------------


def test_the_fidelity_endpoint_sends_the_moments_it_really_traded():
    from qt.api.fidelity import _intrabar_entry_at

    rows = [
        {"symbol": "spy", "status": "closed", "entry_at": "2026-08-04T15:25:04+00:00"},
        {"symbol": "SPY", "status": "open", "entry_at": "2026-08-04T18:37:28+00:00"},
        # A refused order never filled, so it is not evidence anything was
        # tradable — it is evidence a rail said no, which the replay applies
        # itself. 4,612 of these were once counted as open positions; they do not
        # get to grant entries either.
        {"symbol": "SPY", "status": "rejected", "entry_at": "2026-08-04T19:00:00+00:00"},
        {"symbol": "SPY", "status": "closed", "entry_at": None},
    ]
    got = _intrabar_entry_at(rows)
    assert sorted(got) == ["SPY"], got
    assert len(got["SPY"]) == 2, got
    assert all(m.tzinfo is not None for m in got["SPY"])
