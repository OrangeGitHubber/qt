"""Thin Alpaca REST client (trading + market data).

Deliberately a small httpx wrapper rather than the full alpaca-py SDK:
keeping the surface tiny makes reliability and testing easier. Streaming
market data in a later phase can still adopt alpaca-py if useful.
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import httpx

log = logging.getLogger("qt.broker.alpaca")

PAPER_BASE_URL = "https://paper-api.alpaca.markets"
LIVE_BASE_URL = "https://api.alpaca.markets"
DATA_BASE_URL = "https://data.alpaca.markets"

SECRET_KEY_ID = "alpaca_paper_key_id"
SECRET_KEY_SECRET = "alpaca_paper_key_secret"

# Free-tier accounts get the IEX feed; SIP requires a paid data plan.
STOCK_FEED = "iex"

# Retry budget for READS. Alpaca's free tier allows 200 requests/minute, and a
# long comparison backtest — two strategies, hundreds of days, paginated — can
# brush against it. A 429 there is not a failure, it's "wait a moment", but
# without a retry it killed a job that had already run for minutes.
MAX_READ_ATTEMPTS = 4
RETRY_STATUSES = (429, 500, 502, 503, 504)
BACKOFF_SECONDS = (1.0, 3.0, 8.0)  # used when the response carries no Retry-After


class AlpacaError(Exception):
    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        super().__init__(message)


@dataclass
class AlpacaClient:
    key_id: str
    key_secret: str
    base_url: str = PAPER_BASE_URL

    def _headers(self) -> dict[str, str]:
        return {
            "APCA-API-KEY-ID": self.key_id,
            "APCA-API-SECRET-KEY": self.key_secret,
        }

    @staticmethod
    def _check(resp: httpx.Response) -> Any:
        if resp.status_code >= 400:
            try:
                message = resp.json().get("message", "")
            except ValueError:
                message = ""
            if not message or "<html" in message.lower():
                message = resp.reason_phrase or f"HTTP {resp.status_code}"
            if resp.status_code == 429:
                # Say what to DO about it. "too many requests" alone leaves you
                # guessing whether it's your keys, your plan, or the window size.
                message = (
                    "Alpaca rate limit reached, and it didn't clear after retrying. "
                    "The free data plan allows 200 requests a minute — a long comparison "
                    "over many symbols can outrun it. Try a shorter period, fewer symbols, "
                    "or run the two strategies one at a time; already-downloaded bars are "
                    "cached, so a re-run asks for much less."
                )
            raise AlpacaError(resp.status_code, message[:300])
        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()

    @staticmethod
    def _retry_after(resp: httpx.Response, attempt: int) -> float:
        """How long to wait before retrying. Alpaca's own Retry-After wins; else
        a widening backoff."""
        raw = resp.headers.get("Retry-After")
        if raw:
            try:
                return max(0.0, min(60.0, float(raw)))
            except ValueError:
                pass  # HTTP-date form — fall through to the backoff
        return BACKOFF_SECONDS[min(attempt, len(BACKOFF_SECONDS) - 1)]

    async def _get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        base: str | None = None,
        timeout: float = 15,
    ) -> Any:
        """A READ, retried on rate limits and transient gateway errors.

        Only reads. _post is NOT retried and must not be: it places orders, and
        a retry after an ambiguous timeout is how you end up holding the position
        twice. Idempotency is the whole reason this lives here and not in a
        shared helper.
        """
        last: httpx.Response | None = None
        for attempt in range(MAX_READ_ATTEMPTS):
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.get(
                    f"{base or self.base_url}{path}", headers=self._headers(), params=params
                )
            if resp.status_code not in RETRY_STATUSES or attempt == MAX_READ_ATTEMPTS - 1:
                return self._check(resp)
            last = resp
            wait = self._retry_after(resp, attempt)
            log.warning(
                "Alpaca %s on %s — retrying in %.1fs (attempt %d/%d)",
                resp.status_code, path, wait, attempt + 1, MAX_READ_ATTEMPTS,
            )
            await asyncio.sleep(wait)
        return self._check(last)  # unreachable in practice; keeps the type honest

    async def _post(self, path: str, payload: dict[str, Any]) -> Any:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(f"{self.base_url}{path}", headers=self._headers(), json=payload)
        return self._check(resp)

    async def _delete(self, path: str) -> Any:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.delete(f"{self.base_url}{path}", headers=self._headers())
        return self._check(resp)

    # ---- Trading API ----

    async def account(self) -> dict[str, Any]:
        return await self._get("/v2/account")

    async def clock(self) -> dict[str, Any]:
        return await self._get("/v2/clock")

    async def calendar(self, start: str, end: str) -> list[dict[str, Any]]:
        """Trading-session calendar between two YYYY-MM-DD dates. Only trading
        days are returned; each carries the day's real open/close (half-days
        close early). Holidays are omitted entirely."""
        return await self._get("/v2/calendar", params={"start": start, "end": end}) or []

    async def crypto_assets(self) -> list[dict[str, Any]]:
        """Active, tradable crypto pairs (symbols like 'BTC/USD')."""
        return await self.list_assets("crypto")

    async def list_assets(self, alpaca_asset_class: str) -> list[dict[str, Any]]:
        """Tradable assets. `alpaca_asset_class` is 'us_equity' or 'crypto'.
        Each item carries symbol, name, exchange, fractionable, tradable.

        Generous timeout: us_equity is ~11k records / several MB, which the
        default 15s can't reliably pull on a home connection.
        """
        assets = await self._get(
            "/v2/assets",
            params={"asset_class": alpaca_asset_class, "status": "active"},
            timeout=120,
        )
        return [a for a in assets if a.get("tradable")]

    async def submit_order(
        self,
        symbol: str,
        qty: float,
        side: str,
        limit_price: float,
        client_order_id: str,
        time_in_force: str = "day",
    ) -> dict[str, Any]:
        """Marketable LIMIT order — the default, price-protected path."""
        return await self._post(
            "/v2/orders",
            {
                "symbol": symbol,
                "qty": str(qty),
                "side": side,
                "type": "limit",
                "limit_price": str(limit_price),
                "time_in_force": time_in_force,
                "client_order_id": client_order_id,
            },
        )

    async def submit_market_order(
        self,
        symbol: str,
        side: str,
        client_order_id: str,
        *,
        qty: float | None = None,
        notional: float | None = None,
        time_in_force: str = "day",
    ) -> dict[str, Any]:
        """Plain MARKET order — used only when a strategy opts into market +
        fractional execution. Pass exactly one of `qty` (fractional allowed) or
        `notional` (a dollar amount). Notional lets a small $-per-trade buy a
        fractional slice of an expensive name (e.g. $200 of BRK.B). Fills at the
        prevailing price with no limit protection — the trade-off the strategy
        opted into. Alpaca requires market/day for fractional-equity orders."""
        if (qty is None) == (notional is None):
            raise ValueError("submit_market_order needs exactly one of qty or notional.")
        body: dict[str, Any] = {
            "symbol": symbol,
            "side": side,
            "type": "market",
            "time_in_force": time_in_force,
            "client_order_id": client_order_id,
        }
        if notional is not None:
            body["notional"] = str(notional)
        else:
            body["qty"] = str(qty)
        return await self._post("/v2/orders", body)

    async def get_order(self, order_id: str) -> dict[str, Any]:
        return await self._get(f"/v2/orders/{order_id}")

    async def cancel_order(self, order_id: str) -> None:
        await self._delete(f"/v2/orders/{order_id}")

    async def list_positions(self) -> list[dict[str, Any]]:
        """All open positions the broker currently holds."""
        return await self._get("/v2/positions") or []

    async def close_position(self, symbol: str, qty: float | None = None) -> dict[str, Any]:
        """Close ONE position at market — optionally only `qty` units of it, so we
        never touch shares beyond what QT itself holds (another bot may own the
        rest). `symbol` must be the broker's own representation (from
        list_positions), e.g. crypto as 'AVAXUSD'. qty=None closes 100%."""
        from urllib.parse import quote

        path = f"/v2/positions/{quote(symbol, safe='')}"
        if qty is not None:
            q = int(qty) if float(qty).is_integer() else qty
            path += f"?qty={q}"
        return await self._delete(path) or {}

    async def close_all_positions(self, cancel_orders: bool = True) -> list[dict[str, Any]]:
        """Liquidate EVERY open position (at market) and, by default, cancel any
        resting orders — the manual "flatten the whole account" action. This
        deliberately uses the broker's bulk close (market orders), not our usual
        marketable-limit path, because the intent is a guaranteed exit / clean
        slate, not price optimisation. Alpaca returns 207 multi-status; each item
        is one symbol's {symbol, status, ...} result."""
        return await self._delete(f"/v2/positions?cancel_orders={'true' if cancel_orders else 'false'}") or []

    async def list_orders(self, status: str = "open", limit: int = 500) -> list[dict[str, Any]]:
        """Orders by status ('open' | 'closed' | 'all'). Used for reconciliation."""
        return await self._get("/v2/orders", params={"status": status, "limit": limit}) or []

    async def account_activities(
        self, activity_type: str, after: str, until: str, page_size: int = 100
    ) -> list[dict[str, Any]]:
        """Account activities of ONE type between two YYYY-MM-DD days, oldest first.

        Used for fee reconciliation (activity_type 'CFEE' / 'FEE'); see
        qt/services/fees.py for what the payload looks like and why fees can't
        be read at fill time.

        Paginated with `page_token`, which for this endpoint is the LAST ACTIVITY
        ID seen — not an opaque cursor like the market-data endpoints use, and
        not returned in the body. Alpaca caps page_size at 100. We stop when a
        page comes back short, and also break if a page repeats its last id,
        because a token the server ignores would otherwise spin forever.

        `after` / `until` are exclusive on Alpaca's side, so callers that want a
        day included must widen the window themselves.
        """
        out: list[dict[str, Any]] = []
        page_token: str | None = None
        while True:
            params: dict[str, Any] = {
                "after": after,
                "until": until,
                "direction": "asc",
                "page_size": min(page_size, 100),
            }
            if page_token:
                params["page_token"] = page_token
            page = await self._get(f"/v2/account/activities/{activity_type}", params=params) or []
            if not page:
                return out
            out.extend(page)
            last_id = page[-1].get("id")
            if len(page) < params["page_size"] or not last_id or last_id == page_token:
                return out
            page_token = last_id

    # ---- Market data API ----

    async def stock_movers(self, top: int = 50) -> dict[str, Any]:
        """Today's biggest gainers/losers by percent change."""
        return await self._get(
            "/v1beta1/screener/stocks/movers", params={"top": top}, base=DATA_BASE_URL
        )

    async def stock_snapshots(self, symbols: list[str]) -> dict[str, Any]:
        if not symbols:
            return {}
        return await self._get(
            "/v2/stocks/snapshots",
            params={"symbols": ",".join(symbols), "feed": STOCK_FEED},
            base=DATA_BASE_URL,
        )

    async def crypto_snapshots(self, symbols: list[str]) -> dict[str, Any]:
        if not symbols:
            return {}
        payload = await self._get(
            "/v1beta3/crypto/us/snapshots",
            params={"symbols": ",".join(symbols)},
            base=DATA_BASE_URL,
        )
        return payload.get("snapshots", {})

    async def stock_bars(
        self, symbols: list[str], timeframe: str = "15Min", limit: int = 64, start: str | None = None
    ) -> dict[str, Any]:
        if not symbols:
            return {}
        params: dict[str, Any] = {
            "symbols": ",".join(symbols),
            "timeframe": timeframe,
            "limit": limit,
            "feed": STOCK_FEED,
            "sort": "desc",
        }
        # Without an explicit start Alpaca defaults the window to ~today, so a
        # 1Day request comes back with a single (current-day) bar. Callers that
        # need real history (e.g. the 200-day regime MA) must pass a start.
        if start:
            params["start"] = start
        payload = await self._get("/v2/stocks/bars", params=params, base=DATA_BASE_URL)
        return payload.get("bars", {})

    async def crypto_bars(self, symbols: list[str], timeframe: str = "15Min", limit: int = 64) -> dict[str, Any]:
        if not symbols:
            return {}
        payload = await self._get(
            "/v1beta3/crypto/us/bars",
            params={"symbols": ",".join(symbols), "timeframe": timeframe, "limit": limit, "sort": "desc"},
            base=DATA_BASE_URL,
        )
        return payload.get("bars", {})

    async def historical_bars(
        self,
        symbols: list[str],
        asset_class: str,
        timeframe: str,
        start_iso: str,
        end_iso: str | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        """Full history between start and end, oldest-first, following pagination."""
        if not symbols:
            return {}
        if asset_class == "crypto":
            path, extra = "/v1beta3/crypto/us/bars", {}
        else:
            path, extra = "/v2/stocks/bars", {"feed": STOCK_FEED, "adjustment": "split"}
        out: dict[str, list[dict[str, Any]]] = {s: [] for s in symbols}
        page_token: str | None = None
        while True:
            params: dict[str, Any] = {
                "symbols": ",".join(symbols),
                "timeframe": timeframe,
                "start": start_iso,
                "limit": 10000,
                "sort": "asc",
                **extra,
            }
            if end_iso:
                params["end"] = end_iso
            if page_token:
                params["page_token"] = page_token
            payload = await self._get(path, params=params, base=DATA_BASE_URL)
            for symbol, bars in (payload.get("bars") or {}).items():
                out.setdefault(symbol, []).extend(bars)
            page_token = payload.get("next_page_token")
            if not page_token:
                return out
