"""Market scanner: find today's rising stocks and crypto, filtered by
UI-configurable rules. Results are cached briefly to respect Alpaca's
rate limits no matter how often the UI polls."""

import time
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from qt.broker.alpaca import AlpacaClient, AlpacaError
from qt.settings_service import get_setting

CONFIG_KEY = "scanner_config"

# Stocks and crypto need DIFFERENT floors: a $5M volume floor is right for
# stocks but starves crypto (which resets volume at 00:00 UTC), and the $1
# stock price floor would wrongly exclude sub-$1 coins like DOGE. So the
# filters are per-asset-class; top_n and the exclude list stay shared.
STOCK_DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "min_price": 1.0,                 # skip sub-$1 (penny/OTC junk)
    "max_price": 0.0,                 # 0 = no cap
    "min_change_pct": 2.0,            # only "improving" symbols
    "min_dollar_volume": 5_000_000,   # daily $ volume floor (liquidity)
}
CRYPTO_DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "min_price": 0.0,                 # coins can be sub-$1 (DOGE, etc.)
    "max_price": 0.0,
    "min_change_pct": 1.0,
    "min_dollar_volume": 1_000_000,   # realistic for the free crypto feed
}
DEFAULT_CONFIG: dict[str, Any] = {
    "top_n": 10,                      # rows shown per asset class
    "exclude_symbols": [],
    "stocks": dict(STOCK_DEFAULTS),
    "crypto": dict(CRYPTO_DEFAULTS),
}

_CACHE_TTL_SECONDS = 30
_cache: dict[str, Any] = {"at": 0.0, "config": None, "result": None}


def get_config(session: Session) -> dict[str, Any]:
    return _normalize(get_setting(session, CONFIG_KEY) or {})


def _normalize(stored: dict[str, Any]) -> dict[str, Any]:
    """Return the nested per-class config, migrating the pre-split flat shape.

    Old configs stored one set of floors (min_price/min_change_pct/…) shared by
    both asset classes. On upgrade we copy those onto BOTH classes so behavior
    is preserved; the user can then differentiate them.
    """
    cfg: dict[str, Any] = {
        "top_n": stored.get("top_n", DEFAULT_CONFIG["top_n"]),
        "exclude_symbols": stored.get("exclude_symbols", []),
        "stocks": dict(STOCK_DEFAULTS),
        "crypto": dict(CRYPTO_DEFAULTS),
    }
    is_flat = "stocks" not in stored and "crypto" not in stored and any(
        k in stored for k in ("min_price", "max_price", "min_change_pct", "min_dollar_volume")
    )
    if is_flat:
        for cls, en_key, defaults in (
            ("stocks", "stocks_enabled", STOCK_DEFAULTS),
            ("crypto", "crypto_enabled", CRYPTO_DEFAULTS),
        ):
            cfg[cls] = {
                "enabled": stored.get(en_key, True),
                "min_price": stored.get("min_price", defaults["min_price"]),
                "max_price": stored.get("max_price", defaults["max_price"]),
                "min_change_pct": stored.get("min_change_pct", defaults["min_change_pct"]),
                "min_dollar_volume": stored.get("min_dollar_volume", defaults["min_dollar_volume"]),
            }
        return cfg
    for cls, defaults in (("stocks", STOCK_DEFAULTS), ("crypto", CRYPTO_DEFAULTS)):
        if isinstance(stored.get(cls), dict):
            cfg[cls] = {**defaults, **stored[cls]}
    return cfg


def _passes(f: dict, exclude_symbols: list, price: float, change_pct: float, dollar_volume: float, symbol: str) -> bool:
    if symbol.upper() in (s.upper() for s in exclude_symbols):
        return False
    if price < f["min_price"]:
        return False
    if f["max_price"] and price > f["max_price"]:
        return False
    if change_pct < f["min_change_pct"]:
        return False
    if dollar_volume < f["min_dollar_volume"]:
        return False
    return True


def _daily_dollar_volume(snapshot: dict) -> float:
    bar = snapshot.get("dailyBar") or {}
    volume = bar.get("v") or 0
    ref_price = bar.get("vw") or bar.get("c") or 0
    return float(volume) * float(ref_price)


def _rolling_24h(bars: list[dict]) -> tuple[float, float, float] | None:
    """(price, % change, $ volume) over the trailing ~24h from hourly bars.

    Crypto has no daily close, so we use a ROLLING 24-hour window rather than
    the 00:00-UTC calendar bar. That removes the timezone boundary entirely
    (matches how crypto sites quote "24h change") and, crucially, means the
    scanner isn't blind to crypto for the first hours of each UTC day while a
    fresh calendar bar slowly accumulates volume.
    """
    if not bars:
        return None
    # Newest first (the client requests sort=desc, but sort defensively).
    window = sorted(bars, key=lambda b: b.get("t", ""), reverse=True)[:24]
    current = window[0].get("c")
    oldest = window[-1]
    ref = oldest.get("o") or oldest.get("c")
    if not current or not ref:
        return None
    change_pct = (float(current) - float(ref)) / float(ref) * 100
    dollar_volume = sum(float(b.get("v") or 0) * float(b.get("vw") or b.get("c") or 0) for b in window)
    return float(current), change_pct, dollar_volume


def _meta(scanned: int, best: tuple[str, float, float, float] | None) -> dict[str, Any]:
    """Diagnostics so an empty panel can explain itself: how many symbols had
    usable data, and the strongest mover seen (before filtering) with its price
    and $ volume — so the UI can name the exact floor that stopped it."""
    return {
        "scanned": scanned,
        "best_symbol": best[0] if best else None,
        "best_change_pct": round(best[1], 2) if best else None,
        "best_price": round(best[2], 4) if best else None,
        "best_dollar_volume": round(best[3]) if best else None,
    }


async def scan_stocks(client: AlpacaClient, cfg: dict) -> tuple[list[dict], dict]:
    f = cfg["stocks"]
    movers = await client.stock_movers(top=50)
    gainers = movers.get("gainers", [])
    symbols = [g["symbol"] for g in gainers]
    snapshots = await client.stock_snapshots(symbols)

    rows = []
    best: tuple[str, float, float, float] | None = None
    for gainer in gainers:
        symbol = gainer["symbol"]
        snapshot = snapshots.get(symbol) or {}
        price = float(gainer.get("price") or 0)
        change_pct = float(gainer.get("percent_change") or 0)
        dollar_volume = _daily_dollar_volume(snapshot)
        if best is None or change_pct > best[1]:
            best = (symbol, change_pct, price, dollar_volume)
        if _passes(f, cfg["exclude_symbols"], price, change_pct, dollar_volume, symbol):
            rows.append(
                {
                    "symbol": symbol,
                    "asset_class": "stock",
                    "price": price,
                    "change_pct": round(change_pct, 2),
                    "dollar_volume": round(dollar_volume),
                }
            )
    rows.sort(key=lambda r: r["change_pct"], reverse=True)
    return rows[: cfg["top_n"]], _meta(len(gainers), best)


async def scan_crypto(client: AlpacaClient, cfg: dict) -> tuple[list[dict], dict]:
    f = cfg["crypto"]
    assets = await client.crypto_assets()
    usd_pairs = [a["symbol"] for a in assets if a["symbol"].endswith("/USD")]
    # Hourly bars over a rolling 24h window instead of the 00:00-UTC daily bar.
    # IMPORTANT: use the time-windowed, paginated historical endpoint — NOT
    # crypto_bars(limit=N). Alpaca's `limit` on the multi-symbol bars endpoint
    # is a TOTAL cap across all symbols, so a small limit gets consumed by the
    # first symbol or two and every other pair comes back empty (that bug read
    # as "scanned 2 symbols" with ~$0 volume). `start` = ~25h ago (24h + the
    # current partial hour) gives each pair its full window.
    start_iso = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
    bars_by_symbol = await client.historical_bars(usd_pairs, "crypto", "1Hour", start_iso)

    rows = []
    scanned = 0
    best: tuple[str, float, float, float] | None = None
    for symbol in usd_pairs:
        stats = _rolling_24h(bars_by_symbol.get(symbol) or [])
        if stats is None:
            continue
        scanned += 1
        price, change_pct, dollar_volume = stats
        if best is None or change_pct > best[1]:
            best = (symbol, change_pct, price, dollar_volume)
        if _passes(f, cfg["exclude_symbols"], price, change_pct, dollar_volume, symbol):
            rows.append(
                {
                    "symbol": symbol,
                    "asset_class": "crypto",
                    "price": price,
                    "change_pct": round(change_pct, 2),
                    "dollar_volume": round(dollar_volume),
                }
            )
    rows.sort(key=lambda r: r["change_pct"], reverse=True)
    return rows[: cfg["top_n"]], _meta(scanned, best)


async def scan(session: Session, client: AlpacaClient) -> dict[str, Any]:
    cfg = get_config(session)
    now = time.monotonic()
    if _cache["result"] is not None and _cache["config"] == cfg and now - _cache["at"] < _CACHE_TTL_SECONDS:
        return _cache["result"]

    result: dict[str, Any] = {
        "stocks": [],
        "crypto": [],
        "errors": [],
        "market_open": None,   # None = unknown (clock unavailable)
        "stocks_meta": None,
        "crypto_meta": None,
    }

    # Stock movers reflect the LAST session even when the market is closed, so
    # the UI must be able to say "these aren't live" on a weekend/holiday.
    try:
        clock = await client.clock()
        result["market_open"] = bool(clock.get("is_open"))
    except Exception:
        pass

    if cfg["stocks"]["enabled"]:
        try:
            result["stocks"], result["stocks_meta"] = await scan_stocks(client, cfg)
        except AlpacaError as exc:
            result["errors"].append(f"Stock scan failed ({exc.status_code}): {exc}")
        except Exception as exc:
            result["errors"].append(f"Stock scan failed: {exc}")
    if cfg["crypto"]["enabled"]:
        try:
            result["crypto"], result["crypto_meta"] = await scan_crypto(client, cfg)
        except AlpacaError as exc:
            result["errors"].append(f"Crypto scan failed ({exc.status_code}): {exc}")
        except Exception as exc:
            result["errors"].append(f"Crypto scan failed: {exc}")

    _cache.update(at=now, config=cfg, result=result)
    return result


def invalidate_cache() -> None:
    _cache.update(at=0.0, config=None, result=None)
