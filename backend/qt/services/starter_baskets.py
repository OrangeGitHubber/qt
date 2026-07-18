"""Curated starter baskets — hand-picked, real, liquid US large-caps grouped
by theme, plus a Sector-ETFs basket.

HONESTY: these are *curated symbol lists*, not an authoritative sector
classification. Alpaca ships no sector/industry data and no fundamental
screener on this data plan, so "sectors" here are lists we curate and the user
edits — never a database of record. Memberships drift as companies change.

Every symbol below is a real, well-known, liquid US-listed ticker verified by
hand. When in doubt about a symbol, it was dropped rather than guessed.
"""

import logging

from sqlalchemy.orm import Session

from qt.models import Asset, Basket, BasketItem

log = logging.getLogger("qt.baskets")

# name -> list of stock tickers (all US equities; the ETF basket is stocks too)
STARTER_BASKETS: dict[str, list[str]] = {
    "Defense": ["LMT", "RTX", "NOC", "GD", "BA", "LHX", "HII"],
    "Banking": ["JPM", "BAC", "WFC", "C", "GS", "MS", "USB"],
    "Gold & Mining": ["NEM", "GOLD", "FCX", "AEM", "SCCO"],
    "REITs / Property": ["O", "PLD", "AMT", "SPG", "EQIX", "VICI"],
    "Big Tech": ["AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA"],
    "Semiconductors": ["NVDA", "AMD", "INTC", "AVGO", "MU", "QCOM", "TXN"],
    "Energy": ["XOM", "CVX", "COP", "SLB", "EOG"],
    "Healthcare": ["JNJ", "UNH", "PFE", "MRK", "ABBV", "LLY"],
    "Sector ETFs": ["XLK", "XLF", "XLE", "XLV", "XLI", "ITA", "GDX", "VNQ", "SMH"],
}


def seed_starter_baskets(session: Session) -> int:
    """Create the curated baskets IF none exist yet. Idempotent: a reboot never
    duplicates them, and a user who deleted every basket is not re-seeded (the
    guard is "are there ANY baskets", deliberately, so we don't fight edits).

    Returns the number of baskets created (0 on a no-op reseed).

    Seeding does NOT require the Alpaca asset directory to be populated — these
    are hand-verified tickers, so a fresh container seeds them before the first
    asset sync. Where the directory *is* populated, membership is annotated at
    read time (see api/baskets.py), not pruned here.
    """
    if session.query(Basket).count() > 0:
        return 0
    created = 0
    for name, symbols in STARTER_BASKETS.items():
        basket = Basket(name=name, builtin=True)
        session.add(basket)
        session.flush()  # need basket.id
        for symbol in symbols:
            session.add(
                BasketItem(basket_id=basket.id, symbol=symbol, asset_class="stock")
            )
        created += 1
    log.info("seeded %d starter baskets", created)
    return created


def annotate_membership(session: Session, items: list[BasketItem]) -> list[dict]:
    """Turn basket items into dicts, flagging any whose symbol is absent from
    the local Alpaca asset directory. When the directory is empty (pre-sync),
    nothing is flagged — we can't validate against data we don't have."""
    directory_has_any = session.query(Asset.symbol).first() is not None
    known: set[tuple[str, str]] = set()
    if directory_has_any:
        known = {
            (a.symbol, a.asset_class)
            for a in session.query(Asset.symbol, Asset.asset_class).all()
        }
    out = []
    for it in items:
        in_directory = (
            (it.symbol, it.asset_class) in known if directory_has_any else True
        )
        out.append(
            {
                "symbol": it.symbol,
                "asset_class": it.asset_class,
                "in_directory": in_directory,
            }
        )
    return out
