"""Symbol directory: a local mirror of Alpaca's tradable assets, so the UI
can autocomplete on ticker OR company name without spending an API call per
keystroke (and while Alpaca is unreachable).

The list changes slowly — new listings and delistings — so a daily refresh
is plenty.
"""

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from qt.broker.alpaca import AlpacaClient
from qt.models import Asset, utcnow

log = logging.getLogger("qt.assets")

STALE_AFTER = timedelta(hours=24)

# Alpaca's asset_class values → ours
CLASS_MAP = {"us_equity": "stock", "crypto": "crypto"}


async def sync(session: Session, client: AlpacaClient) -> dict:
    """Refresh the local asset directory. Returns per-class counts."""
    counts: dict[str, int] = {}
    for alpaca_class, our_class in CLASS_MAP.items():
        rows = await client.list_assets(alpaca_class)
        seen: set[str] = set()
        existing = {
            a.symbol: a
            for a in session.query(Asset).filter(Asset.asset_class == our_class).all()
        }
        for row in rows:
            symbol = row["symbol"]
            seen.add(symbol)
            name = row.get("name") or ""
            exchange = row.get("exchange") or ""
            fractionable = bool(row.get("fractionable"))
            asset = existing.get(symbol)
            if asset:
                asset.name = name
                asset.exchange = exchange
                asset.fractionable = fractionable
                asset.updated_at = utcnow()
            else:
                session.add(
                    Asset(
                        symbol=symbol, asset_class=our_class, name=name,
                        exchange=exchange, fractionable=fractionable,
                    )
                )
        # drop anything Alpaca no longer lists as tradable (delistings)
        for symbol, asset in existing.items():
            if symbol not in seen:
                session.delete(asset)
        counts[our_class] = len(rows)
    log.info("asset directory synced: %s", counts)
    return counts


def status(session: Session) -> dict:
    total = session.query(func.count(Asset.symbol)).scalar() or 0
    newest = session.query(func.max(Asset.updated_at)).scalar()
    if newest is not None and newest.tzinfo is None:
        newest = newest.replace(tzinfo=timezone.utc)
    return {
        "count": int(total),
        "stocks": int(
            session.query(func.count(Asset.symbol)).filter(Asset.asset_class == "stock").scalar() or 0
        ),
        "crypto": int(
            session.query(func.count(Asset.symbol)).filter(Asset.asset_class == "crypto").scalar() or 0
        ),
        "updated_at": newest.isoformat() if newest else None,
        "stale": newest is None or (datetime.now(timezone.utc) - newest) > STALE_AFTER,
    }


def search(session: Session, query: str, asset_class: str | None = None, limit: int = 20) -> list[dict]:
    """Match on ticker OR company name. Exact ticker first, then ticker
    prefix, then name matches — so typing 'nvda' doesn't bury NVDA under
    leveraged ETFs with 'NVDA' in their name."""
    q = (query or "").strip()
    if not q:
        return []
    upper = q.upper()
    like = f"%{q}%"

    rows_q = session.query(Asset)
    if asset_class:
        rows_q = rows_q.filter(Asset.asset_class == asset_class)
    rows = (
        rows_q.filter(
            or_(Asset.symbol.ilike(f"%{q}%"), Asset.name.ilike(like))
        )
        .limit(200)
        .all()
    )

    def rank(a: Asset) -> tuple:
        symbol = a.symbol.upper()
        name = (a.name or "").upper()
        if symbol == upper:
            tier = 0
        elif symbol.startswith(upper):
            tier = 1
        elif name.startswith(upper):
            tier = 2
        elif upper in symbol:
            tier = 3
        else:
            tier = 4
        return (tier, len(symbol), symbol)

    return [
        {
            "symbol": a.symbol,
            "name": a.name,
            "asset_class": a.asset_class,
            "exchange": a.exchange,
            "fractionable": a.fractionable,
        }
        for a in sorted(rows, key=rank)[:limit]
    ]
