"""Two regressions an adversarial audit found in the previous day's own fixes.

Both were introduced by changes that were themselves correct elsewhere — the
kind of defect that only shows up when someone re-reads the diff hunting for it.
"""

import json

from qt.api.backtest import BASELINE_WARMUP_DAYS, warmup_days_for
from qt.api.fidelity import _merge_segment_results


class _Seg:
    """Just enough of _Segment for the merge."""

    def __init__(self, result, live=()):
        self.result = result
        self.live = list(live)


# ---- the universe must stay UNKNOWN if any segment could not report one ----

def test_a_silent_segment_makes_the_merged_universe_unknown():
    """A union let a segment that DID report its symbols lend them to one that
    didn't, so a name the second segment never looked at came back as "covered"
    — reinstating "the replay was watching this symbol and passed"."""
    merged, symbols, _gaps = _merge_segment_results([
        _Seg({"symbols": ["ADA/USD", "ETH/USD"], "trade_list": []}),
        _Seg({"symbols": [], "trade_list": []}),  # could not say what it covered
    ])
    assert symbols == [], "one silent segment must make the whole answer unknown"


def test_the_universe_survives_when_every_segment_reports_one():
    """The other direction — otherwise returning [] unconditionally would pass
    and every comparison would go back to saying "unknown"."""
    _merged, symbols, _gaps = _merge_segment_results([
        _Seg({"symbols": ["ADA/USD"], "trade_list": []}),
        _Seg({"symbols": ["ETH/USD"], "trade_list": []}),
    ])
    assert symbols == ["ADA/USD", "ETH/USD"]


# ---- the deep indicator warm-up must not be applied to an INTRADAY fetch ----

def test_the_baseline_is_far_smaller_than_the_indicator_warmup():
    """The portfolio path fetched its 15-minute bars from the deep daily warm-up
    start — up to 120 days early. For 40 crypto symbols that is roughly 460,000
    bars downloaded to warm indicators an intraday run never reads. Results were
    unaffected (sim_start still gates trading); the cost was the download.

    Pinning the ratio rather than the wiring: if these two ever became the same
    number, the guard that separates them would be pointless."""
    macd = {
        "entry": {"require_macd_bullish": True, "require_above_vwap": True},
        "exit": {"stop_loss_pct": 4},
        "macd": {"fast": 12, "slow": 26, "signal": 9},
    }
    deep = warmup_days_for(macd, "crypto")
    baseline = BASELINE_WARMUP_DAYS["crypto"]
    assert deep >= 100, "the daily warm-up should still be deep enough for MACD"
    assert baseline <= 5
    assert deep > baseline * 10, (
        "an intraday fetch starting at the daily warm-up point is the regression "
        "this ratio exists to make visible"
    )
