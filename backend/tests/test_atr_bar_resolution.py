"""ATR is a DAILY indicator, and the bar-size rules have to know that.

The live engine computes ATR from completed daily bars. Replay it on 15-minute
bars and a "14-period ATR" measures three and a half HOURS of range instead of
fourteen days — a fraction of the real figure, so every stop derived from it
lands absurdly tight. Replay it on daily bars only, and the stop is checked once
a day at the close, so a stop that would have been hit intraday looks free.

_needs_warmup always counted ATR as a daily indicator; _uses_daily_only_signals
did not. That disagreement is what this file pins down: ATR strategies were
fetched warm-up history and then classified as having no daily signal at all, so
they never qualified for mixed resolution — which is exactly the arrangement
they need.
"""

from datetime import datetime, timedelta, timezone
import pytest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from qt.api.backtest import (
    WARMUP_DAYS,
    _has_price_triggered_exit,
    _mixed_resolution,
    _needs_warmup,
    _uses_daily_only_signals,
    load_scanner_replay_dataset,
)
from qt.services import barcache


def _params(**over) -> dict:
    p = {
        "entry": {"min_day_gain_pct": 1, "require_above_vwap": False},
        "exit": {"trailing_stop_pct": 2, "stop_loss_pct": 8, "take_profit_pct": 10},
    }
    p.update(over)
    return p


# --- classification ---------------------------------------------------------


def test_an_atr_stop_makes_a_strategy_daily_signalled():
    assert _uses_daily_only_signals(_params(atr={"period": 14, "stop_mult": 1.5})) is True


def test_atr_position_sizing_alone_also_counts():
    """risk_usd sizing reads the same daily ATR, so it has the same requirement
    even with no ATR stop."""
    assert _uses_daily_only_signals(_params(atr={"period": 14, "stop_mult": 0, "risk_usd": 50})) is True


def test_a_disabled_atr_block_changes_nothing():
    assert _uses_daily_only_signals(_params(atr={"period": 14, "stop_mult": 0, "risk_usd": 0})) is False


def test_the_vwap_rule_still_takes_precedence():
    """VWAP needs intraday and ATR wants daily; the pre-existing rule is that the
    VWAP guard wins so the two can't deadlock. Adding ATR must not change that."""
    p = _params(atr={"period": 14, "stop_mult": 1.5})
    p["entry"]["require_above_vwap"] = True
    assert _uses_daily_only_signals(p) is False


def test_the_atr_stop_counts_as_a_price_triggered_exit_on_its_own():
    """It IS a stop — just one whose distance comes from volatility. It has to be
    named explicitly because it REPLACES stop_loss_pct, so the fixed percentage
    can legitimately be zero."""
    p = _params(
        exit={"trailing_stop_pct": 0, "stop_loss_pct": 0, "take_profit_pct": 0},
        atr={"period": 14, "stop_mult": 1.5},
    )
    assert _has_price_triggered_exit(p) is True


def test_an_atr_strategy_with_stops_is_mixed_resolution():
    """THE case. Daily signal + price-triggered exit = the one arrangement a
    single bar stream cannot serve, and the shape of every ATR scalper."""
    assert _mixed_resolution(_params(atr={"period": 14, "stop_mult": 1.5})) is True


def test_warmup_and_signal_classification_now_agree():
    """The two used to disagree, which is how the bug hid: warm-up bars were
    fetched for a strategy that was then treated as having no daily signal."""
    p = _params(atr={"period": 14, "stop_mult": 1.5})
    assert _needs_warmup(p) == _uses_daily_only_signals(p) is True


# --- the scanner-replay dataset --------------------------------------------


def _cache(monkeypatch):
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    barcache.CacheBase.metadata.create_all(eng)
    Sess = sessionmaker(bind=eng, expire_on_commit=False)
    monkeypatch.setattr(barcache, "_engine", eng)
    monkeypatch.setattr(barcache, "_Session", Sess)
    return Sess


def _seed(monkeypatch, *, window_days: int = 30):
    """A mover inside the window, plus daily bars reaching well before it — the
    history an ATR needs to be defined on the window's first day."""
    Sess = _cache(monkeypatch)
    now = datetime.now(timezone.utc)
    in_window = (now - timedelta(days=window_days - 5)).strftime("%Y-%m-%d")
    before_window = (now - timedelta(days=window_days + 20)).strftime("%Y-%m-%d")
    with Sess() as s:
        barcache.save_daily_bars(s, "BTC/USD", [
            {"t": f"{before_window}T00:00:00Z", "o": 100, "h": 101, "l": 99, "c": 100, "v": 1e6, "vw": 100},
            {"t": f"{in_window}T00:00:00Z", "o": 105, "h": 106, "l": 104, "c": 105, "v": 1e6, "vw": 105},
        ], model=barcache.CryptoDailyBar)
        barcache.store_movers(s, in_window, [("BTC/USD", 5.0, 105.0, 1e8)],
                              model=barcache.CryptoDailyMover)
        s.commit()
    return in_window, before_window


def test_the_dataset_carries_daily_bars_from_before_the_window(monkeypatch):
    """The indicator source reaches back over the warm-up. Without this the ATR
    is undefined for the window's first weeks — on a 180-day replay, a fifth of
    the test runs with a dead signal."""
    _, before_window = _seed(monkeypatch)
    ds = load_scanner_replay_dataset("crypto", 30, 10)
    assert any(b["t"][:10] == before_window for b in ds.daily["BTC/USD"])


def test_the_replay_timeline_still_starts_at_the_window(monkeypatch):
    """Warm-up bars are for indicators only. If they leaked into the replay the
    tested window would silently grow by WARMUP_DAYS and every reported return
    would cover a different period than the one asked for."""
    _, before_window = _seed(monkeypatch)
    ds = load_scanner_replay_dataset("crypto", 30, 10)
    assert all(b["t"][:10] >= ds.start_day for b in ds.bars["BTC/USD"])
    assert before_window < ds.start_day  # the warm-up bar really is outside


def test_the_warmup_reaches_back_the_full_lookback(monkeypatch):
    _seed(monkeypatch)
    ds = load_scanner_replay_dataset("crypto", 30, 10)
    earliest = min(b["t"][:10] for b in ds.daily["BTC/USD"])
    limit = (datetime.strptime(ds.start_day, "%Y-%m-%d") - timedelta(days=WARMUP_DAYS)).strftime("%Y-%m-%d")
    assert earliest >= limit


# --- short windows cannot be served by daily bars ---------------------------


def _seed_short(monkeypatch, *, with_intraday: bool, second_symbol: bool = False):
    """Today's movers with a daily bar each, optionally with intraday cover.

    A daily bar is stamped at the START of its day, so for a window of a few
    hours it sits BEFORE the window and can never trade — which is the whole
    problem: the daily fallback looks safe and yields an empty replay."""
    Sess = _cache(monkeypatch)
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    symbols = ["BTC/USD"] + (["ETH/USD"] if second_symbol else [])
    with Sess() as s:
        for sym in symbols:
            barcache.save_daily_bars(s, sym, [
                {"t": f"{day}T00:00:00Z", "o": 100, "h": 106, "l": 99, "c": 105, "v": 1e6, "vw": 103},
            ], model=barcache.CryptoDailyBar)
        # Only the FIRST symbol gets intraday, so a second symbol makes coverage
        # partial — the case that used to flip the whole replay to daily.
        if with_intraday:
            barcache.save_intraday_bars(s, "BTC/USD", [
                {"t": f"{day}T{h:02d}:00:00Z", "o": 100, "h": 106, "l": 99, "c": 100 + h, "v": 1e4, "vw": 100 + h}
                for h in range(20, 24)
            ], model=barcache.CryptoIntradayBar)
        barcache.store_movers(
            s, day, [(sym, 5.0, 105.0, 1e8) for sym in symbols],
            model=barcache.CryptoDailyMover,
        )
        s.commit()
    return day


def test_a_short_window_uses_intraday_even_on_partial_coverage(monkeypatch):
    """Full intraday coverage is the rule everywhere else, and rightly so. Here
    it must bend: falling back to daily on a 4-hour window returns nothing at
    all, which the fidelity report then blames on the strategy."""
    _seed_short(monkeypatch, with_intraday=True, second_symbol=True)
    ds = load_scanner_replay_dataset("crypto", 1, 10, window_hours=4.0)
    assert ds.used_intraday is True
    assert ds.timeframe == "15Min"
    assert ds.bars.get("BTC/USD"), "the covered symbol must still be replayed"


def test_a_long_window_still_demands_full_coverage(monkeypatch):
    """The bend applies ONLY to short windows — otherwise one incidentally
    cached symbol would flip a 180-day replay to intraday and silently drop
    every uncovered name."""
    _seed_short(monkeypatch, with_intraday=True, second_symbol=True)
    ds = load_scanner_replay_dataset("crypto", 30, 10, window_hours=24 * 30)
    assert ds.used_intraday is False
    assert ds.timeframe == "1Day"


def test_a_short_window_with_no_intraday_at_all_refuses_loudly(monkeypatch):
    """The one outcome worse than an error is a silent empty replay presented as
    a verdict on the strategy."""
    from fastapi import HTTPException

    _seed_short(monkeypatch, with_intraday=False)
    with pytest.raises(HTTPException) as caught:
        load_scanner_replay_dataset("crypto", 1, 10, window_hours=4.0)
    assert caught.value.status_code == 422
    assert "intraday" in caught.value.detail


# --- the DAILY replay needs a baseline for its first window day too ----------


def _seed_daily_window(monkeypatch, *, days_before: int = 4, window_days: int = 3):
    """Daily bars running from before the window through to today, with a mover
    on the window's FIRST day — the day that used to be unjudgeable.

    `first_day` is derived the way the loader derives `start_day` (finish minus
    `days`), not from the seed's own shape. Getting that wrong is how the first
    version of this test passed against the very bug it was written for: the
    assertion landed a day inside the window, where a baseline already existed.
    """
    Sess = _cache(monkeypatch)
    today = datetime.now(timezone.utc)
    first_day = (today - timedelta(days=window_days)).strftime("%Y-%m-%d")
    bars = []
    for i in range(days_before + window_days):
        d = today - timedelta(days=days_before + window_days - 1 - i)
        c = 100 + i
        bars.append({"t": d.strftime("%Y-%m-%dT00:00:00Z"), "o": c, "h": c, "l": c,
                     "c": c, "v": 1e6, "vw": c})
    with Sess() as s:
        barcache.save_daily_bars(s, "BTC/USD", bars, model=barcache.CryptoDailyBar)
        for i in range(window_days + 1):
            day = (today - timedelta(days=window_days - i)).strftime("%Y-%m-%d")
            barcache.store_movers(s, day, [("BTC/USD", 5.0, 105.0, 1e8)],
                                  model=barcache.CryptoDailyMover)
        s.commit()
    return first_day


def test_a_daily_replay_can_judge_the_first_day_of_its_window(monkeypatch):
    """The daily path trimmed its bars to the window's own first day, so that day
    had nothing to measure a day-gain against — and `_simulate` drops a bar whose
    change_pct is None without a word. Every daily scanner replay was blind on
    day one, and a one-day window saw nothing at all.

    Asserted on the OUTCOME, not the trim: the first in-window bar must come out
    of `_prepare` with a real change_pct."""
    from qt.services.backtest import _day_fn, _prepare

    first_day = _seed_daily_window(monkeypatch)
    ds = load_scanner_replay_dataset("crypto", 3, 10)

    assert ds.timeframe == "1Day"
    prepared = _prepare(ds.bars["BTC/USD"], _day_fn("crypto"), rolling_24h=True)
    in_window = [b for b in prepared if b["day"] >= first_day]
    assert in_window, "no bars landed inside the window at all — seed is wrong"
    assert in_window[0]["change_pct"] is not None, (
        "the window's first day had no prior bar to measure against, so the "
        "replay silently skipped it"
    )


def test_the_baseline_prefix_is_not_counted_as_coverage(monkeypatch):
    """The prefix exists to be a reference price and can never trade. A symbol
    holding only prefix bars must not inflate the replayed universe — otherwise
    widening the baseline would quietly make coverage look better."""
    _seed_daily_window(monkeypatch)
    ds = load_scanner_replay_dataset("crypto", 3, 10)
    assert ds.replayed == ["BTC/USD"]
    assert ds.dropped == []
