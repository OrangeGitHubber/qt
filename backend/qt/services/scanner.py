"""Market scanner: find today's rising stocks and crypto, filtered by
UI-configurable rules. Results are cached briefly to respect Alpaca's
rate limits no matter how often the UI polls."""

import time
from typing import Any

from sqlalchemy.orm import Session

from qt.broker.alpaca import AlpacaClient, AlpacaError
from qt.settings_service import get_setting

CONFIG_KEY = "scanner_config"

DEFAULT_CONFIG: dict[str, Any] = {
    "stocks_enabled": True,
    "crypto_enabled": True,
    "top_n": 10,                      # rows shown per asset class
    "min_price": 1.0,                 # skip sub-$1 (penny/OTC junk)
    "max_price": 0,                   # 0 = no cap
    "min_change_pct": 2.0,            # only "improving" symbols
    "min_dollar_volume": 5_000_000,   # daily $ volume floor (liquidity)
    "exclude_symbols": [],
}

_CACHE_TTL_SECONDS = 30
_cache: dict[str, Any] = {"at": 0.0, "config": None, "result": None}


def get_config(session: Session) -> dict[str, Any]:
    stored = get_setting(session, CONFIG_KEY) or {}
    return {**DEFAULT_CONFIG, **stored}


def _passes(cfg: dict, price: float, change_pct: float, dollar_volume: float, symbol: str) -> bool:
    if symbol.upper() in (s.upper() for s in cfg["exclude_symbols"]):
        return False
    if price < cfg["min_price"]:
        return False
    if cfg["max_price"] and price > cfg["max_price"]:
        return False
    if change_pct < cfg["min_change_pct"]:
        return False
    if dollar_volume < cfg["min_dollar_volume"]:
        return False
    return True


def _daily_dollar_volume(snapshot: dict) -> float:
    bar = snapshot.get("dailyBar") or {}
    volume = bar.get("v") or 0
    ref_price = bar.get("vw") or bar.get("c") or 0
    return float(volume) * float(ref_price)


def _change_pct(snapshot: dict) -> float | None:
    daily = (snapshot.get("dailyBar") or {}).get("c")
    prev = (snapshot.get("prevDailyBar") or {}).get("c")
    if not daily or not prev:
        return None
    return (float(daily) - float(prev)) / float(prev) * 100


def _meta(scanned: int, best: tuple[str, float] | None) -> dict[str, Any]:
    """Diagnostics so an empty panel can explain itself: how many symbols had
    usable data, and the strongest mover seen (before filtering)."""
    return {
        "scanned": scanned,
        "best_symbol": best[0] if best else None,
        "best_change_pct": round(best[1], 2) if best else None,
    }


async def scan_stocks(client: AlpacaClient, cfg: dict) -> tuple[list[dict], dict]:
    movers = await client.stock_movers(top=50)
    gainers = movers.get("gainers", [])
    symbols = [g["symbol"] for g in gainers]
    snapshots = await client.stock_snapshots(symbols)

    rows = []
    best: tuple[str, float] | None = None
    for gainer in gainers:
        symbol = gainer["symbol"]
        snapshot = snapshots.get(symbol) or {}
        price = float(gainer.get("price") or 0)
        change_pct = float(gainer.get("percent_change") or 0)
        dollar_volume = _daily_dollar_volume(snapshot)
        if best is None or change_pct > best[1]:
            best = (symbol, change_pct)
        if _passes(cfg, price, change_pct, dollar_volume, symbol):
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
    assets = await client.crypto_assets()
    usd_pairs = [a["symbol"] for a in assets if a["symbol"].endswith("/USD")]
    snapshots = await client.crypto_snapshots(usd_pairs)

    rows = []
    scanned = 0
    best: tuple[str, float] | None = None
    for symbol, snapshot in snapshots.items():
        change_pct = _change_pct(snapshot)
        if change_pct is None:
            continue
        scanned += 1
        price = float((snapshot.get("dailyBar") or {}).get("c") or 0)
        dollar_volume = _daily_dollar_volume(snapshot)
        if best is None or change_pct > best[1]:
            best = (symbol, change_pct)
        if _passes(cfg, price, change_pct, dollar_volume, symbol):
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

    if cfg["stocks_enabled"]:
        try:
            result["stocks"], result["stocks_meta"] = await scan_stocks(client, cfg)
        except AlpacaError as exc:
            result["errors"].append(f"Stock scan failed ({exc.status_code}): {exc}")
        except Exception as exc:
            result["errors"].append(f"Stock scan failed: {exc}")
    if cfg["crypto_enabled"]:
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
