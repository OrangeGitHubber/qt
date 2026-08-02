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
