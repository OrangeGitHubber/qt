"""Thin Alpaca REST client (paper trading endpoints for now).

Deliberately a small httpx wrapper rather than the full alpaca-py SDK:
Phase 0/1 only needs account + clock + assets, and keeping the surface
tiny makes reliability and testing easier. Streaming market data in a
later phase can still adopt alpaca-py if useful.
"""

from dataclasses import dataclass
from typing import Any

import httpx

PAPER_BASE_URL = "https://paper-api.alpaca.markets"
LIVE_BASE_URL = "https://api.alpaca.markets"

SECRET_KEY_ID = "alpaca_paper_key_id"
SECRET_KEY_SECRET = "alpaca_paper_key_secret"


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

    async def _get(self, path: str) -> Any:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(f"{self.base_url}{path}", headers=self._headers())
        if resp.status_code >= 400:
            try:
                message = resp.json().get("message", resp.text)
            except ValueError:
                message = resp.text
            raise AlpacaError(resp.status_code, message)
        return resp.json()

    async def account(self) -> dict[str, Any]:
        return await self._get("/v2/account")

    async def clock(self) -> dict[str, Any]:
        return await self._get("/v2/clock")
