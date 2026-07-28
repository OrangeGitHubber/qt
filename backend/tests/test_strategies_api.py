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


def test_atr_block_persists_through_save(client):
    # pydantic drops keys it doesn't model, so the atr block only survives because
    # StrategyParams declares an explicit ATRConfig field (mirroring DCAConfig).
    body = _body()
    body["params"]["atr"] = {"period": 20, "stop_mult": 2.5, "risk_usd": 75}
    resp = client.post("/api/strategies", json=body)
    assert resp.status_code == 200
    atr = resp.json()["params"]["atr"]
    assert atr == {"period": 20, "stop_mult": 2.5, "risk_usd": 75.0}

    # And it round-trips on read-back.
    sid = resp.json()["id"]
    mine = next(s for s in client.get("/api/strategies").json() if s["id"] == sid)
    assert mine["params"]["atr"]["stop_mult"] == 2.5


def test_macd_flags_and_periods_persist_through_save(client):
    # Regression: the MACD entry/exit flags + periods block were dropped on save
    # because they weren't declared in the pydantic models.
    body = _body()
    body["params"]["entry"]["require_macd_bullish"] = True
    body["params"]["exit"]["exit_on_macd_bearish"] = True
    body["params"]["macd"] = {"fast": 8, "slow": 21, "signal": 5}
    resp = client.post("/api/strategies", json=body)
    assert resp.status_code == 200
    saved = resp.json()["params"]
    assert saved["entry"]["require_macd_bullish"] is True
    assert saved["exit"]["exit_on_macd_bearish"] is True
    assert saved["macd"] == {"fast": 8, "slow": 21, "signal": 5}
    # fast must be < slow
    bad = _body()
    bad["params"]["macd"] = {"fast": 30, "slow": 26, "signal": 9}
    assert client.post("/api/strategies", json=bad).status_code == 422


def test_rotate_on_rank_dropout_persists_through_save(client):
    # Regression: the sector-rotation exit flag was dropped on save.
    bid = client.post("/api/baskets", json={"name": "RotPersist"}).json()["id"]
    body = _body(universe="basket", basket_id=bid, rank_by="relative_strength", top_n=3)
    body["params"]["exit"]["rotate_on_rank_dropout"] = True
    resp = client.post("/api/strategies", json=body)
    assert resp.status_code == 200
    assert resp.json()["params"]["exit"]["rotate_on_rank_dropout"] is True


def test_rank_enabled_opts_in_for_watchlist_and_custom(client):
    made = []
    # A watchlist strategy can now opt into top-N ranking.
    r = client.post(
        "/api/strategies",
        json=_body(universe="watchlist", rank_enabled=True, rank_by="relative_strength", top_n=5),
    )
    assert r.status_code == 200
    assert r.json()["rank_enabled"] is True and r.json()["top_n"] == 5
    made.append(r.json()["id"])

    # So can a custom list (needs symbols).
    r2 = client.post(
        "/api/strategies",
        json=_body(universe="custom", symbols=["AAPL", "MSFT"], rank_enabled=True),
    )
    assert r2.status_code == 200 and r2.json()["rank_enabled"] is True
    made.append(r2.json()["id"])

    # A basket is ALWAYS ranked — the flag is forced on even if omitted/false.
    bid = client.post("/api/baskets", json={"name": "RankForce"}).json()["id"]
    r3 = client.post("/api/strategies", json=_body(universe="basket", basket_id=bid, rank_enabled=False))
    assert r3.status_code == 200 and r3.json()["rank_enabled"] is True
    made.append(r3.json()["id"])

    # Scanner/both are pre-ranked, so the flag is forced OFF (a no-op there).
    r4 = client.post("/api/strategies", json=_body(universe="scanner", rank_enabled=True))
    assert r4.status_code == 200 and r4.json()["rank_enabled"] is False
    made.append(r4.json()["id"])

    for sid in made:
        client.delete(f"/api/strategies/{sid}")
    client.delete(f"/api/baskets/{bid}")


def test_strategy_holdings_lists_open_positions(client):
    from unittest.mock import AsyncMock, patch

    from qt.broker.alpaca import AlpacaClient
    from qt.db import session_scope
    from qt.models import Trade

    sid = client.post("/api/strategies", json=_body()).json()["id"]
    with session_scope() as s:
        s.add(Trade(strategy_id=sid, mode="paper", symbol="AAPL", asset_class="stock",
                    status="open", qty=2, notional=200, entry_price=100.0))
        s.add(Trade(strategy_id=sid, mode="paper", symbol="MSFT", asset_class="stock",
                    status="closed", qty=1, notional=50, entry_price=50.0))  # closed — excluded
        s.commit()

    snaps = {"AAPL": {"latestTrade": {"p": 150.0}}}
    with patch.object(AlpacaClient, "stock_snapshots", new=AsyncMock(return_value=snaps)):
        # Needs keys for get_client to build a client; set them inline.
        from qt import security
        from qt.broker.alpaca import SECRET_KEY_ID, SECRET_KEY_SECRET
        with session_scope() as s:
            security.set_secret(s, SECRET_KEY_ID, "k")
            security.set_secret(s, SECRET_KEY_SECRET, "s")
        body = client.get(f"/api/strategies/{sid}/holdings").json()

    assert len(body["holdings"]) == 1  # only the open AAPL trade
    h = body["holdings"][0]
    assert h["symbol"] == "AAPL"
    assert h["current_price"] == 150.0
    assert h["unrealized_pnl"] == 100.0  # (150-100)*2
    assert body["total_unrealized_pnl"] == 100.0

    with session_scope() as s:
        from qt import security
        from qt.broker.alpaca import SECRET_KEY_ID, SECRET_KEY_SECRET
        s.query(Trade).filter(Trade.strategy_id == sid).delete()
        security.delete_secret(s, SECRET_KEY_ID)
        security.delete_secret(s, SECRET_KEY_SECRET)
        s.commit()
    client.delete(f"/api/strategies/{sid}")


def test_strategy_last_run_endpoint(client):
    from qt.services import engine

    sid = client.post("/api/strategies", json=_body()).json()["id"]
    # Nothing recorded yet.
    assert client.get(f"/api/strategies/{sid}/last-run").json()["ran"] is False

    # Populate a trace exactly as the engine's entry loop would.
    engine._last_run[sid] = {
        "strategy_id": sid, "strategy_name": "x", "ran_at": "2026-07-28T00:00:00Z", "mode": "paper",
        "universe": "Top 3 of the “IT” basket, ranked by today's % move — only these are evaluated; the other members aren't.",
        "outcome": "Evaluated 1 candidate(s); bought 0.",
        "candidates": [
            {"symbol": "NVDA", "price": 120.0, "change_pct": 4.1, "macd_bullish": False,
             "decision": "skipped", "reason": "MACD not bullish — line ≤ signal (bearish)"},
        ],
    }
    r = client.get(f"/api/strategies/{sid}/last-run").json()
    assert r["ran"] is True
    assert r["outcome"].startswith("Evaluated")
    assert r["candidates"][0]["decision"] == "skipped"
    assert "MACD" in r["candidates"][0]["reason"]

    engine._last_run.pop(sid, None)
    client.delete(f"/api/strategies/{sid}")


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


def test_basket_universe_requires_basket_id(client):
    body = _body(universe="basket")
    body.pop("basket_id", None)
    assert client.post("/api/strategies", json=body).status_code == 422


def test_basket_universe_rejects_missing_basket(client):
    assert client.post("/api/strategies", json=_body(universe="basket", basket_id=999999)).status_code == 422


def test_basket_strategy_roundtrips(client):
    bid = client.post("/api/baskets", json={"name": "Roundtrip"}).json()["id"]
    resp = client.post(
        "/api/strategies",
        json=_body(universe="basket", basket_id=bid, rank_by="return_30d", top_n=7),
    )
    assert resp.status_code == 200
    row = resp.json()
    assert row["universe"] == "basket" and row["basket_id"] == bid
    assert row["rank_by"] == "return_30d" and row["top_n"] == 7
    client.delete(f"/api/strategies/{row['id']}")
    client.delete(f"/api/baskets/{bid}")


def test_custom_universe_requires_symbols(client):
    assert client.post("/api/strategies", json=_body(universe="custom")).status_code == 422
    assert client.post("/api/strategies", json=_body(universe="custom", symbols=["  "])).status_code == 422


def test_custom_strategy_roundtrips_and_cleans_symbols(client):
    resp = client.post(
        "/api/strategies",
        json=_body(universe="custom", symbols=["spcx", "AAPL", "spcx", " nvda "]),
    )
    assert resp.status_code == 200
    row = resp.json()
    assert row["universe"] == "custom"
    # de-duped, upper-cased, trimmed, sorted
    assert row["symbols"] == ["AAPL", "NVDA", "SPCX"]
    client.delete(f"/api/strategies/{row['id']}")


def test_bad_rank_by_rejected(client):
    bid = client.post("/api/baskets", json={"name": "BadRank"}).json()["id"]
    assert (
        client.post("/api/strategies", json=_body(universe="basket", basket_id=bid, rank_by="dividend_yield")).status_code
        == 422
    )
    client.delete(f"/api/baskets/{bid}")


def test_rs_vs_spy_accepted_for_stock_basket(client):
    bid = client.post("/api/baskets", json={"name": "RSStock"}).json()["id"]
    resp = client.post(
        "/api/strategies",
        json=_body(universe="basket", basket_id=bid, asset_class="stock", rank_by="rs_vs_spy"),
    )
    assert resp.status_code == 200
    assert resp.json()["rank_by"] == "rs_vs_spy"
    client.delete(f"/api/strategies/{resp.json()['id']}")
    client.delete(f"/api/baskets/{bid}")


def test_rs_vs_spy_rejected_for_crypto_basket(client):
    # rs_vs_spy is benchmarked to SPY (a stock) — a crypto basket can't pick it.
    bid = client.post("/api/baskets", json={"name": "RSCrypto"}).json()["id"]
    assert (
        client.post(
            "/api/strategies",
            json=_body(universe="basket", basket_id=bid, asset_class="crypto", rank_by="rs_vs_spy"),
        ).status_code
        == 422
    )
    client.delete(f"/api/baskets/{bid}")
