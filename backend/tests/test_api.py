from unittest.mock import AsyncMock, patch

from qt.broker.alpaca import AlpacaError

FAKE_ACCOUNT = {
    "account_number": "PA123",
    "status": "ACTIVE",
    "equity": "100000",
    "cash": "100000",
    "buying_power": "200000",
    "currency": "USD",
}
FAKE_CLOCK = {
    "is_open": False,
    "next_open": "2026-07-13T13:30:00Z",
    "next_close": "2026-07-13T20:00:00Z",
    "timestamp": "2026-07-11T15:00:00Z",
}


def test_health(client):
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_setup_state_starts_unconfigured(client):
    resp = client.get("/api/setup/state")
    assert resp.status_code == 200
    assert resp.json()["alpaca_configured"] is False


def test_status_unconfigured(client):
    resp = client.get("/api/status")
    body = resp.json()
    assert body["alpaca_configured"] is False
    assert body["trading_mode"] == "paper"


def test_save_keys_rejected_by_alpaca(client):
    with patch("qt.api.setup.AlpacaClient.account", new=AsyncMock(side_effect=AlpacaError(401, "unauthorized"))):
        resp = client.post("/api/setup/alpaca", json={"key_id": "bad", "key_secret": "bad"})
    assert resp.status_code == 400
    assert "401" in resp.json()["detail"]


def test_save_keys_then_status(client):
    with patch("qt.api.setup.AlpacaClient.account", new=AsyncMock(return_value=FAKE_ACCOUNT)):
        resp = client.post("/api/setup/alpaca", json={"key_id": "good", "key_secret": "good"})
    assert resp.status_code == 200
    assert resp.json()["account_number"] == "PA123"

    assert client.get("/api/setup/state").json()["alpaca_configured"] is True

    with (
        patch("qt.api.status.AlpacaClient.account", new=AsyncMock(return_value=FAKE_ACCOUNT)),
        patch("qt.api.status.AlpacaClient.clock", new=AsyncMock(return_value=FAKE_CLOCK)),
    ):
        body = client.get("/api/status").json()
    assert body["alpaca_configured"] is True
    assert body["broker"]["equity"] == "100000"
    assert body["market"]["is_open"] is False
