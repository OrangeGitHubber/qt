"""The ranking counters on a SPLIT fidelity comparison.

Found by measurement, not by reading: strategy 25's report showed 261 ranked /
116 cut / 29 unrankable, while replaying the stretch that actually traded
straight through /api/backtest evaluated 5,106 symbol-bars. The report was
showing the FIRST stretch's counters — the silent overnight one — as though they
described the whole comparison.
"""

from qt.api.fidelity import _merge_ranking, _ranking_report


def _ranking(**kw) -> dict:
    base = {
        "applied": True,
        "symbols_never_rankable": [],
        "rank_by": "relative_strength",
        "top_n": 5,
        "pool_size": 10,
        "metric_source": "daily bars",
        "symbol_bars_ranked": 100,
        "symbol_bars_cut": 40,
        "symbol_bars_unrankable": 0,
        "benchmark_missing": False,
        "warning": None,
    }
    base.update(kw)
    return base


def test_the_counters_are_added_up_not_taken_from_the_first_stretch():
    """The defect exactly: the quiet stretch's numbers standing in for both."""
    quiet = _ranking(symbol_bars_ranked=261, symbol_bars_cut=116, symbol_bars_unrankable=29)
    traded = _ranking(symbol_bars_ranked=3272, symbol_bars_cut=1456, symbol_bars_unrankable=378)

    merged = _merge_ranking([quiet, traded])

    assert merged["symbol_bars_ranked"] == 261 + 3272
    assert merged["symbol_bars_cut"] == 116 + 1456
    assert merged["symbol_bars_unrankable"] == 29 + 378
    assert merged["stretches_counted"] == 2


def test_one_stretch_that_could_not_rank_makes_the_merged_claim_false():
    """Contagious in the false direction: some trades below were picked unranked."""
    merged = _merge_ranking([_ranking(applied=True), _ranking(applied=False)])
    assert merged["applied"] is False


def test_a_descriptor_that_differs_between_stretches_is_not_reported_as_one_value():
    """A config edit is what splits a comparison, so top_n can genuinely differ."""
    merged = _merge_ranking([_ranking(top_n=5), _ranking(top_n=3)])
    assert merged["top_n"] is None
    assert merged["rank_by"] == "relative_strength"  # agreed, so it survives


def test_a_descriptor_all_stretches_agree_on_survives_the_merge():
    merged = _merge_ranking([_ranking(pool_size=10), _ranking(pool_size=10)])
    assert merged["pool_size"] == 10


def test_never_rankable_names_are_pooled_across_stretches():
    merged = _merge_ranking([
        _ranking(symbols_never_rankable=["SPCX"]),
        _ranking(symbols_never_rankable=["GOOP"]),
    ])
    assert merged["symbols_never_rankable"] == ["GOOP", "SPCX"]


def test_a_split_comparison_says_the_never_rankable_list_is_a_weaker_claim():
    """The union cannot tell the reader WHICH stretch a name failed in."""
    merged = _merge_ranking([_ranking(symbols_never_rankable=["SPCX"]), _ranking()])
    assert "at least one" in (merged["warning"] or "")
    assert "2 stretches" in (merged["warning"] or "")


def test_a_single_stretch_is_left_exactly_as_it_came():
    """An unsegmented comparison must not grow split-comparison caveats."""
    merged = _merge_ranking([_ranking(warning="pool too small")])
    assert merged["warning"] == "pool too small"
    assert "stretches_counted" not in merged


def test_benchmark_missing_in_any_stretch_is_reported():
    merged = _merge_ranking([_ranking(benchmark_missing=False), _ranking(benchmark_missing=True)])
    assert merged["benchmark_missing"] is True


def test_no_ranking_anywhere_stays_none():
    assert _merge_ranking([]) is None


def test_the_report_percentage_is_computed_off_the_merged_total():
    """The share quoted to the reader must describe the whole comparison."""
    a = {"ranking": _ranking(symbol_bars_ranked=100, symbol_bars_unrankable=0)}
    b = {"ranking": _ranking(symbol_bars_ranked=100, symbol_bars_unrankable=50)}

    report = _ranking_report([a, b])

    assert report["symbol_bars_ranked"] == 200
    assert report["symbol_bars_unrankable"] == 50
    # 50 of 200 is 25% — off the first stretch alone it would read 0, and the
    # whole unrankable_effect sentence would be suppressed entirely.
    assert "25.0%" in report["unrankable_effect"]


def test_a_clean_split_comparison_still_says_nothing_about_unrankable_bars():
    """Anti-vacuity: the sentence appears because of the data, not always."""
    a = {"ranking": _ranking(symbol_bars_unrankable=0)}
    b = {"ranking": _ranking(symbol_bars_unrankable=0)}
    assert _ranking_report([a, b])["unrankable_effect"] is None
