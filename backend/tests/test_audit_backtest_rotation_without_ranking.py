"""Rotation follows the ROTATION setting, not the entry-ranking setting.

MEASURED on strategy 29 ("Favorites - optimized 4 aug v2"), whose live config is
`universe: custom`, `rank_enabled: False`, `rotate_on_rank_dropout: True`. The
two engines disagreed about what that means:

  LIVE rotates. `_rotation_exits` collects every strategy with
  `rotate_on_rank_dropout` set and calls `_ranked_symbols_now`, which ranks
  `_strategy_pool` — and that returns the custom symbol list whatever
  `rank_enabled` says. So holdings outside the top-N are sold.

  THE REPLAY did not. Its gate asked `ranking_config`, which returns None unless
  `rank_enabled` is on or the universe is a basket. No ranker, no rotation, ever.

That is a live-vs-replay divergence inside the feature whose whole job is
detecting them, and it was introduced by the rotation exit itself (c96d584) —
the gate was written against the ENTRY ranker, which is a different question
from "may this strategy rotate".

The two settings are genuinely separate: `rank_enabled` cuts ENTRIES to the top
N, `rotate_on_rank_dropout` sells HOLDINGS that fall out of it. A user may
sensibly want the second without the first — buy anything that qualifies, but
hold only what stays near the top. Live already allows that combination, so the
replay has to reproduce it rather than quietly do less.

The ranker still has to be able to rank: an unrankable pool holds, exactly as
before, because an empty ranking means missing data and not "sell everything".
"""

from datetime import datetime, timedelta, timezone

from qt.services.backtest import run_backtest
from qt.services.engine import RISK_DEFAULTS

UTC = timezone.utc
DAY1 = datetime(2026, 5, 4, 14, 0, tzinfo=UTC)
DAY2 = DAY1 + timedelta(days=1)
STEP = timedelta(minutes=15)


def _bar(at: datetime, close: float) -> dict:
    return {"t": at.strftime("%Y-%m-%dT%H:%M:%SZ"), "o": close, "h": close,
            "l": close, "c": close, "v": 1e6, "vw": close}


def _series(day2: list[float]) -> list[dict]:
    bars = [_bar(DAY1 + STEP * n, 100.0) for n in range(4)]
    return bars + [_bar(DAY2 + STEP * n, c) for n, c in enumerate(day2)]


def _strategy(rank_enabled: bool) -> dict:
    return {
        "asset_class": "stock", "swing_mode": True, "sizing_usd": 500.0,
        "sleeve_usd": 5000.0, "max_positions": 5,
        # Strategy 29's shape: a CUSTOM pool, entry-ranking off, rotation on.
        "universe": "custom", "rank_enabled": rank_enabled,
        "rank_by": "momentum_today", "top_n": 1,
        "params": {
            "entry": {"min_day_gain_pct": 1.0, "require_above_vwap": False,
                      "entry_window_start": None, "entry_window_end": None},
            "exit": {"trailing_stop_pct": 0, "stop_loss_pct": 50,
                     "take_profit_pct": 0, "max_holding_hours": 0,
                     "flatten_before_close": False, "exit_below_vwap": False,
                     "rotate_on_rank_dropout": True},
        },
    }


# AAA leads, then BBB overtakes it decisively and AAA — nowhere near its stop —
# drops out of a top_n of one.
AAA = _series([102.0] * 4 + [102.5] * 8)
BBB = _series([101.0] * 4 + [115.0] * 8)


def _run(rank_enabled: bool) -> dict:
    return run_backtest(
        _strategy(rank_enabled), {"AAA": AAA, "BBB": BBB},
        dict(RISK_DEFAULTS, max_total_positions=50), starting_cash=5000, spread_pct=0,
        sim_start=DAY2, sim_end=DAY2 + timedelta(hours=3),
    )


def _rotated(result: dict) -> list[dict]:
    return [t for t in (result.get("trade_list") or [])
            if "rotated out" in (t.get("exit_reason") or "")]


def test_rotation_fires_even_with_entry_ranking_switched_off():
    """The measured divergence. Live sells here; the replay did not."""
    assert _rotated(_run(rank_enabled=False)), "the replay never rotated"


def test_it_still_fires_with_entry_ranking_on():
    """The control — the case that already worked must not regress."""
    assert _rotated(_run(rank_enabled=True))


def test_entry_ranking_off_still_means_every_name_is_a_candidate():
    """`rank_enabled` governs ENTRIES and must keep doing so. Reusing the ranker
    for rotation must not quietly start cutting the entry pool to the top N —
    that would be a second, opposite divergence.

    The fixture is the claim: LAGGARD qualifies on day-gain the whole time but is
    NEVER top-1, because LEADER is always further ahead. So it is entered only if
    the entry pool really is uncut. An earlier version used two names that took
    turns leading, and each was bought either way — the mutation "cut entries to
    the top N too" survived it."""
    leader = _series([120.0] * 12)
    laggard = _series([102.0] * 12)     # over the 1% bar, never the best
    result = run_backtest(
        _strategy(rank_enabled=False), {"LEADER": leader, "LAGGARD": laggard},
        dict(RISK_DEFAULTS, max_total_positions=50), starting_cash=5000, spread_pct=0,
        sim_start=DAY2, sim_end=DAY2 + timedelta(hours=3),
    )
    entries = (result.get("trade_list") or []) + (result.get("open_positions") or [])
    assert {e["symbol"] for e in entries} == {"LEADER", "LAGGARD"}, entries


def test_a_strategy_that_neither_ranks_nor_rotates_builds_no_ranker():
    """Not correctness — cost. `_rotation_top` checks the flag itself, so
    dropping it from `rotation_config` changes no verdict; what it changes is
    that every ordinary backtest would rank its whole pool on every bar for
    nothing. A surviving mutation said the guard was untested rather than
    unnecessary, so this tests the thing it is actually for."""
    plain = _strategy(rank_enabled=False)
    plain["params"]["exit"]["rotate_on_rank_dropout"] = False
    result = run_backtest(
        plain, {"AAA": AAA, "BBB": BBB},
        dict(RISK_DEFAULTS, max_total_positions=50), starting_cash=5000, spread_pct=0,
        sim_start=DAY2, sim_end=DAY2 + timedelta(hours=3),
    )
    assert result.get("ranking") is None, result.get("ranking")
