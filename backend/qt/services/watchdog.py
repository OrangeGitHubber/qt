"""Engine heartbeat + watchdog.

The engine records a heartbeat (``last_tick_at``) at the end of every healthy
tick. The watchdog job periodically checks whether that heartbeat has gone
stale while the market is open, and Slack-alerts once if so. The staleness
decision is a pure function so it's trivially testable.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

log = logging.getLogger("qt.watchdog")

DEFAULT_STALE_MINUTES = 5
_HEARTBEAT_KEY = "last_tick_at"
_ALERTED_KEY = "watchdog_alerted"


def tick_is_stale(last_tick_at: datetime | None, now: datetime, threshold: timedelta) -> bool:
    """True if we've never ticked, or the last tick is older than ``threshold``."""
    if last_tick_at is None:
        return True
    if last_tick_at.tzinfo is None:
        last_tick_at = last_tick_at.replace(tzinfo=timezone.utc)
    return (now - last_tick_at) > threshold


def should_alert(
    *,
    mode: str,
    market_open: bool,
    last_tick_at: datetime | None,
    now: datetime,
    threshold: timedelta,
    already_alerted: bool,
) -> bool:
    """Pure watchdog decision.

    Alert only when the engine is supposed to be running (mode != off), the
    market is open (so a healthy engine would be ticking), the heartbeat is
    stale, and we haven't already alerted for this stall."""
    if mode == "off":
        return False
    if not market_open:
        return False
    if already_alerted:
        return False
    return tick_is_stale(last_tick_at, now, threshold)


# ---- impure glue ----


def record_heartbeat(session) -> None:
    """Called at the end of a healthy engine tick. Also clears the alerted flag
    so a recovered engine can alert again on a future stall."""
    from qt.settings_service import get_setting, set_setting

    set_setting(session, _HEARTBEAT_KEY, datetime.now(timezone.utc).isoformat())
    if get_setting(session, _ALERTED_KEY):
        set_setting(session, _ALERTED_KEY, False)


def last_tick_at(session) -> datetime | None:
    from qt.settings_service import get_setting

    raw = get_setting(session, _HEARTBEAT_KEY)
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


async def check(threshold_minutes: int = DEFAULT_STALE_MINUTES) -> None:
    """Watchdog job: alert once if the engine heartbeat is stale while open."""
    from qt.broker.factory import get_client
    from qt.db import session_scope
    from qt.services import notify
    from qt.services.engine import get_mode
    from qt.settings_service import get_setting, set_setting

    try:
        with session_scope() as session:
            mode = get_mode(session)
            if mode == "off":
                return
            client = get_client(session)
            if client is None:
                return
            try:
                clock = await client.clock()
            except Exception as exc:
                log.warning("watchdog: could not fetch clock: %s", exc)
                return
            now = datetime.now(timezone.utc)
            last = last_tick_at(session)
            already = bool(get_setting(session, _ALERTED_KEY))
            if should_alert(
                mode=mode,
                market_open=bool(clock.get("is_open")),
                last_tick_at=last,
                now=now,
                threshold=timedelta(minutes=threshold_minutes),
                already_alerted=already,
            ):
                ago = "never" if last is None else f"{(now - last).total_seconds() / 60:.0f} min ago"
                log.error("watchdog: engine heartbeat stale (last tick %s)", ago)
                await notify.slack(
                    session,
                    f":rotating_light: *QT engine may be stalled* — last successful tick {ago}, "
                    f"but the market is open and the engine is in {mode.upper()} mode. Check the container.",
                )
                set_setting(session, _ALERTED_KEY, True)
    except Exception:
        log.exception("watchdog check failed")
