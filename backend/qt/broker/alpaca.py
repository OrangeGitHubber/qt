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

    async def _get(self, path: str, params: dict[str, Any] | None = None, base: str | None = None) -> Any:
        url = f"{base or self.base_url}{path}"
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, headers=self._headers(), params=params)
        if resp.status_code >= 400:
            try:
                message = resp.json().get("message", "")
            except ValueError:
                message = ""
            if not message or "<html" in message.lower():
                message = resp.reason_phrase or f"HTTP {resp.status_code}"
            raise AlpacaError(resp.status_code, message[:300])
        return resp.json()

    # ---- Trading API ----

    async def account(self) -> dict[str, Any]:
        return await self._get("/v2/account")

    async def clock(self) -> dict[str, Any]:
        return await self._get("/v2/clock")

    async def crypto_assets(self) -> list[dict[str, Any]]:
        """Active, tradable crypto pairs (symbols like 'BTC/USD')."""
        assets = await self._get("/v2/assets", params={"asset_class": "crypto", "status": "active"})
        return [a for a in assets if a.get("tradable")]

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
