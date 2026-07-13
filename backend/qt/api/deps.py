"""Shared FastAPI dependencies — above all, the auth gate."""

import os

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from qt import security
from qt.db import get_session
from qt.settings_service import get_setting

SESSION_COOKIE = "qt_session"


def auth_disabled() -> bool:
    """Dev-only bypass. Documented, never present in the unraid template."""
    return os.environ.get("QT_AUTH_DISABLED", "").lower() == "true"


def leverage_unlockable() -> bool:
    """Container-level lock: without this env var the leverage option
    does not exist anywhere in the app."""
    return os.environ.get("QT_ALLOW_LEVERAGE", "").lower() == "true"


def require_user(request: Request, session: Session = Depends(get_session)) -> str:
    """Every data/config endpoint depends on this. Returns the user's email."""
    if auth_disabled():
        return "dev@localhost"

    client_id = get_setting(session, "google_client_id")
    if not client_id:
        raise HTTPException(status_code=503, detail="Authentication is not set up yet.")

    token = request.cookies.get(SESSION_COOKIE)
    email = security.verify_session_token(token) if token else None
    if not email:
        raise HTTPException(status_code=401, detail="Not signed in.")

    allowed = get_setting(session, "allowed_emails") or []
    if email.lower() not in (a.lower() for a in allowed):
        raise HTTPException(status_code=403, detail="This Google account is not on the allowlist.")
    return email


def require_owner(request: Request, session: Session = Depends(get_session)) -> str:
    email = require_user(request, session)
    owner = get_setting(session, "owner_email")
    if not auth_disabled() and (not owner or email.lower() != owner.lower()):
        raise HTTPException(status_code=403, detail="Only the owner account can do this.")
    return email
