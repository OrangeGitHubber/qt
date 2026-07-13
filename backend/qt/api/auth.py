"""Google Sign-In (OIDC authorization-code flow) gating the whole app.

Flow: /api/auth/login redirects to Google → Google redirects back to
/api/auth/callback with a code → we exchange it server-side (confidential
client over TLS, so Google's userinfo endpoint is the source of truth for
the email) → allowlist check → encrypted session cookie.

Bootstrap: until a Google client is configured, /api/auth/bootstrap accepts
the client ID/secret + owner email once. Every other endpoint returns 503
until then, so an unconfigured instance exposes nothing.
"""

import secrets as pysecrets
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.orm import Session

from qt import security
from qt.api.deps import SESSION_COOKIE, auth_disabled, require_owner, require_user
from qt.db import get_session
from qt.models import AuditLog
from qt.settings_service import get_setting, set_setting

router = APIRouter(prefix="/api/auth", tags=["auth"])

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
STATE_COOKIE = "qt_oauth_state"
SECRET_GOOGLE_CLIENT_SECRET = "google_client_secret"


class BootstrapBody(BaseModel):
    client_id: str = Field(min_length=10)
    client_secret: str = Field(min_length=10)
    owner_email: EmailStr


def _redirect_uri(request: Request) -> str:
    return str(request.base_url).rstrip("/") + "/api/auth/callback"


@router.get("/state")
def auth_state(request: Request, session: Session = Depends(get_session)) -> dict:
    """Public: the frontend uses this to decide which screen to show."""
    configured = bool(get_setting(session, "google_client_id"))
    email = None
    if auth_disabled():
        email = "dev@localhost"
    else:
        token = request.cookies.get(SESSION_COOKIE)
        candidate = security.verify_session_token(token) if token else None
        allowed = get_setting(session, "allowed_emails") or []
        if candidate and candidate.lower() in (a.lower() for a in allowed):
            email = candidate
    return {
        "configured": configured,
        "email": email,
        "auth_disabled": auth_disabled(),
        "redirect_uri": _redirect_uri(request),
    }


@router.post("/bootstrap")
def bootstrap(body: BootstrapBody, session: Session = Depends(get_session)) -> dict:
    if get_setting(session, "google_client_id"):
        raise HTTPException(status_code=409, detail="Authentication is already configured.")
    set_setting(session, "google_client_id", body.client_id.strip())
    security.set_secret(session, SECRET_GOOGLE_CLIENT_SECRET, body.client_secret.strip())
    set_setting(session, "owner_email", str(body.owner_email))
    set_setting(session, "allowed_emails", [str(body.owner_email)])
    session.add(AuditLog(category="auth", message=f"Google Sign-In configured; owner {body.owner_email}"))
    return {"ok": True}


@router.get("/login")
def login(request: Request, session: Session = Depends(get_session)) -> RedirectResponse:
    client_id = get_setting(session, "google_client_id")
    if not client_id:
        raise HTTPException(status_code=503, detail="Authentication is not set up yet.")
    state = pysecrets.token_urlsafe(24)
    params = {
        "client_id": client_id,
        "redirect_uri": _redirect_uri(request),
        "response_type": "code",
        "scope": "openid email",
        "state": state,
        "prompt": "select_account",
    }
    resp = RedirectResponse(f"{GOOGLE_AUTH_URL}?{urlencode(params)}")
    resp.set_cookie(
        STATE_COOKIE,
        security.get_fernet().encrypt(state.encode()).decode(),
        max_age=600,
        httponly=True,
        samesite="lax",
    )
    return resp


async def _exchange_code_for_email(
    code: str, client_id: str, client_secret: str, redirect_uri: str
) -> tuple[str, bool]:
    """Exchange the auth code and return (email, email_verified)."""
    async with httpx.AsyncClient(timeout=15) as client:
        token_resp = await client.post(
            GOOGLE_TOKEN_URL,
            data={
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
        )
        if token_resp.status_code != 200:
            raise HTTPException(status_code=401, detail="Google rejected the sign-in code.")
        access_token = token_resp.json().get("access_token")
        info_resp = await client.get(
            GOOGLE_USERINFO_URL, headers={"Authorization": f"Bearer {access_token}"}
        )
        if info_resp.status_code != 200:
            raise HTTPException(status_code=401, detail="Could not fetch Google account info.")
        info = info_resp.json()
    return info.get("email", ""), bool(info.get("email_verified"))


@router.get("/callback")
async def callback(
    request: Request,
    code: str = "",
    state: str = "",
    session: Session = Depends(get_session),
) -> RedirectResponse:
    from cryptography.fernet import InvalidToken

    state_cookie = request.cookies.get(STATE_COOKIE, "")
    try:
        expected_state = security.get_fernet().decrypt(state_cookie.encode(), ttl=600).decode()
    except (InvalidToken, ValueError):
        raise HTTPException(status_code=400, detail="Sign-in session expired — try again.")
    if not code or not state or state != expected_state:
        raise HTTPException(status_code=400, detail="Invalid sign-in state — try again.")

    client_id = get_setting(session, "google_client_id")
    client_secret = security.get_secret(session, SECRET_GOOGLE_CLIENT_SECRET)
    email, verified = await _exchange_code_for_email(
        code, client_id, client_secret, _redirect_uri(request)
    )
    if not email or not verified:
        raise HTTPException(status_code=401, detail="Google account has no verified email.")

    allowed = get_setting(session, "allowed_emails") or []
    if email.lower() not in (a.lower() for a in allowed):
        session.add(AuditLog(category="auth", message=f"Sign-in DENIED for {email} (not on allowlist)"))
        resp = RedirectResponse("/?denied=1")
        resp.delete_cookie(STATE_COOKIE)
        return resp

    session.add(AuditLog(category="auth", message=f"Sign-in OK: {email}"))
    resp = RedirectResponse("/")
    resp.delete_cookie(STATE_COOKIE)
    resp.set_cookie(
        SESSION_COOKIE,
        security.create_session_token(email),
        max_age=security.SESSION_TTL_SECONDS,
        httponly=True,
        samesite="lax",
    )
    return resp


@router.post("/logout")
def logout() -> RedirectResponse:
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie(SESSION_COOKIE)
    return resp


# ---- Allowlist management ----


class AllowlistBody(BaseModel):
    email: EmailStr


@router.get("/allowlist")
def get_allowlist(
    session: Session = Depends(get_session), _user: str = Depends(require_user)
) -> dict:
    return {
        "emails": get_setting(session, "allowed_emails") or [],
        "owner": get_setting(session, "owner_email"),
    }


@router.post("/allowlist")
def add_allowlist(
    body: AllowlistBody,
    session: Session = Depends(get_session),
    owner: str = Depends(require_owner),
) -> dict:
    emails = get_setting(session, "allowed_emails") or []
    email = str(body.email)
    if email.lower() in (e.lower() for e in emails):
        raise HTTPException(status_code=409, detail="Already on the allowlist.")
    emails.append(email)
    set_setting(session, "allowed_emails", emails)
    session.add(AuditLog(category="auth", message=f"{owner} added {email} to allowlist"))
    return {"emails": emails}


@router.delete("/allowlist/{email}")
def remove_allowlist(
    email: str,
    session: Session = Depends(get_session),
    owner: str = Depends(require_owner),
) -> dict:
    owner_email = get_setting(session, "owner_email") or ""
    if email.lower() == owner_email.lower():
        raise HTTPException(status_code=400, detail="The owner cannot be removed.")
    emails = [e for e in (get_setting(session, "allowed_emails") or []) if e.lower() != email.lower()]
    set_setting(session, "allowed_emails", emails)
    session.add(AuditLog(category="auth", message=f"{owner} removed {email} from allowlist"))
    return {"emails": emails}
