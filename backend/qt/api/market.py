"""Scanner, watchlist, and chart-data endpoints."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from qt.broker.alpaca import AlpacaClient, AlpacaError
from qt.broker.factory import get_client
from qt.db import get_session
from qt.models import Asset, AuditLog, WatchlistItem
from qt.services import scanner
from qt.settings_service import set_setting

router = APIRouter(prefix="/api", tags=["market"])


def require_client(session: Session = Depends(get_session)) -> AlpacaClient:
    client = get_client(session)
    if client is None:
        raise HTTPException(status_code=409, detail="Alpaca is not configured yet — finish setup first.")
    return client


# ---- Scanner ----


class ScannerConfig(BaseModel):
    stocks_enabled: bool = True
    crypto_enabled: bool = True
    top_n: int = Field(default=10, ge=1, le=50)
    min_price: float = Field(default=1.0, ge=0)
    max_price: float = Field(default=0, ge=0)
    min_change_pct: float = Field(default=2.0, ge=0, le=100)
    min_dollar_volume: float = Field(default=5_000_000, ge=0)
    exclude_symbols: list[str] = []


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

    rows = []
    for item in items:
        snapshot = quotes.get(item.symbol) or {}
        daily = snapshot.get("dailyBar") or {}
        prev = snapshot.get("prevDailyBar") or {}
        price = daily.get("c")
        change_pct = None
        if daily.get("c") and prev.get("c"):
            change_pct = round((daily["c"] - prev["c"]) / prev["c"] * 100, 2)
        rows.append(
            {
                "symbol": item.symbol,
                "asset_class": item.asset_class,
                "price": price,
                "change_pct": change_pct,
                "added_at": item.added_at.isoformat(),
            }
        )
    return {"items": rows, "errors": errors}


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
    symbol = symbol.upper()
    try:
        if asset_class == "crypto":
            data = await client.crypto_bars([symbol])
        else:
            data = await client.stock_bars([symbol])
    except AlpacaError as exc:
        raise HTTPException(status_code=502, detail=f"Bar fetch failed ({exc.status_code}): {exc}")
    # Bars arrive newest-first (sort=desc); flip for charting.
    series = list(reversed(data.get(symbol, [])))
    return {"symbol": symbol, "bars": [{"t": b["t"], "c": b["c"]} for b in series]}
