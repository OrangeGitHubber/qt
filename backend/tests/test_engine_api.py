import os

from qt.db import session_scope
from qt.settings_service import set_setting


def _reset(client):
    with session_scope() as s:
        set_setting(s, "engine_mode", "off")
        set_setting(s, "risk_config", {})


def test_engine_state_defaults(client):
    _reset(client)
    body = client.get("/api/engine").json()
    assert body["mode"] == "off"
    assert body["risk"]["leverage_enabled"] is False
    assert body["leverage"]["unlockable"] is False


def test_mode_validation(client):
    _reset(client)
    assert client.post("/api/engine/mode", json={"mode": "warp"}).status_code == 422
    # paper requires confirmation
    assert client.post("/api/engine/mode", json={"mode": "paper"}).status_code == 428
    # paper requires an enabled strategy
    assert client.post("/api/engine/mode", json={"mode": "paper", "confirm": True}).status_code == 409
    # shadow is allowed freely
    assert client.post("/api/engine/mode", json={"mode": "shadow"}).status_code == 200
    assert client.get("/api/engine").json()["mode"] == "shadow"
    _reset(client)


def _risk_payload(**overrides):
    payload = {
        "max_daily_loss_usd": 150,
        "max_daily_loss_pct": 4,
        "max_total_positions": 5,
        "max_total_exposure_usd": 2500,
        "max_trades_per_day": 8,
        "cooldown_hours_after_loss": 12,
        "wash_sale_guard": "block",
        "leverage_enabled": False,
        "leverage_confirm": "",
    }
    payload.update(overrides)
    return payload


def test_risk_update_roundtrip(client):
    _reset(client)
    resp = client.put("/api/engine/risk", json=_risk_payload())
    assert resp.status_code == 200
    assert resp.json()["max_trades_per_day"] == 8


def test_leverage_blocked_without_env_var(client):
    _reset(client)
    os.environ.pop("QT_ALLOW_LEVERAGE", None)
    resp = client.put("/api/engine/risk", json=_risk_payload(leverage_enabled=True))
    assert resp.status_code == 403
    assert "locked at the server level" in resp.json()["detail"]


def test_leverage_with_env_var_needs_typed_confirmation(client):
    _reset(client)
    os.environ["QT_ALLOW_LEVERAGE"] = "true"
    try:
        resp = client.put("/api/engine/risk", json=_risk_payload(leverage_enabled=True))
        assert resp.status_code == 428  # visible but still needs the phrase

        resp = client.put(
            "/api/engine/risk",
            json=_risk_payload(leverage_enabled=True, leverage_confirm="I ACCEPT AMPLIFIED LOSSES"),
        )
        assert resp.status_code == 200
        assert resp.json()["leverage_enabled"] is True

        # engine state reflects it while unlocked
        assert client.get("/api/engine").json()["leverage"]["enabled"] is True
    finally:
        os.environ.pop("QT_ALLOW_LEVERAGE", None)
        _reset(client)


def test_leverage_setting_ignored_once_env_removed(client):
    """Even if leverage was enabled while unlocked, removing the env var re-locks it."""
    _reset(client)
    os.environ["QT_ALLOW_LEVERAGE"] = "true"
    client.put(
        "/api/engine/risk",
        json=_risk_payload(leverage_enabled=True, leverage_confirm="I ACCEPT AMPLIFIED LOSSES"),
    )
    os.environ.pop("QT_ALLOW_LEVERAGE", None)
    body = client.get("/api/engine").json()
    assert body["risk"]["leverage_enabled"] is False
    assert body["leverage"]["unlockable"] is False
    _reset(client)


def test_slack_url_validation(client):
    assert client.put("/api/engine/slack", json={"url": "https://evil.example.com/x"}).status_code == 422
    assert client.put("/api/engine/slack", json={"url": "https://hooks.slack.com/services/T00/B00/xyz"}).status_code == 200
    assert client.put("/api/engine/slack", json={"url": ""}).status_code == 200
