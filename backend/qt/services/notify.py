"""Slack notifications via incoming webhook. Best-effort: a Slack outage
must never affect trading logic."""

import logging

import httpx
from sqlalchemy.orm import Session

from qt.settings_service import get_setting

log = logging.getLogger("qt.notify")


async def slack(session: Session, text: str) -> bool:
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
