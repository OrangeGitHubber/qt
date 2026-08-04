"""The replay never rotated a position out. The engine does.

MEASURED on strategy 25. Live bought SPY at 11:25, sold at 14:37 "rotated out of
the top 5 by relative strength", bought straight back in, sold again at 14:58,
and again at 15:00 — three round trips in thirty-five minutes. The replay bought
once and held to the end of the window, so every one of those live re-entries had
no counterpart and the report filled with rows about a difference that had one
cause.

`rotate_on_rank_dropout` appeared in exactly three places: the strategy model
(`api/strategies.py`), the live engine (`engine._rotation_exits`), and a preset
that switches it on and calls it "THE rotation rule". It appeared NOWHERE in
`services/backtest.py`. The simulator could not rotate a position out under any
circumstances.

That is not a fidelity-report problem. Every backtest and every optimizer run on
a rotation strategy was simulating a strategy that never rotates — the entries
were the strategy's, the exits were somebody else's.

THE ENGINE'S RULE, copied rather than reinvented (see `_rotation_exits`): rank
the strategy's pool now, and sell any holding that is not in the top N. If the
ranking cannot be produced at all, HOLD — live logs "couldn't rank right now —
don't rotate blindly" and does nothing, because rotating on an empty ranking
would liquidate the book on a data gap.
"""

from datetime import datetime, timedelta, timezone

from qt.services.backtest import run_backtest
from qt.services.engine import RISK_DEFAULTS

UTC = timezone.utc
DAY0 = datetime(2026, 5, 4, 14, 0, tzinfo=UTC)


def _bar(at: datetime, close: float) -> dict:
    return {"t": at.strftime("%Y-%m-%dT%H:%M:%SZ"), "o": close, "h": close,
            "l": close, "c": close, "v": 1e6, "vw": close}


def _series(day2: list[float]) -> list[dict]:
    """A flat prior day for the baseline, then day two minute by minute."""
    out = [_bar(DAY0 + timedelta(minutes=n), 100.0) for n in range(0, 60, 15)]
    start = DAY0 + timedelta(days=1)
    return out + [_bar(start + timedelta(minutes=i), c) for i, c in enumerate(day2)]


def _strategy(rotate: bool) -> dict:
    return {
        "asset_class": "stock", "swing_mode": True, "sizing_usd": 500.0,
        "sleeve_usd": 5000.0, "max_positions": 5,
        # A ranked universe is what makes rotation meaningful at all.
        "universe": "basket", "rank_enabled": True, "rank_by": "momentum_today",
        "top_n": 1,
        "params": {
            "entry": {"min_day_gain_pct": 1.0, "require_above_vwap": False,
                      "entry_window_start": None, "entry_window_end": None},
            "exit": {"trailing_stop_pct": 0, "stop_loss_pct": 50,
                     "take_profit_pct": 0, "max_holding_hours": 0,
                     "flatten_before_close": False, "exit_below_vwap": False,
                     "rotate_on_rank_dropout": rotate},
        },
    }


# AAA leads for twenty minutes, then BBB overtakes it decisively and AAA — still
# well above its stop — drops out of a top_n of one.
AAA = _series([102.0] * 20 + [102.5] * 40)
BBB = _series([101.0] * 20 + [115.0] * 40)


def _run(rotate: bool) -> dict:
    return run_backtest(
        _strategy(rotate), {"AAA": AAA, "BBB": BBB},
        dict(RISK_DEFAULTS, max_total_positions=50), starting_cash=5000, spread_pct=0,
        sim_start=DAY0 + timedelta(days=1), sim_end=DAY0 + timedelta(days=1, hours=2),
    )


def _closed(result: dict) -> list[dict]:
    return result.get("trade_list") or []


def test_a_holding_that_leaves_the_top_n_is_sold():
    result = _run(rotate=True)
    rotated = [t for t in _closed(result) if "rotated out" in (t.get("exit_reason") or "")]
    assert rotated, [t.get("exit_reason") for t in _closed(result)] or "nothing closed at all"
    assert rotated[0]["symbol"] == "AAA"


def test_the_reason_is_word_for_word_the_engine_s():
    """`_same_rule` compares the first few words of the reason, so a replayed
    rotation that says anything else reads as a DIFFERENT rule from live's and
    the fidelity report calls a perfect agreement a disagreement."""
    rotated = [t for t in _closed(_run(rotate=True))
               if "rotated out" in (t.get("exit_reason") or "")]
    assert rotated[0]["exit_reason"] == "rotated out of the top 1 by momentum today", \
        rotated[0]["exit_reason"]


def test_without_the_setting_nothing_rotates():
    """The control, and the guard on blast radius: this rule is opt-in, and a
    strategy that never asked for it must replay exactly as it did before."""
    result = _run(rotate=False)
    assert not [t for t in _closed(result) if "rotated out" in (t.get("exit_reason") or "")]


def test_an_unrankable_pool_holds_rather_than_liquidating():
    """Live's own guard: "couldn't rank right now — don't rotate blindly". A
    ranking that comes back empty is missing data, and selling the whole book on
    a data gap is the worst possible reading of it."""
    flat = _series([102.0] * 60)
    result = run_backtest(
        dict(_strategy(True), rank_by="relative_strength"),   # needs 200 daily bars
        {"AAA": flat, "BBB": flat},
        dict(RISK_DEFAULTS, max_total_positions=50), starting_cash=5000, spread_pct=0,
        sim_start=DAY0 + timedelta(days=1), sim_end=DAY0 + timedelta(days=1, hours=2),
    )
    assert not [t for t in _closed(result) if "rotated out" in (t.get("exit_reason") or "")]


def test_the_portfolio_replay_rotates_too():
    """Two independent bar loops, and a fix that lands on one of them is the
    shape of most of the bugs this file has had. run_portfolio_backtest ranks per
    strategy inside its ENTRY pass; the exit pass had to learn to rank too."""
    from qt.services.backtest import run_portfolio_backtest

    result = run_portfolio_backtest(
        [dict(_strategy(True), id=1, name="rotator")],
        {1: {"AAA": AAA, "BBB": BBB}},
        dict(RISK_DEFAULTS, max_total_positions=50), starting_cash=5000, spread_pct=0,
        sim_start=DAY0 + timedelta(days=1), sim_end=DAY0 + timedelta(days=1, hours=2),
    )
    closed = result.get("trade_list") or []
    assert [t for t in closed if "rotated out" in (t.get("exit_reason") or "")], \
        [t.get("exit_reason") for t in closed] or "nothing closed at all"


def test_a_ranking_that_comes_back_empty_holds_everything():
    """The guard the test above does NOT reach, and a surviving mutation is what
    said so: that one leaves `applied` False, so the empty-ranking branch is
    never entered.

    `applied` True with an EMPTY ranking is a real and documented state — see
    _PoolRanker's note, where relative_strength over a series shorter than its
    200-day lookback makes every member unrankable on every bar. Reading that as
    "nothing belongs in the top N" would sell the entire book on missing data.
    Live refuses to: "couldn't rank right now — don't rotate blindly"."""
    from qt.services.backtest import _rotation_top

    class _Ranker:
        applied = True

    params = {"exit": {"rotate_on_rank_dropout": True}}
    assert _rotation_top(params, _Ranker(), []) is None
    # …and a ranking that DID produce names still rotates against it.
    assert _rotation_top(params, _Ranker(), [("BBB", 1, 1)]) == {"BBB"}
