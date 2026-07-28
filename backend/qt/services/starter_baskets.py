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

# name -> list of stock tickers (all US equities). These are GICS-style sector
# baskets of ~30 large-caps each, plus a dividend/high-yield theme. They are a
# curated convenience, not an authoritative sector index — memberships drift.
STARTER_BASKETS: dict[str, list[str]] = {
    "Information Technology": [
        "NVDA", "AAPL", "MSFT", "AVGO", "AMD", "CRM", "CSCO", "ACN", "ORCL",
        "INTC", "QCOM", "IBM", "TXN", "NOW", "INTU", "AMAT", "MU", "LRCX",
        "PANW", "ADI", "KLAC", "SNPS", "CDNS", "ROP", "MSI", "APH", "ADSK",
        "NXPI", "TEAM", "PLTR",
    ],
    "Health Care": [
        "LLY", "UNH", "JNJ", "MRK", "ABBV", "TMO", "PFE", "AMGN", "ISRG",
        "BMY", "CVS", "GILD", "MDT", "REGN", "VRTX", "SYK", "ZTS", "CI",
        "BSX", "ELV", "BDX", "HCA", "A", "MCK", "IQV", "HUM", "CNC", "IDXX",
        "EW", "MRNA",
    ],
    "Financials": [
        "BRK.B", "JPM", "V", "MA", "BAC", "WFC", "MS", "GS", "SPGI", "BLK",
        "AXP", "C", "SCHW", "MMC", "PGR", "CB", "AON", "CME", "MCO", "ICE",
        "USB", "PNC", "COF", "AIG", "MET", "TRV", "TROW", "PRU", "ALL", "BK",
    ],
    "Consumer Discretionary": [
        "AMZN", "TSLA", "HD", "MCD", "NKE", "LOW", "SBUX", "BKNG", "TJX",
        "CMG", "F", "GM", "MAR", "ORLY", "AZO", "HLT", "ROST", "LULU", "YUM",
        "TSCO", "LEN", "DHI", "BBY", "GPC", "NVR", "DRI", "PHM", "HAS", "WSM",
        "RCL",
    ],
    "Consumer Staples": [
        "WMT", "PG", "COST", "KO", "PEP", "PM", "MDLZ", "MO", "TGT", "EL",
        "CL", "SYY", "KDP", "STZ", "KR", "GIS", "ADM", "MKC", "HSY", "CHD",
        "CLX", "K", "CAG", "SJM", "TAP", "KMB", "COTY", "TSN", "HRL", "DLTR",
    ],
    "Industrials": [
        "CAT", "GE", "UNP", "HON", "UPS", "RTX", "LMT", "DE", "ADP", "BA",
        "MMM", "FDX", "CSX", "NSC", "NOC", "GD", "WM", "ETN", "EMR", "ROP",
        "PAYX", "CPRT", "FAST", "GWW", "DAL", "UAL", "TXT", "CMI", "TT",
        "CARR",
    ],
    "Communication Services": [
        "GOOGL", "META", "NFLX", "TMUS", "VZ", "T", "DIS", "CMCSA", "CHTR",
        "WBD", "EA", "TTWO", "OMC", "IPG", "LYV", "MTCH", "FOXA", "NWSA",
        "PARA", "NYT", "ZG", "DASH", "SPOT", "SNAP", "PINS", "GOOG", "LBRDK",
        "FWONA", "RBLX",
    ],
    "Energy": [
        "XOM", "CVX", "COP", "EOG", "SLB", "MPC", "PSX", "VLO", "PXD", "OXY",
        "HES", "HAL", "BKR", "DVN", "WMB", "OKE", "KMI", "FANG", "CTRA",
        "MRO", "APA", "EQT", "OVV", "TRGP", "CHRD", "MTDR", "PBF", "AMR",
        "CNX", "RRC",
    ],
    "Utilities": [
        "NEE", "SO", "DUK", "AEP", "SRE", "D", "EXC", "XEL", "ED", "PEG",
        "WEC", "EIX", "AWK", "FE", "AEE", "ETR", "ES", "DTE", "PPL", "CNP",
        "CMS", "ATO", "LNT", "NI", "PNW", "OGE", "SR", "BKH", "IDA", "POR",
    ],
    "Real Estate": [
        "PLD", "AMT", "CCI", "EQIX", "PSA", "O", "SPG", "WELL", "VICI",
        "SBAC", "CBRE", "DLR", "EXR", "AVB", "EQR", "VRE", "ARE", "VNO",
        "BXP", "MAA", "CPT", "UDR", "REG", "FRT", "KIM", "NNN", "ESS", "HST",
        "GLPI", "BRX",
    ],
    "Materials": [
        "LIN", "SHW", "FCX", "APD", "ECL", "NUE", "DOW", "CTVA", "DD", "NEM",
        "ALB", "PPG", "VMC", "MLM", "CE", "FMC", "MOS", "CF", "IP", "WRK",
        "PKG", "AEM", "GOLD", "STLD", "VALE", "SCCO", "CCK", "BALL", "AA",
        "EMN",
    ],
    "High-Yield & Dividend": [
        "MO", "VZ", "PFE", "VICI", "GIS", "KHC", "AMCR", "ARE", "UPS", "CLX",
        "KMB", "O", "ABBV", "HRL", "TROW", "KVUE", "PEP", "ES", "XOM", "MKC",
        "CVX", "MDT", "BEN", "BKH", "T", "KO", "PG", "JNJ", "MCD", "IBM",
    ],
}


def _dedupe(symbols: list[str]) -> list[str]:
    """Uppercase + drop duplicates, preserving first-seen order."""
    seen: set[str] = set()
    out: list[str] = []
    for s in symbols:
        u = s.upper()
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def seed_starter_baskets(session: Session) -> int:
    """Upsert the curated builtin baskets so their membership matches
    STARTER_BASKETS. Idempotent and convergent: a reboot refreshes the shipped
    baskets' members to the canonical lists rather than duplicating them.

    Only *builtin* baskets are touched. User-created (non-builtin) baskets are
    never modified, even if a user made one whose name collides with a shipped
    basket. Builtin baskets that are no longer in STARTER_BASKETS (e.g. an old
    starter set) are left in place — they are not auto-deleted here, so an
    already-running instance keeps its stale rows until the user removes them
    via the UI.

    Returns the number of baskets newly created (0 when all already existed).

    Seeding does NOT require the Alpaca asset directory to be populated — these
    are curated tickers, so a fresh container seeds them before the first asset
    sync. Where the directory *is* populated, membership is annotated at read
    time (see api/baskets.py), not pruned here.
    """
    created = 0
    for name, symbols in STARTER_BASKETS.items():
        basket = (
            session.query(Basket)
            .filter(Basket.name == name, Basket.builtin.is_(True))
            .one_or_none()
        )
        if basket is None:
            basket = Basket(name=name, builtin=True)
            session.add(basket)
            session.flush()  # need basket.id
            created += 1
        else:
            # Refresh membership to the canonical list (overwrite is intended).
            session.query(BasketItem).filter(
                BasketItem.basket_id == basket.id
            ).delete()
            session.flush()
        for symbol in _dedupe(symbols):
            session.add(
                BasketItem(basket_id=basket.id, symbol=symbol, asset_class="stock")
            )
    log.info(
        "seeded/refreshed %d starter baskets (%d newly created)",
        len(STARTER_BASKETS),
        created,
    )
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
