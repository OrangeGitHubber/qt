"""Per-symbol statistics derived from daily bars.

Pure functions over bar lists (oldest-first) so they're testable without a
broker. The point of these numbers is to make a strategy's settings
judgeable *before* you commit to them — above all ATR%, which tells you
whether a stop is wider than the symbol's ordinary daily noise.
"""

from datetime import datetime, timedelta, timezone


def _ts(bar: dict) -> datetime:
    return datetime.fromisoformat(bar["t"].replace("Z", "+00:00"))


def pct_change_over(bars: list[dict], days: int, current_price: float | None = None) -> float | None:
    """% change from the close ~`days` calendar days ago to `current_price`
    (or the last close). None if history doesn't reach back that far."""
    if not bars:
        return None
    price_now = current_price if current_price is not None else float(bars[-1]["c"])
    cutoff = _ts(bars[-1]) - timedelta(days=days)
    older = [b for b in bars if _ts(b) <= cutoff]
    if not older:
        return None  # don't silently compare against a shorter window
    base = float(older[-1]["c"])
    if not base:
        return None
    return round((price_now / base - 1) * 100, 2)


def atr_pct(bars: list[dict], period: int = 14, current_price: float | None = None) -> float | None:
    """Average True Range as a % of price — the symbol's typical daily move.

    True Range accounts for overnight gaps, not just the day's high-low:
    max(high-low, |high-prevClose|, |low-prevClose|).
    """
    if len(bars) < period + 1:
        return None
    trs = []
    for prev, cur in zip(bars[-(period + 1) : -1], bars[-period:]):
        high, low = float(cur["h"]), float(cur["l"])
        prev_close = float(prev["c"])
        trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    atr = sum(trs) / len(trs)
    price = current_price if current_price is not None else float(bars[-1]["c"])
    if not price:
        return None
    return round(atr / price * 100, 2)


def sma(bars: list[dict], period: int) -> float | None:
    if len(bars) < period:
        return None
    return sum(float(b["c"]) for b in bars[-period:]) / period


def vs_sma_pct(bars: list[dict], period: int = 200, current_price: float | None = None) -> float | None:
    """% above (+) or below (−) the N-day moving average — the same trend
    test the regime filter applies to SPY, per symbol."""
    average = sma(bars, period)
    if not average:
        return None
    price = current_price if current_price is not None else float(bars[-1]["c"])
    return round((price / average - 1) * 100, 2)


def compute(bars: list[dict], current_price: float | None = None) -> dict:
    """All watchlist stats for one symbol. Missing history yields None rather
    than a number computed over a shorter window than advertised."""
    return {
        "change_30d_pct": pct_change_over(bars, 30, current_price),
        "atr_pct": atr_pct(bars, 14, current_price),
        "vs_sma200_pct": vs_sma_pct(bars, 200, current_price),
        "bars_available": len(bars),
    }


def utc_day() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")
