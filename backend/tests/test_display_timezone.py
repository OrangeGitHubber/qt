"""The display-timezone setting: a presentation choice, stored server-side.

The point of these tests is the boundary. Storage is UTC and must stay UTC, so
what is asserted here is that this setting persists, validates, and touches
nothing else — a timezone that changed a stored timestamp would be the bug the
setting exists to avoid.
"""

from qt.db import session_scope
from qt.settings_service import DEFAULTS, get_setting, set_setting


def _reset():
    with session_scope() as s:
        set_setting(s, "display_timezone", DEFAULTS["display_timezone"])
        s.commit()


def test_default_is_the_market_zone(client):
    """Unset means New York — the market's own clock, not the server's locale."""
    with session_scope() as s:
        set_setting(s, "display_timezone", DEFAULTS["display_timezone"])
        s.commit()
    assert client.get("/api/settings/display").json()["display_timezone"] == "America/New_York"


def test_save_and_read_back(client):
    r = client.put("/api/settings/display", json={"display_timezone": "Africa/Johannesburg"})
    assert r.status_code == 200
    assert client.get("/api/settings/display").json()["display_timezone"] == "Africa/Johannesburg"
    _reset()


def test_unknown_zone_is_rejected(client):
    """A name the server can't resolve is one the browser probably can't either,
    and a bad value would break the formatting of every timestamp at once — so it
    must never reach the database."""
    r = client.put("/api/settings/display", json={"display_timezone": "Mars/Olympus_Mons"})
    assert r.status_code == 422
    with session_scope() as s:
        assert get_setting(s, "display_timezone") == DEFAULTS["display_timezone"]


def test_offset_style_zone_is_rejected(client):
    """Offsets are what makes daylight saving rot. Only IANA names get stored."""
    assert client.put("/api/settings/display", json={"display_timezone": "UTC+2"}).status_code == 422


def test_status_carries_the_zone(client):
    """The shell blocks its first render on /api/status, so the zone rides along
    there — that is what stops timestamps drawing in the default and then
    flipping once a separate settings fetch lands."""
    client.put("/api/settings/display", json={"display_timezone": "Asia/Tokyo"})
    assert client.get("/api/status").json()["display_timezone"] == "Asia/Tokyo"
    _reset()
