"""Presentation settings — how the app is *shown*, not how it behaves.

Nothing here changes a stored value or a trading decision. It exists because
those choices still have to survive a container restart and read the same from
every browser, which rules out localStorage.
"""

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from qt.db import get_session
from qt.settings_service import get_setting, set_setting

router = APIRouter(prefix="/api/settings", tags=["settings"])


class DisplaySettings(BaseModel):
    # An IANA zone NAME, never an offset: the name is what makes daylight saving
    # correct by itself, and a stored offset would be wrong for half the year.
    display_timezone: str = Field(min_length=1, max_length=64)


@router.get("/display")
def display_settings(session: Session = Depends(get_session)) -> dict:
    return {"display_timezone": get_setting(session, "display_timezone")}


@router.put("/display")
def save_display_settings(body: DisplaySettings, session: Session = Depends(get_session)) -> dict:
    # Validate against the system tz database here rather than trusting the UI's
    # dropdown — a saved name the server can't resolve would be a name the
    # browser probably can't either, and every timestamp in the app would then
    # fail to format at once.
    try:
        ZoneInfo(body.display_timezone)
    except (ZoneInfoNotFoundError, ValueError):
        raise HTTPException(
            status_code=422,
            detail=f"'{body.display_timezone}' is not a known timezone name. "
            "Use an IANA name such as America/New_York.",
        )
    set_setting(session, "display_timezone", body.display_timezone)
    return {"display_timezone": body.display_timezone}
