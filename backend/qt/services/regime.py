"""Market regime filter: only open new long stock positions while SPY is
above its 200-day simple moving average — a blunt, well-known proxy for
"the tide is rising". Cached because it only changes daily."""

import time

from qt.broker.alpaca import AlpacaClient

_CACHE_TTL = 1800.0  # 30 min
_cache: dict = {"at": 0.0, "value": None}


async def regime_status(client: AlpacaClient) -> dict:
    now = time.monotonic()
    if _cache["value"] is not None and now - _cache["at"] < _CACHE_TTL:
        return _cache["value"]

    bars = await client.stock_bars(["SPY"], timeframe="1Day", limit=210)
    closes = [b["c"] for b in bars.get("SPY", [])]  # newest first (sort=desc)
    if len(closes) < 200:
        # A safety rail fails CLOSED: unknown regime = no new stock entries.
        # Exits are never affected by the regime filter.
        value = {
            "ok": False,
            "insufficient_data": True,
            "detail": f"Only {len(closes)} daily bars for SPY — regime unknown, blocking new stock entries.",
            "spy_close": closes[0] if closes else None,
            "sma200": None,
        }
    else:
        sma200 = sum(closes[:200]) / 200
        last = closes[0]
        value = {
            "ok": last > sma200,
            "insufficient_data": False,
            "detail": f"SPY {last:,.2f} vs 200-day MA {sma200:,.2f}",
            "spy_close": last,
            "sma200": round(sma200, 2),
        }
    _cache.update(at=now, value=value)
    return value


def invalidate_cache() -> None:
    _cache.update(at=0.0, value=None)
