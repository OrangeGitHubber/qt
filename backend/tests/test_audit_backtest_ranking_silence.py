"""Audit (2026-08-03) of _PoolRanker's SILENCE about what it could not rank.

`ranking.rank_symbols` drops a symbol whose metric is None — correct, and the
same thing the live engine does. What was missing is the report. `applied` only
goes False when NO symbol has any daily history at all; between that and a clean
run sits the case that actually bites a user:

  * relative_strength is a 200-day average. Hand the replay 150 daily bars and
    EVERY member is unrankable on EVERY bar, `rank_symbols` returns an empty top-N
    every time, nothing is ever offered to the entry rules — and the result said
    `applied: true`, `trades: 0`, with no warning. On screen that is
    indistinguishable from "your rules never triggered", which is the wrong thing
    to conclude when the replay never evaluated a single candidate.

  * One thin name in an otherwise fine pool is silently un-buyable for the whole
    run while the rest trades normally.

Measured against a real run today: `symbol_bars_unrankable: 29` of 261 ranked,
with nothing on screen saying which names those were or that they could not be
bought.
"""

from datetime import datetime, timedelta, timezone

from qt.services.backtest import _PoolRanker, _utc_day

D = datetime(2026, 5, 5, tzinfo=timezone.utc)


def _daily(n: int, close: float = 100.0) -> list[dict]:
    """`n` daily bars ending the day before D."""
    return [
        {"t": (D - timedelta(days=n - i)).strftime("%Y-%m-%dT00:00:00Z"),
         "c": close + i * 0.01, "h": close, "l": close, "v": 100, "vw": close}
        for i in range(n)
    ]


def _prepared(symbols: list[str]) -> dict[str, list[dict]]:
    return {
        s: [{"ts": D, "day": _utc_day(D), "close": 100.0, "change_pct": 1.0}]
        for s in symbols
    }


def _ranker(daily: dict[str, list[dict]], symbols: list[str], top_n: int = 5) -> _PoolRanker:
    prepared = _prepared(symbols)
    r = _PoolRanker("relative_strength", top_n, prepared, daily, _utc_day)
    r.rank({s: prepared[s][0] for s in symbols}, D)
    return r


def test_a_pool_that_can_never_be_ranked_says_no_trade_was_possible():
    """200-day metric, 150 days of history: nothing rankable, so nothing could be
    bought — and the run has to say that rather than let "0 trades" be read as a
    verdict on the entry rules."""
    symbols = ["AAA", "BBB"]
    ranker = _ranker({s: _daily(150) for s in symbols}, symbols)
    report = ranker.report()
    assert report["applied"] is True          # daily bars WERE present; this is not that case
    assert report["symbol_bars_unrankable"] == 2
    assert sorted(report["symbols_never_rankable"]) == symbols
    assert report["warning"] and "NO trade was possible" in report["warning"]


def test_one_short_name_in_a_healthy_pool_is_named():
    """The rest of the pool ranks fine, so the run is not a write-off — but the
    thin name could not be bought once, all run, and nothing used to say so."""
    symbols = ["AAA", "BBB", "SHORTY"]
    daily = {"AAA": _daily(250), "BBB": _daily(250), "SHORTY": _daily(150)}
    report = _ranker(daily, symbols).report()
    assert report["symbols_never_rankable"] == ["SHORTY"]
    assert report["warning"] and "SHORTY" in report["warning"]
    assert "could never be ranked" in report["warning"]


def test_a_pool_that_ranks_cleanly_carries_no_warning():
    """The anti-vacuity control: the warning must be a finding, not a fixture. A
    pool with enough history for every member says nothing at all."""
    symbols = ["AAA", "BBB"]
    report = _ranker({s: _daily(250) for s in symbols}, symbols).report()
    assert report["symbols_never_rankable"] == []
    assert report["warning"] is None


def test_a_name_that_ranks_on_some_bars_is_not_reported_as_never_rankable():
    """"Unrankable on this bar" and "unrankable all run" are different claims. A
    symbol that ranked even once is buyable, and naming it would send somebody
    hunting for missing data that is not missing.

    The fixture has to straddle that line or it proves nothing: AAA gets a bar on
    a day BEFORE its daily history opens (no prefix → unrankable) and another on a
    day well inside it (250 bars → rankable). Reporting the bare unrankable set
    would name it; the difference against the rankable set must not."""
    early = D - timedelta(days=400)
    prepared = {
        "AAA": [
            {"ts": early, "day": _utc_day(early), "close": 100.0, "change_pct": 1.0},
            {"ts": D, "day": _utc_day(D), "close": 100.0, "change_pct": 1.0},
        ]
    }
    ranker = _PoolRanker("relative_strength", 5, prepared, {"AAA": _daily(250)}, _utc_day)
    ranker.rank({"AAA": prepared["AAA"][0]}, early)   # no prefix yet → unrankable
    ranker.rank({"AAA": prepared["AAA"][1]}, D)       # full prefix → rankable
    report = ranker.report()
    assert report["symbol_bars_unrankable"] == 1, "the fixture never hit an unrankable bar"
    assert report["symbols_never_rankable"] == []
    assert report["warning"] is None
