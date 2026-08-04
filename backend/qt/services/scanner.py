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
    # Alpaca's crypto daily $-volumes are thin — even the busiest pairs run
    # ~$70k–$400k/day, not millions. A $1M floor (the old default) rejected
    # almost every day and left scanner replay with a handful of mover-days.
    # $25k keeps a real liquidity guard while matching this feed's reality.
    "min_dollar_volume": 25_000,
}
DEFAULT_CONFIG: dict[str, Any] = {
    "top_n": 10,                      # rows shown per asset class
    "exclude_symbols": [],
    "stocks": dict(STOCK_DEFAULTS),
    "crypto": dict(CRYPTO_DEFAULTS),
}

# Stablecoins are pegged to $1, so they CANNOT produce a momentum move: the
# ±0.05% a scanner sees is peg noise, not a trend. Left in, any strategy with a
# low enough gain gate buys a dollar with a dollar — spending sleeve budget and a
# position slot on a thing that by construction goes nowhere. This is a fixed,
# well-known set rather than a judgement call, so it's built in rather than left
# to the user's exclude list.
#
# Membership rule, so extending this stays obvious: the token's whole purpose is
# to hold ONE US DOLLAR. Pegged to something that itself moves does NOT belong
# here — PAXG and XAUT track the gold price and swing several percent a day, so
# they are real momentum candidates and must keep passing. Nor do euro-pegged
# coins (EURC, EURT): quoted in USD they move with EUR/USD, which is a real,
# tradable move. Nor do the governance tokens of stablecoin projects (FXS, MKR,
# ENA) — those float freely.
#
# Beware the near-misses: USDP (Pax Dollar) is a stablecoin, PAXG (Paxos Gold)
# is not, and they differ by one letter.
STABLECOIN_BASES: frozenset[str] = frozenset(
    {
        "BUSD",    # Binance USD
        "DAI",     # MakerDAO Dai
        "FDUSD",   # First Digital USD
        "FRAX",    # Frax USD
        "GUSD",    # Gemini Dollar
        "LUSD",    # Liquity USD
        "PYUSD",   # PayPal USD
        "RLUSD",   # Ripple USD
        "TUSD",    # TrueUSD
        "USDC",    # Circle USD Coin
        "USDD",    # Tron USDD
        "USDE",    # Ethena USDe
        "USDG",    # Global Dollar
        "USDP",    # Pax Dollar (NOT PAXG — see above)
        "USDS",    # Sky Dollar
        "USDT",    # Tether
    }
)
STABLECOIN_REASON = "pegged to $1 (a stablecoin can't trend)"

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


def is_stablecoin(symbol: str) -> bool:
    """True when this is a CRYPTO PAIR whose base asset is a $1 stablecoin.

    Matches the base only — "USDC" of "USDC/USD" — never the quote, or every USD
    pair on the venue would qualify. Requires the pair shape, so a stock ticker
    can never be caught by this list even if one day it collides with a coin
    name."""
    base, sep, _quote = symbol.upper().partition("/")
    return bool(sep) and base in STABLECOIN_BASES


# The quote legs Alpaca prices crypto in, longest first so "BTCUSDT" strips
# "USDT" rather than stopping at "USDC"/"USD". Only used to read a base off the
# SLASH-LESS spelling — see is_stablecoin_pair.
_CRYPTO_QUOTES = ("USDT", "USDC", "USD", "BTC")


def is_stablecoin_pair(symbol: str) -> bool:
    """`is_stablecoin` for a symbol that may be spelled EITHER way.

    The live scanner sees Alpaca's slashed form ("USDC/USD"); the bar cache
    stores whatever the bars endpoint returned, which for crypto is slash-less
    ("USDCUSD"). `is_stablecoin` deliberately requires the slash so a stock
    ticker can never be caught by the list — which meant the cache's own
    spelling never matched, and the movers reconstruction (i.e. every replay and
    every optimizer run) went on trading USDC and USDT after the live engine
    stopped.

    ONLY EVER CALLED ON A SYMBOL ALREADY KNOWN TO BE CRYPTO — the callers select
    the crypto mover tables — so stripping a quote leg here cannot reach a stock
    ticker, and the safety `is_stablecoin` gets from the slash is preserved by
    the caller instead of by the string."""
    s = symbol.strip().upper()
    if "/" in s:
        return is_stablecoin(s)
    for quote in _CRYPTO_QUOTES:
        if s.endswith(quote) and len(s) > len(quote):
            return s[: -len(quote)] in STABLECOIN_BASES
    return s in STABLECOIN_BASES


def _reject_reason(
    f: dict, exclude_symbols: list, price: float, change_pct: float, dollar_volume: float, symbol: str
) -> str | None:
    """Which filter rejected this symbol, or None if it passed.

    A reason rather than a bool because the panel needs to explain a SHORT list,
    not just an empty one. Three rows with no explanation reads as "the scanner
    is broken"; "28 below your $0.50 min price" reads as a setting you chose.
    Phrased as the user's own setting — every one of these is a number they
    typed.

    Not every reason is a setting: a stablecoin is refused by construction. It
    still gets its OWN line in the tally rather than being folded into the
    exclude list or (as it would otherwise be) counted under the min-gain floor,
    so a name that vanishes can be explained.
    """
    if symbol.upper() in (s.upper() for s in exclude_symbols):
        return "on your exclude list"
    # Before the numeric floors: a stablecoin fails min gain too, and being told
    # "below your 1% min gain" would hide the real, permanent reason.
    if is_stablecoin(symbol):
        return STABLECOIN_REASON
    if price < f["min_price"]:
        return f"below your ${f['min_price']:g} min price"
    if f["max_price"] and price > f["max_price"]:
        return f"above your ${f['max_price']:g} max price"
    if change_pct < f["min_change_pct"]:
        return f"below your {f['min_change_pct']:g}% min gain"
    if dollar_volume < f["min_dollar_volume"]:
        return f"below your ${f['min_dollar_volume']:,.0f} min $ volume"
    return None


def _passes(f: dict, exclude_symbols: list, price: float, change_pct: float, dollar_volume: float, symbol: str) -> bool:
    return _reject_reason(f, exclude_symbols, price, change_pct, dollar_volume, symbol) is None


def _bar_dollar_volume(bar: dict) -> float:
    volume = bar.get("v") or 0
    ref_price = bar.get("vw") or bar.get("c") or 0
    return float(volume) * float(ref_price)


def _stock_session_volume(snapshot: dict, market_open: bool) -> float:
    """Stock liquidity floor = the last COMPLETED session's dollar volume.

    While the market is open, today's dailyBar is only partial (it grows all
    day), which makes a fixed floor bite harder in the morning than the
    afternoon for no real reason — so use the previous full session. When the
    market is closed, dailyBar already IS the last completed session (e.g.
    Friday on a weekend), so use it. If the preferred bar carries no volume
    (missing, a data gap, or a prevDailyBar that only holds a reference price),
    fall back to the other rather than reading zero and wrongly filtering the
    symbol out.
    """
    daily = snapshot.get("dailyBar") or {}
    prev = snapshot.get("prevDailyBar") or {}
    primary, secondary = (prev, daily) if market_open else (daily, prev)
    vol = _bar_dollar_volume(primary)
    return vol if vol > 0 else _bar_dollar_volume(secondary)


def rolling_24h(bars: list[dict]) -> tuple[float, float, float] | None:
    """(price, % change, $ volume) over the trailing ~24h from hourly bars.

    Crypto has no daily close, so we use a ROLLING 24-hour window rather than
    the 00:00-UTC calendar bar. That removes the timezone boundary entirely
    (matches how crypto sites quote "24h change") and, crucially, means the
    scanner isn't blind to crypto for the first hours of each UTC day while a
    fresh calendar bar slowly accumulates volume.

    This is THE definition of a crypto "day gain" everywhere in QT — scanner,
    engine candidates, ranking, watchlist — so the number a user calibrates a
    strategy against is the number the engine actually trades on.
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


def _meta(
    scanned: int,
    best: tuple[str, float, float, float] | None,
    passed: int = 0,
    rejected: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Diagnostics so the panel can explain itself: how many symbols had usable
    data, the strongest mover seen (before filtering) with its price and $
    volume — so the UI can name the exact floor that stopped it — and how many
    passed the filters.

    `passed` matters when the list is FULL, not empty. The rows are cut to
    top_n, so a symbol can clear every filter and still not be on screen; without
    this the only honest answer to "why isn't my mover here?" was to go and read
    the code."""
    return {
        "scanned": scanned,
        "passed": passed,
        # {reason: how many}. Answers "why isn't my mover here?" for ANY symbol,
        # which is otherwise only answerable by reading the filter values back
        # against each price by hand.
        "rejected": dict(sorted((rejected or {}).items(), key=lambda kv: -kv[1])),
        "best_symbol": best[0] if best else None,
        "best_change_pct": round(best[1], 2) if best else None,
        "best_price": round(best[2], 4) if best else None,
        "best_dollar_volume": round(best[3]) if best else None,
    }


async def scan_stocks(client: AlpacaClient, cfg: dict, market_open: bool) -> tuple[list[dict], dict]:
    f = cfg["stocks"]
    movers = await client.stock_movers(top=50)
    gainers = movers.get("gainers", [])
    symbols = [g["symbol"] for g in gainers]
    snapshots = await client.stock_snapshots(symbols)

    rejected: dict[str, int] = {}
    rows = []
    best: tuple[str, float, float, float] | None = None
    for gainer in gainers:
        symbol = gainer["symbol"]
        snapshot = snapshots.get(symbol) or {}
        price = float(gainer.get("price") or 0)
        change_pct = float(gainer.get("percent_change") or 0)
        dollar_volume = _stock_session_volume(snapshot, market_open)
        if best is None or change_pct > best[1]:
            best = (symbol, change_pct, price, dollar_volume)
        reason = _reject_reason(f, cfg["exclude_symbols"], price, change_pct, dollar_volume, symbol)
        if reason is not None:
            rejected[reason] = rejected.get(reason, 0) + 1
        else:
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
    return rows[: cfg["top_n"]], _meta(len(gainers), best, passed=len(rows), rejected=rejected)


async def crypto_rolling_stats(
    client: AlpacaClient, symbols: list[str]
) -> dict[str, tuple[float, float, float]]:
    """{symbol: (price, 24h % change, 24h $ volume)} for each pair with data —
    ONE batched fetch of hourly bars over the trailing ~25h.

    IMPORTANT: uses the time-windowed, paginated historical endpoint — NOT
    crypto_bars(limit=N). Alpaca's `limit` on the multi-symbol bars endpoint is
    a TOTAL cap across all symbols, so a small limit gets consumed by the first
    symbol or two and every other pair comes back empty (that bug read as
    "scanned 2 symbols" with ~$0 volume). `start` = ~25h ago (24h + the current
    partial hour) gives each pair its full window."""
    if not symbols:
        return {}
    start_iso = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
    bars_by_symbol = await client.historical_bars(symbols, "crypto", "1Hour", start_iso)
    out: dict[str, tuple[float, float, float]] = {}
    for symbol in symbols:
        stats = rolling_24h(bars_by_symbol.get(symbol) or [])
        if stats is not None:
            out[symbol] = stats
    return out


async def scan_crypto(client: AlpacaClient, cfg: dict) -> tuple[list[dict], dict]:
    f = cfg["crypto"]
    assets = await client.crypto_assets()
    usd_pairs = [a["symbol"] for a in assets if a["symbol"].endswith("/USD")]
    stats_by_symbol = await crypto_rolling_stats(client, usd_pairs)

    rows = []
    scanned = 0
    rejected: dict[str, int] = {}
    best: tuple[str, float, float, float] | None = None
    for symbol in usd_pairs:
        stats = stats_by_symbol.get(symbol)
        if stats is None:
            continue
        scanned += 1
        price, change_pct, dollar_volume = stats
        if best is None or change_pct > best[1]:
            best = (symbol, change_pct, price, dollar_volume)
        reason = _reject_reason(f, cfg["exclude_symbols"], price, change_pct, dollar_volume, symbol)
        if reason is not None:
            rejected[reason] = rejected.get(reason, 0) + 1
        else:
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
    return rows[: cfg["top_n"]], _meta(scanned, best, passed=len(rows), rejected=rejected)


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
            result["stocks"], result["stocks_meta"] = await scan_stocks(client, cfg, bool(result["market_open"]))
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
