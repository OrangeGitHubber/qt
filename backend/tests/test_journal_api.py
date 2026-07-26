from qt.db import session_scope
from qt.models import Trade

STRAT = {
    "name": "Journal filter test",
    "asset_class": "stock",
    "universe": "scanner",
    "preset": "custom",
    "params": {
        "entry": {"min_day_gain_pct": 3, "require_above_vwap": True,
                  "entry_window_start": None, "entry_window_end": None},
        "exit": {"trailing_stop_pct": 5, "stop_loss_pct": 4, "take_profit_pct": 12,
                 "max_holding_hours": 0, "flatten_before_close": False, "exit_below_vwap": False},
    },
    "sizing_usd": 200, "sleeve_usd": 1000, "max_positions": 3,
    "swing_mode": True, "ignore_regime": False,
}


def test_journal_status_filter_and_timestamp(client):
    sid = client.post("/api/strategies", json=STRAT).json()["id"]
    with session_scope() as s:
        for st in ("open", "closed", "rejected"):
            s.add(Trade(strategy_id=sid, mode="shadow", symbol="AAA",
                        asset_class="stock", qty=1, notional=100, status=st))
        s.add(Trade(strategy_id=sid, mode="shadow", symbol="BTC/USD",
                    asset_class="crypto", qty=1, notional=100, status="open"))

    mine = lambda rows: [r for r in rows if r["strategy"] == "Journal filter test"]

    all_rows = mine(client.get("/api/engine/journal").json())
    assert len(all_rows) == 4  # 3 stock + 1 crypto
    # Every row carries a timestamp — including the rejected one (no entry_at) —
    # and it MUST carry a UTC offset, or the browser parses the offset-less
    # string as local time and shows the UTC wall-clock mislabeled as local.
    assert all(r["logged_at"] for r in all_rows)
    assert all(r["logged_at"].endswith("+00:00") or r["logged_at"].endswith("Z") for r in all_rows)

    trades = mine(client.get("/api/engine/journal?status=trades").json())
    assert {r["status"] for r in trades} == {"open", "closed"}  # rejected excluded

    rejected = mine(client.get("/api/engine/journal?status=rejected").json())
    assert rejected and all(r["status"] == "rejected" for r in rejected)

    crypto = mine(client.get("/api/engine/journal?asset_class=crypto").json())
    assert {r["symbol"] for r in crypto} == {"BTC/USD"}
    stocks = mine(client.get("/api/engine/journal?asset_class=stock").json())
    assert len(stocks) == 3 and all(r["asset_class"] == "stock" for r in stocks)
    # filters compose: crypto + executed-only
    combo = mine(client.get("/api/engine/journal?asset_class=crypto&status=trades").json())
    assert [r["symbol"] for r in combo] == ["BTC/USD"]

    with session_scope() as s:
        s.query(Trade).delete()
    client.delete(f"/api/strategies/{sid}")
