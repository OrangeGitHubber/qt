"""Market-calendar correctness (half-days, holidays).

Hardcoded ET clock times are wrong on half-days (the market closes at 13:00,
not 16:00) and holidays (no session at all). Two things depend on this:

* **Flatten-before-close** already uses Alpaca's ``clock.next_close``, which is
  half-day/holiday-correct at the source — verified, no change needed here.
* **The daily summary** used to fire on a fixed 16:10 ET cron regardless of
  whether the market even traded. It now consults this calendar and skips
  non-trading days.

The decision helpers are pure functions over Alpaca ``/v2/calendar`` entries so
they unit-test with no broker. The fetch is cached (the calendar changes at
most daily).
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

from qt.broker.alpaca import AlpacaClient

# A normal NYSE/Nasdaq session closes at 16:00 ET. Half-days close early
# (typically 13:00) — Alpaca reports the actual close per day.
NORMAL_CLOSE = "16:00"

_CACHE_TTL = 6 * 3600.0  # 6 hours
_cache: dict = {"at": 0.0, "start": None, "end": None, "value": None}


# ---- pure helpers ----


def find_day(calendar: list[dict], date_str: str) -> dict | None:
    """The calendar entry for ``date_str`` (YYYY-MM-DD), or None if the market
    did not trade that day (Alpaca omits holidays/weekends)."""
    for entry in calendar:
        if entry.get("date") == date_str:
            return entry
    return None


def is_trading_day(calendar: list[dict], date_str: str) -> bool:
    return find_day(calendar, date_str) is not None


def is_half_day(entry: dict | None) -> bool:
    """True if the given calendar entry closes earlier than a normal session."""
    if not entry:
        return False
    close = entry.get("close", "")
    return bool(close) and close < NORMAL_CLOSE


def market_closed_on(calendar: list[dict], date_str: str) -> bool:
    return not is_trading_day(calendar, date_str)


# ---- cached fetch ----


async def get_calendar(client: AlpacaClient, start_iso: str, end_iso: str) -> list[dict]:
    now = time.monotonic()
    if (
        _cache["value"] is not None
        and _cache["start"] == start_iso
        and _cache["end"] == end_iso
        and now - _cache["at"] < _CACHE_TTL
    ):
        return _cache["value"]
    value = await client.calendar(start_iso, end_iso)
    _cache.update(at=now, start=start_iso, end=end_iso, value=value)
    return value


def invalidate_cache() -> None:
    _cache.update(at=0.0, start=None, end=None, value=None)


async def is_trading_today(client: AlpacaClient, today: datetime | None = None) -> bool:
    """Convenience: did the US market trade today (ET date)?"""
    from zoneinfo import ZoneInfo

    now = today or datetime.now(timezone.utc)
    et_date = now.astimezone(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    calendar = await get_calendar(client, et_date, et_date)
    return is_trading_day(calendar, et_date)
