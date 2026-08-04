"""Where the replay's MACD/RSI/ATR actually come from.

The live engine reads all three off COMPLETED DAILY closes, always. A replay
without a daily series computes them from its own bars — on a 1-minute replay
that is a 1-minute MACD, a different signal wearing the same name.

Measured on strategy 25 (2026-08-03): live bought AMZN at 14:01:20 on a bullish
daily MACD; the replay sat through 74 bars of "MACD not bullish" and entered at
14:26 when the 1-MINUTE MACD crossed. The MACD line moved every single minute —
a daily MACD is one value per day. Nothing in the response said which had been
used.

The cause was `_uses_daily_only_signals` returning False whenever the VWAP rule
is on, calling VWAP+MACD "misconfigured" and standing down — so no daily bars
were fetched. Live runs that combination without difficulty.
"""

from qt.api.backtest import _needs_warmup, _uses_daily_only_signals
from qt.services.backtest import _daily_signal_report

MACD_ON = {"entry": {"require_macd_bullish": True}, "exit": {}}
MACD_AND_VWAP = {
    "entry": {"require_macd_bullish": True, "require_above_vwap": True}, "exit": {},
}
NOTHING = {"entry": {}, "exit": {}}

MINUTE, HOUR, DAY = 60, 3600, 86_400


def test_the_vwap_veto_is_why_no_daily_bars_were_fetched():
    """Pin the cause. VWAP+MACD is an ordinary strategy, and the predicate that
    decides 'does this need daily history' must not care about VWAP."""
    assert _uses_daily_only_signals(MACD_AND_VWAP) is False   # the veto, still there
    assert _needs_warmup(MACD_AND_VWAP) is True               # the predicate now used


def test_a_minute_replay_without_daily_bars_is_flagged():
    report = _daily_signal_report(MACD_ON, None, MINUTE)
    assert report["matches_live"] is False
    assert report["computed_from"] == "the replay's own bars"
    assert "1-minute" in report["warning"]
    assert "not comparable to live" in report["warning"]


def test_daily_bars_present_means_no_warning():
    report = _daily_signal_report(MACD_ON, {"AMZN": [{"c": 1}]}, MINUTE)
    assert report["matches_live"] is True
    assert report["computed_from"] == "daily bars"
    assert report["warning"] is None


def test_a_daily_replay_needs_no_daily_series_to_match_live():
    """On 1Day bars the replay's own closes ARE daily closes."""
    report = _daily_signal_report(MACD_ON, None, DAY)
    assert report["matches_live"] is True
    assert report["warning"] is None


def test_an_hourly_replay_without_daily_bars_is_still_flagged():
    report = _daily_signal_report(MACD_ON, None, HOUR)
    assert report["matches_live"] is False
    assert "1-hour" in report["warning"]


def test_a_strategy_with_no_daily_indicator_gets_no_report_at_all():
    """A caveat on every result is a caveat nobody reads."""
    assert _daily_signal_report(NOTHING, None, MINUTE) is None


def test_the_warning_names_the_indicators_in_play():
    rsi = {"entry": {"rsi_min": 30}, "exit": {}}
    assert _daily_signal_report(rsi, None, MINUTE)["indicators"] == ["RSI"]
    assert "RSI" in _daily_signal_report(rsi, None, MINUTE)["warning"]
