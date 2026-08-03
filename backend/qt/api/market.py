"""Scanner, watchlist, and chart-data endpoints."""

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from qt.broker.alpaca import AlpacaClient, AlpacaError
from qt.broker.factory import get_client
from qt.db import get_session
from qt.models import Asset, AuditLog, WatchlistItem
from qt.services import scanner, stats
from qt.settings_service import set_setting
from qt.timeutil import iso_utc

router = APIRouter(prefix="/api", tags=["market"])

# Daily bars per symbol, valid for one UTC day (see _daily_bars_cached).
_daily_cache: dict = {"day": None, "bars": {}}


def require_client(session: Session = Depends(get_session)) -> AlpacaClient:
    client = get_client(session)
    if client is None:
        raise HTTPException(status_code=409, detail="Alpaca is not configured yet — finish setup first.")
    return client


# ---- Scanner ----


class ClassFilters(BaseModel):
    enabled: bool = True
    min_price: float = Field(default=0.0, ge=0)
    max_price: float = Field(default=0.0, ge=0)
    min_change_pct: float = Field(default=1.0, ge=0, le=100)
    min_dollar_volume: float = Field(default=1_000_000, ge=0)


class ScannerConfig(BaseModel):
    top_n: int = Field(default=10, ge=1, le=50)
    exclude_symbols: list[str] = []
    # Per-asset-class floors (see scanner.py): stocks and crypto want different
    # price/volume thresholds. Defaults mirror scanner.STOCK/CRYPTO_DEFAULTS.
    stocks: ClassFilters = ClassFilters(min_price=1.0, min_change_pct=2.0, min_dollar_volume=5_000_000)
    crypto: ClassFilters = ClassFilters(min_price=0.0, min_change_pct=1.0, min_dollar_volume=1_000_000)


@router.get("/scanner")
async def scanner_results(
    session: Session = Depends(get_session), client: AlpacaClient = Depends(require_client)
) -> dict:
    return await scanner.scan(session, client)


@router.get("/scanner/config")
def scanner_config(session: Session = Depends(get_session)) -> dict:
    return scanner.get_config(session)


@router.put("/scanner/config")
def update_scanner_config(cfg: ScannerConfig, session: Session = Depends(get_session)) -> dict:
    cleaned = cfg.model_dump()
    cleaned["exclude_symbols"] = sorted({s.strip().upper() for s in cleaned["exclude_symbols"] if s.strip()})
    set_setting(session, scanner.CONFIG_KEY, cleaned)
    session.add(AuditLog(category="config", message="Scanner configuration updated", detail=str(cleaned)))
    scanner.invalidate_cache()
    return cleaned


# ---- Watchlist ----


class WatchlistAdd(BaseModel):
    symbol: str = Field(min_length=1, max_length=32)
    asset_class: str = Field(pattern="^(stock|crypto)$")


async def _snapshot_for(client: AlpacaClient, symbol: str, asset_class: str) -> dict | None:
    if asset_class == "stock":
        snapshots = await client.stock_snapshots([symbol])
    else:
        snapshots = await client.crypto_snapshots([symbol])
    return snapshots.get(symbol)


async def _daily_bars_cached(
    client: AlpacaClient, symbols: list[str], asset_class: str
) -> dict[str, list[dict]]:
    """~400 calendar days of daily bars per symbol, cached for the UTC day.

    Daily history only changes once a day, so this is one API call per asset
    class per day no matter how often the watchlist polls.
    """
    if not symbols:
        return {}
    today = stats.utc_day()
    if _daily_cache["day"] != today:
        _daily_cache.update(day=today, bars={})

    missing = [s for s in symbols if s not in _daily_cache["bars"]]
    if missing:
        start = (datetime.now(timezone.utc) - timedelta(days=400)).strftime("%Y-%m-%dT%H:%M:%SZ")
        fetched = await client.historical_bars(missing, asset_class, "1Day", start)
        for symbol in missing:
            _daily_cache["bars"][symbol] = fetched.get(symbol) or []
    return {s: _daily_cache["bars"].get(s, []) for s in symbols}


@router.get("/watchlist")
async def watchlist(
    session: Session = Depends(get_session), client: AlpacaClient = Depends(require_client)
) -> dict:
    items = session.query(WatchlistItem).order_by(WatchlistItem.added_at).all()
    stock_symbols = [i.symbol for i in items if i.asset_class == "stock"]
    crypto_symbols = [i.symbol for i in items if i.asset_class == "crypto"]

    quotes: dict[str, dict] = {}
    errors: list[str] = []
    try:
        if stock_symbols:
            quotes.update(await client.stock_snapshots(stock_symbols))
        if crypto_symbols:
            quotes.update(await client.crypto_snapshots(crypto_symbols))
    except AlpacaError as exc:
        errors.append(f"Quote fetch failed ({exc.status_code}): {exc}")

    # Longer-horizon stats are a bonus: never let them break the price view.
    daily: dict[str, list[dict]] = {}
    try:
        daily.update(await _daily_bars_cached(client, stock_symbols, "stock"))
        daily.update(await _daily_bars_cached(client, crypto_symbols, "crypto"))
    except AlpacaError as exc:
        errors.append(f"History fetch failed ({exc.status_code}): {exc} — 30d/ATR/MA columns unavailable")

    # Crypto "today" = rolling 24h change, matching the scanner and the engine
    # (one definition everywhere; see scanner.rolling_24h). Best-effort too.
    crypto_stats: dict[str, tuple[float, float, float]] = {}
    try:
        crypto_stats = await scanner.crypto_rolling_stats(client, crypto_symbols)
    except AlpacaError as exc:
        errors.append(f"Crypto 24h fetch failed ({exc.status_code}): {exc}")

    rows = []
    for item in items:
        snapshot = quotes.get(item.symbol) or {}
        daily_bar = snapshot.get("dailyBar") or {}
        prev = snapshot.get("prevDailyBar") or {}
        price = daily_bar.get("c")
        change_pct = None
        if item.asset_class == "crypto":
            stats24 = crypto_stats.get(item.symbol)
            change_pct = round(stats24[1], 2) if stats24 else None
        elif daily_bar.get("c") and prev.get("c"):
            change_pct = round((daily_bar["c"] - prev["c"]) / prev["c"] * 100, 2)
        row = {
            "symbol": item.symbol,
            "asset_class": item.asset_class,
            "price": price,
            "change_pct": change_pct,
            "added_at": iso_utc(item.added_at),
        }
        row.update(stats.compute(daily.get(item.symbol) or [], current_price=price))
        rows.append(row)
    return {"items": rows, "errors": errors}


@router.get("/market/history")
async def history(
    symbol: str,
    asset_class: str = "stock",
    years: float = 10,
    client: AlpacaClient = Depends(require_client),
) -> dict:
    """Daily price history for the detail chart — as far back as the data
    plan allows (Alpaca's free stock history starts ~2016)."""
    symbol = symbol.upper()
    start = (datetime.now(timezone.utc) - timedelta(days=int(years * 365))).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        data = await client.historical_bars([symbol], asset_class, "1Day", start)
    except AlpacaError as exc:
        raise HTTPException(status_code=502, detail=f"History fetch failed ({exc.status_code}): {exc}")
    series = data.get(symbol) or []
    if not series:
        raise HTTPException(status_code=404, detail=f"No daily history available for {symbol}.")
    return {
        "symbol": symbol,
        "asset_class": asset_class,
        # High/low/volume ride along so the detail chart can draw volatility
        # overlays (ATR) and a volume sub-panel without a second round-trip. They
        # may be missing on a sparse bar, hence the defensive .get.
        "bars": [
            {
                "t": b["t"],
                "c": float(b["c"]),
                "h": float(b["h"]) if b.get("h") is not None else None,
                "l": float(b["l"]) if b.get("l") is not None else None,
                "v": float(b["v"]) if b.get("v") is not None else None,
            }
            for b in series
        ],
        "stats": stats.compute(series),
    }


@router.post("/watchlist")
async def add_to_watchlist(
    body: WatchlistAdd,
    session: Session = Depends(get_session),
    client: AlpacaClient = Depends(require_client),
) -> dict:
    symbol = body.symbol.strip().upper()
    if session.get(WatchlistItem, (symbol, body.asset_class)):
        raise HTTPException(status_code=409, detail=f"{symbol} is already on the watchlist.")

    # The local symbol directory is authoritative when it knows the symbol —
    # no API round-trip, and adding still works if market data is hiccuping.
    # Fall back to a live quote check for symbols it hasn't heard of.
    if session.get(Asset, (symbol, body.asset_class)) is None:
        try:
            snapshot = await _snapshot_for(client, symbol, body.asset_class)
        except AlpacaError as exc:
            raise HTTPException(status_code=502, detail=f"Could not verify symbol ({exc.status_code}): {exc}")
        if not snapshot:
            hint = "use BTC/USD format" if body.asset_class == "crypto" else "check the ticker"
            raise HTTPException(status_code=404, detail=f"Alpaca has no data for '{symbol}' — {hint}.")

    session.add(WatchlistItem(symbol=symbol, asset_class=body.asset_class))
    session.add(AuditLog(category="watchlist", message=f"Added {symbol} ({body.asset_class})"))
    return {"ok": True, "symbol": symbol}


@router.delete("/watchlist/{asset_class}/{symbol:path}")
def remove_from_watchlist(asset_class: str, symbol: str, session: Session = Depends(get_session)) -> dict:
    item = session.get(WatchlistItem, (symbol.upper(), asset_class))
    if not item:
        raise HTTPException(status_code=404, detail="Not on the watchlist.")
    session.delete(item)
    session.add(AuditLog(category="watchlist", message=f"Removed {symbol.upper()} ({asset_class})"))
    return {"ok": True}


# ---- Bars (mini-charts) ----


@router.get("/market/bars")
async def bars(
    symbol: str,
    asset_class: str = "stock",
    client: AlpacaClient = Depends(require_client),
) -> dict:
    """Recent trend for the watchlist sparkline: the last ~30 DAILY closes.

    Daily bars exist for every tradable symbol and don't depend on the thin
    free intraday (IEX) feed, which is empty off-hours and sparse for many
    stocks — that gap is what made the old 15-min sparkline show "no data".
    We over-fetch ~50 calendar days (weekends/holidays have no bars) and keep
    the last 30 sessions."""
    symbol = symbol.upper()
    start = (datetime.now(timezone.utc) - timedelta(days=50)).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        data = await client.historical_bars([symbol], asset_class, "1Day", start)
    except AlpacaError as exc:
        raise HTTPException(status_code=502, detail=f"Bar fetch failed ({exc.status_code}): {exc}")
    # historical_bars returns oldest-first; keep the last 30 sessions.
    series = (data.get(symbol) or [])[-30:]
    return {"symbol": symbol, "bars": [{"t": b["t"], "c": float(b["c"])} for b in series]}
