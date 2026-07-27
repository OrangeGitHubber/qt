"""Slack notifications via incoming webhook. Best-effort: a Slack outage
must never affect trading logic.

Every message belongs to a CATEGORY the user can opt into (Settings → Slack).
``slack_cat`` gates on that saved preference; ``slack`` always sends and is used
only for the manual "send test" button.
"""

import logging

import httpx
from sqlalchemy.orm import Session

from qt.settings_service import get_setting, set_setting

log = logging.getLogger("qt.notify")

PREFS_KEY = "slack_notify_prefs"

# The finite catalog of opt-in Slack message types. ``default`` applies until the
# user saves a preference. Order here is the display order in Settings, and the
# API serves this list so the UI never hard-codes labels/descriptions.
NOTIFY_CATEGORIES: list[dict] = [
    {
        "key": "trade_confirmations",
        "label": "Trade confirmations",
        "default": True,
        "description": "A message the moment the bot buys or sells — symbol, quantity, price, "
        "the reason it fired, and realized P&L on exits.",
    },
    {
        "key": "daily_summary",
        "label": "Daily summary",
        "default": True,
        "description": "An end-of-trading-day recap: how many trades opened and closed, and the "
        "day's realized P&L. Posts after the US close on trading days.",
    },
    {
        "key": "weekly_summary",
        "label": "Weekly summary",
        "default": False,
        "description": "A Sunday-evening recap of the past 7 days: trades closed, realized P&L, "
        "and win rate.",
    },
    {
        "key": "strategy_breakdown",
        "label": "Per-strategy P&L breakdown",
        "default": False,
        "description": "Adds a per-strategy line-item breakdown to the daily and weekly summaries, "
        "so you can see which strategy drove the result. Does nothing on its own — turn on a "
        "summary above.",
    },
    {
        "key": "reconciliation",
        "label": "Reconciliation alerts",
        "default": True,
        "description": "When the bot re-syncs with Alpaca and finds a mismatch — a broker position "
        "it isn't tracking, or an exit that filled while it was offline.",
    },
    {
        "key": "engine_health",
        "label": "Engine health warnings",
        "default": True,
        "description": "A watchdog alert if the engine stops ticking while the market is open.",
    },
    {
        "key": "risk_changes",
        "label": "Risk & leverage changes",
        "default": True,
        "description": "When leverage is enabled or disabled in the risk settings.",
    },
    {
        "key": "system_alerts",
        "label": "Critical system alerts",
        "default": True,
        "description": "Data-persistence or secret-decryption problems that can silently lose your "
        "configuration and trade history. Strongly recommended to keep on.",
    },
]

_DEFAULTS = {c["key"]: c["default"] for c in NOTIFY_CATEGORIES}


def notify_prefs(session: Session) -> dict[str, bool]:
    """Effective per-category on/off, with each category's default merged under
    any saved value."""
    stored = get_setting(session, PREFS_KEY) or {}
    return {key: bool(stored.get(key, default)) for key, default in _DEFAULTS.items()}


def set_notify_prefs(session: Session, prefs: dict[str, bool]) -> dict[str, bool]:
    """Persist a (partial) set of category toggles; unknown keys are ignored.
    Returns the full effective prefs after the update."""
    current = dict(get_setting(session, PREFS_KEY) or {})
    for key, value in prefs.items():
        if key in _DEFAULTS:
            current[key] = bool(value)
    set_setting(session, PREFS_KEY, current)
    return notify_prefs(session)


def category_enabled(session: Session, category: str) -> bool:
    """Is this message category currently enabled? Unknown categories default to
    on, so a new message type is never silently muted before it's catalogued."""
    return notify_prefs(session).get(category, True)


async def slack(session: Session, text: str) -> bool:
    """Post to the configured webhook unconditionally (the manual test button).
    Category-gated callers should use :func:`slack_cat` instead."""
    url = get_setting(session, "slack_webhook_url")
    if not url:
        return False
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json={"text": text})
        return resp.status_code < 300
    except Exception as exc:
        log.warning("Slack notification failed: %s", exc)
        return False


async def slack_cat(session: Session, category: str, text: str) -> bool:
    """Post ``text`` only if the user has this message ``category`` enabled."""
    if not category_enabled(session, category):
        return False
    return await slack(session, text)
