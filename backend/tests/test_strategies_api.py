def _body(**overrides):
    body = {
        "name": "Test momentum",
        "asset_class": "stock",
        "universe": "scanner",
        "preset": "momentum_swing_stocks",
        "params": {
            "entry": {"min_day_gain_pct": 3, "require_above_vwap": True,
                      "entry_window_start": "10:00", "entry_window_end": "15:30"},
            "exit": {"trailing_stop_pct": 5, "stop_loss_pct": 4, "take_profit_pct": 12,
                     "max_holding_hours": 120, "flatten_before_close": False, "exit_below_vwap": False},
        },
        "sizing_usd": 200,
        "sleeve_usd": 1000,
        "max_positions": 3,
        "swing_mode": True,
        "ignore_regime": False,
    }
    body.update(overrides)
    return body


def test_presets_available(client):
    presets = client.get("/api/strategies/presets").json()
    assert "momentum_swing_stocks" in presets
    assert presets["momentum_intraday_crypto"]["asset_class"] == "crypto"


def test_create_update_versioning(client):
    resp = client.post("/api/strategies", json=_body())
    assert resp.status_code == 200
    created = resp.json()
    assert created["enabled"] is False  # strategies are born disabled
    sid = created["id"]

    listed = client.get("/api/strategies").json()
    mine = next(s for s in listed if s["id"] == sid)
    assert mine["version"] == 1

    resp = client.put(f"/api/strategies/{sid}", json=_body(name="Test momentum v2"))
    assert resp.status_code == 200
    mine = next(s for s in client.get("/api/strategies").json() if s["id"] == sid)
    assert mine["version"] == 2 and mine["name"] == "Test momentum v2"

    # toggle + delete
    assert client.post(f"/api/strategies/{sid}/toggle").json()["enabled"] is True
    assert client.post(f"/api/strategies/{sid}/toggle").json()["enabled"] is False
    assert client.delete(f"/api/strategies/{sid}").status_code == 200


def test_stop_loss_is_mandatory(client):
    body = _body()
    body["params"]["exit"]["stop_loss_pct"] = 0
    assert client.post("/api/strategies", json=body).status_code == 422


def test_sizing_cannot_exceed_sleeve(client):
    assert client.post("/api/strategies", json=_body(sizing_usd=2000, sleeve_usd=1000)).status_code == 422
