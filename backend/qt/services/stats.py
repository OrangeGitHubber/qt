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


def rsi(bars: list[dict], period: int = 14, current_price: float | None = None) -> float | None:
    """Wilder's Relative Strength Index over the daily closes (0–100). >70 is
    conventionally "overbought", <30 "oversold". Needs at least `period`+1 bars.
    `current_price` (the live quote) replaces the last close so the reading is
    up to date intraday, matching the other stats here.

    Uses Wilder's smoothing: a simple average of the first `period` gains/losses,
    then an exponential-style running average for the rest."""
    if len(bars) < period + 1:
        return None
    closes = [float(b["c"]) for b in bars]
    if current_price is not None:
        closes[-1] = current_price
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(d, 0.0) for d in deltas]
    losses = [max(-d, 0.0) for d in deltas]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0  # no losses = maxed; flat = neutral
    rs = avg_gain / avg_loss
    return round(100 - 100 / (1 + rs), 1)


def _ema_series(values: list[float], period: int) -> list[float | None]:
    """EMA over `values` (oldest-first), seeded with the SMA of the first
    `period` points. Returns a list aligned to `values`; entries before the seed
    are None."""
    if period <= 0 or len(values) < period:
        return [None] * len(values)
    k = 2 / (period + 1)
    out: list[float | None] = [None] * len(values)
    prev = sum(values[:period]) / period  # SMA seed
    out[period - 1] = prev
    for i in range(period, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def macd(closes: list[float], fast: int = 12, slow: int = 26, signal: int = 9) -> tuple[float, float, float] | None:
    """MACD (line, signal, histogram) at the LAST close.

    `closes` is oldest-first and must contain only COMPLETED bars — the caller
    excludes any in-progress bar, so there is NO look-ahead. MACD line =
    EMA(fast) − EMA(slow); signal = EMA(signal) of the MACD line; histogram =
    line − signal. Returns None when there isn't enough history to form the
    signal line (needs ≥ slow + signal points; more to stabilise the EMAs)."""
    if not (0 < fast < slow) or signal <= 0 or len(closes) < slow + signal:
        return None
    ema_fast = _ema_series(closes, fast)
    ema_slow = _ema_series(closes, slow)
    macd_line = [
        (f - s) for f, s in zip(ema_fast, ema_slow) if f is not None and s is not None
    ]
    if len(macd_line) < signal:
        return None
    signal_series = _ema_series(macd_line, signal)
    last_line, last_sig = macd_line[-1], signal_series[-1]
    if last_sig is None:
        return None
    return round(last_line, 6), round(last_sig, 6), round(last_line - last_sig, 6)


def macd_bullish(closes: list[float], fast: int = 12, slow: int = 26, signal: int = 9) -> bool | None:
    """True when the MACD line is above its signal line (momentum bullish),
    False when at/below, None when there isn't enough history to decide."""
    m = macd(closes, fast, slow, signal)
    if m is None:
        return None
    return m[0] > m[1]


def compute(bars: list[dict], current_price: float | None = None) -> dict:
    """All watchlist stats for one symbol. Missing history yields None rather
    than a number computed over a shorter window than advertised."""
    return {
        "change_30d_pct": pct_change_over(bars, 30, current_price),
        "atr_pct": atr_pct(bars, 14, current_price),
        "vs_sma200_pct": vs_sma_pct(bars, 200, current_price),
        "rsi": rsi(bars, 14, current_price),
        "bars_available": len(bars),
    }


def utc_day() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")
