"""Thin Alpaca REST client (trading + market data).

Deliberately a small httpx wrapper rather than the full alpaca-py SDK:
keeping the surface tiny makes reliability and testing easier. Streaming
market data in a later phase can still adopt alpaca-py if useful.
"""

from dataclasses import dataclass
from typing import Any

import httpx

PAPER_BASE_URL = "https://paper-api.alpaca.markets"
LIVE_BASE_URL = "https://api.alpaca.markets"
DATA_BASE_URL = "https://data.alpaca.markets"

SECRET_KEY_ID = "alpaca_paper_key_id"
SECRET_KEY_SECRET = "alpaca_paper_key_secret"

# Free-tier accounts get the IEX feed; SIP requires a paid data plan.
STOCK_FEED = "iex"


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
            raise AlpacaError(resp.status_code, message[:300])
        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()

    async def _get(self, path: str, params: dict[str, Any] | None = None, base: str | None = None) -> Any:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(f"{base or self.base_url}{path}", headers=self._headers(), params=params)
        return self._check(resp)

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

    async def crypto_assets(self) -> list[dict[str, Any]]:
        """Active, tradable crypto pairs (symbols like 'BTC/USD')."""
        assets = await self._get("/v2/assets", params={"asset_class": "crypto", "status": "active"})
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
        """Marketable LIMIT order — this app never sends plain market orders."""
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

    async def get_order(self, order_id: str) -> dict[str, Any]:
        return await self._get(f"/v2/orders/{order_id}")

    async def cancel_order(self, order_id: str) -> None:
        await self._delete(f"/v2/orders/{order_id}")

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

    async def stock_bars(self, symbols: list[str], timeframe: str = "15Min", limit: int = 64) -> dict[str, Any]:
        if not symbols:
            return {}
        payload = await self._get(
            "/v2/stocks/bars",
            params={
                "symbols": ",".join(symbols),
                "timeframe": timeframe,
                "limit": limit,
                "feed": STOCK_FEED,
                "sort": "desc",
            },
            base=DATA_BASE_URL,
        )
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
