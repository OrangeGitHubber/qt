"""Comparing a window whose configuration changed while it was being traded.

A single replay uses one configuration. Edit a strategy — or the basket it points
at — halfway through the period being compared and no single configuration is
faithful to all of those trades. The report used to say so and stop there. It now
CUTS the window at the moments the configuration changed and replays each stretch
with the settings that were live during it.

What that cannot do is carry state across a cut: each stretch's replay starts
with fresh cash and no open positions. So a trade opened in one stretch and
closed in another is beyond every one of them, and these tests exist mostly to
pin down that it is counted and set aside rather than quietly scored as the
backtester getting an exit wrong.

Symbols are unique per test: the endpoint fetches through the real bar cache,
which lives for the whole session, so two tests sharing a name would serve each
other's prices.
"""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from qt import security
from qt.broker.alpaca import SECRET_KEY_ID, SECRET_KEY_SECRET, AlpacaClient
from qt.db import session_scope
from qt.models import Strategy, StrategyConfigVersion, Trade

NOW = datetime.now(timezone.utc)


@pytest.fixture()
def configured(client):
    with session_scope() as s:
        security.set_secret(s, SECRET_KEY_ID, "k")
        security.set_secret(s, SECRET_KEY_SECRET, "s")
    yield
    with session_scope() as s:
        s.query(Trade).delete()
        s.query(StrategyConfigVersion).delete()
        s.query(Strategy).delete()
        security.delete_secret(s, SECRET_KEY_ID)
        security.delete_secret(s, SECRET_KEY_SECRET)


def _strategy_body(min_gain: float, symbols: list[str], name: str = "segmented") -> dict:
    return {
        "name": name, "asset_class": "stock", "universe": "custom",
        "symbols": symbols, "preset": "custom",
        "params": {
            "entry": {"min_day_gain_pct": min_gain, "require_above_vwap": False,
                      "entry_window_start": None, "entry_window_end": None},
            "exit": {"trailing_stop_pct": 5, "stop_loss_pct": 4, "take_profit_pct": 0,
                     "max_holding_hours": 0, "flatten_before_close": False,
                     "exit_below_vwap": False},
        },
        "sizing_usd": 1000, "sleeve_usd": 5000, "max_positions": 3,
        "swing_mode": True, "ignore_regime": True,
    }


def _bars(rise_days_ago: int, to_price: float) -> list[dict]:
    """Daily bars for the last 35 days: flat at 100, then `to_price` from
    `rise_days_ago` onward — one dateable rise, then no further day-gain."""
    out = []
    for n in range(35, 0, -1):
        c = 100.0 if n > rise_days_ago else to_price
        ts = (NOW - timedelta(days=n)).replace(hour=14, minute=0, second=0, microsecond=0)
        out.append({"t": ts.strftime("%Y-%m-%dT%H:%M:%SZ"), "o": c, "h": c, "l": c,
                    "c": c, "v": 1000, "vw": c})
    return out


def _day(days_ago: int) -> str:
    return (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%d")


def _add_trade(sid: int, symbol: str, entry_days_ago: int, exit_days_ago: int | None):
    with session_scope() as s:
        s.add(Trade(
            strategy_id=sid, mode="paper", symbol=symbol, asset_class="stock",
            status="closed" if exit_days_ago is not None else "open",
            qty=10, notional=1000, entry_price=100.0,
            exit_price=110.0 if exit_days_ago is not None else None,
            pnl=100.0 if exit_days_ago is not None else None,
            entry_reason="gain", exit_reason="take-profit: +10%" if exit_days_ago is not None else "",
            entry_at=(NOW - timedelta(days=entry_days_ago)).replace(hour=14, minute=5),
            exit_at=((NOW - timedelta(days=exit_days_ago)).replace(hour=14, minute=5)
                     if exit_days_ago is not None else None),
        ))


def _backdate_versions(sid: int, ages: list[int]) -> None:
    """Push each config version back to `ages[i]` days ago, oldest version first.
    Versions are written at save time, which in a test is always 'now' — and a
    window is cut at the MOMENT a config changed, so without this every edit
    lands after every trade and nothing is ever split."""
    with session_scope() as s:
        rows = (
            s.query(StrategyConfigVersion)
            .filter_by(strategy_id=sid)
            .order_by(StrategyConfigVersion.version_no)
            .all()
        )
        for row, age in zip(rows, ages):
            row.created_at = NOW - timedelta(days=age)


def _compare(client, sid: int, symbols: dict[str, list[dict]], **extra):
    with patch.object(AlpacaClient, "historical_bars", new=AsyncMock(return_value=symbols)):
        response = client.post(
            "/api/fidelity/compare", json={"strategy_id": sid, "days": 30, **extra}
        )
    assert response.status_code == 200, response.text
    return response.json()


# --- the case the whole feature exists for ----------------------------------


def _edited_strategy(client) -> int:
    """A strategy whose entry bar was raised from 3% to 6% halfway through the
    window. SEGA1 rose 4% before that edit — allowed then, refused now — and
    SEGA2 rose 8% after it, allowed under either. So a comparison that replays
    today's settings over the whole window must miss SEGA1, and one that replays
    each stretch with its own settings must find both."""
    sid = client.post("/api/strategies", json=_strategy_body(3, ["SEGA1", "SEGA2"])).json()["id"]
    client.put(f"/api/strategies/{sid}", json=_strategy_body(6, ["SEGA1", "SEGA2"]))
    _backdate_versions(sid, [25, 12])
    _add_trade(sid, "SEGA1", entry_days_ago=20, exit_days_ago=None)
    _add_trade(sid, "SEGA2", entry_days_ago=6, exit_days_ago=None)
    return sid


A_BARS = {"SEGA1": _bars(20, 104.0), "SEGA2": _bars(6, 108.0)}


def test_a_strategy_edited_mid_window_is_replayed_stretch_by_stretch(client, configured):
    body = _compare(client, _edited_strategy(client), A_BARS)

    config = body["config"]
    assert config["segmented"] is True
    assert len(config["segments"]) == 2
    # Both real trades found, each by the configuration that produced it.
    assert {m["symbol"] for m in body["matched"]} == {"SEGA1", "SEGA2"}
    assert body["live_only"] == []
    assert config["boundary_spanning_trades"] == 0


def test_the_same_trades_compared_unsegmented_miss_the_earlier_one(client, configured):
    """The control, through a real code path rather than a mutation: imported
    trades are never segmented (they come from another instance, whose config
    history this one does not have), so the same journal sent that way gets the
    single-replay treatment — and today's 6% bar refuses SEGA1's 4% day.

    Without this the test above could be passing because the fixture is easy
    rather than because splitting the window did anything."""
    sid = _edited_strategy(client)
    imported = [
        {"symbol": "SEGA1", "status": "open", "entry_day": _day(20), "exit_day": None,
         "entry_price": 100.0, "exit_price": None, "pnl": None,
         "entry_reason": "gain", "exit_reason": ""},
        {"symbol": "SEGA2", "status": "open", "entry_day": _day(6), "exit_day": None,
         "entry_price": 100.0, "exit_price": None, "pnl": None,
         "entry_reason": "gain", "exit_reason": ""},
    ]
    body = _compare(client, sid, A_BARS, imported_trades=imported)

    assert body["config"]["segmented"] is False
    assert [r["symbol"] for r in body["live_only"]] == ["SEGA1"]


# --- what splitting costs, said out loud ------------------------------------


def test_a_trade_that_crosses_a_boundary_is_counted_and_its_exit_set_aside(client, configured):
    """The honest cost of splitting. SEGC1 was bought before the edit and sold
    after it. No stretch's replay can reproduce that: the first stops watching at
    the boundary and the second starts with nothing held. The ENTRY is still a
    real decision and still counts — but scoring the exit would report the exit
    logic as broken when no replay was ever given the chance to run it."""
    sid = client.post("/api/strategies", json=_strategy_body(3, ["SEGC1"], "spanner")).json()["id"]
    client.put(f"/api/strategies/{sid}", json=_strategy_body(6, ["SEGC1"], "spanner"))
    _backdate_versions(sid, [25, 12])
    _add_trade(sid, "SEGC1", entry_days_ago=15, exit_days_ago=9)  # across the 12-day cut

    body = _compare(client, sid, {"SEGC1": _bars(15, 104.0)})

    assert body["config"]["segmented"] is True
    assert body["config"]["boundary_spanning_trades"] == 1
    assert [m["symbol"] for m in body["matched"]] == ["SEGC1"]
    decision = body["decision"]
    assert decision["boundary_spanning_exits"] == 1
    # NOT filed as a hand-closed exit: that is a different claim about a
    # different thing, and conflating them would misdirect the reader.
    assert decision["manual_exits"] == 0
    # With its exit set aside there is nothing left to score, so the exit
    # percentages say "no data" rather than "0% agreement".
    assert decision["same_exit_day_pct"] is None
    assert decision["same_exit_rule_pct"] is None


def test_too_many_edits_declines_to_split_and_says_why(client, configured):
    """Past a handful of cuts the resets say more than the comparison does —
    every stretch starting with a full wallet and no positions. Handing back a
    worse answer confidently is the failure mode this whole page exists against,
    so it declines and explains."""
    sid = client.post("/api/strategies", json=_strategy_body(3, ["SEGE1"], "churn")).json()["id"]
    with session_scope() as s:
        snapshot = json.loads(
            s.query(StrategyConfigVersion).filter_by(strategy_id=sid).one().snapshot
        )
        for n, age in enumerate(range(24, 4, -2), start=2):  # ten more, all inside the window
            s.add(StrategyConfigVersion(
                strategy_id=sid, version_no=n,
                snapshot=json.dumps({**snapshot, "sleeve_usd": 5000 + n}),
                created_at=NOW - timedelta(days=age),
            ))
    _add_trade(sid, "SEGE1", entry_days_ago=20, exit_days_ago=None)

    body = _compare(client, sid, {"SEGE1": _bars(20, 104.0)})

    config = body["config"]
    assert config["segmented"] is False
    assert config["segments"] == []
    assert "fresh account" in config["not_segmented_reason"]
    assert "Narrow the window" in config["not_segmented_reason"]


# --- edits that are not edits ------------------------------------------------


def test_a_rename_does_not_split_the_comparison(client, configured):
    """Every save writes a config version, including one that only changed the
    name. Cutting the window there would pay a boundary's cost — a reset account,
    a trade that can no longer be followed across it — for a change that cannot
    move a single trade."""
    sid = client.post("/api/strategies", json=_strategy_body(3, ["SEGD1"], "before")).json()["id"]
    client.put(f"/api/strategies/{sid}", json=_strategy_body(3, ["SEGD1"], "after"))
    _backdate_versions(sid, [25, 12])
    _add_trade(sid, "SEGD1", entry_days_ago=20, exit_days_ago=None)

    body = _compare(client, sid, {"SEGD1": _bars(20, 104.0)})

    assert body["config"]["segmented"] is False
    assert body["config"]["segments"] == []
    # …and the comparison still works: a rename changes nothing about the replay.
    assert [m["symbol"] for m in body["matched"]] == ["SEGD1"]


# --- the basket, which changes the universe without touching the strategy -----


def test_a_basket_edited_mid_window_splits_it_too(client, configured):
    """A strategy's config version records WHICH basket it uses, never who is in
    it — so a basket edit changes what it trades while its own version stays
    byte-identical. If only config versions were cut on, the whole window would
    replay against today's membership, and every trade made before the edit would
    be judged against a universe it never had."""
    from qt.models import Basket, BasketItem, BasketVersion

    bid = client.post("/api/baskets", json={"name": "Segment Bank"}).json()["id"]
    client.post(f"/api/baskets/{bid}/items", json={"symbol": "SEGF1", "asset_class": "stock"})

    strat = _strategy_body(3, [], "basket segments")
    strat.update({"universe": "basket", "basket_id": bid, "symbols": []})
    sid = client.post("/api/strategies", json=strat).json()["id"]
    # Both the strategy and the basket existed BEFORE the compared window, so the
    # membership at its start is known rather than reconstructed.
    _backdate_versions(sid, [35])
    with session_scope() as s:
        s.query(BasketVersion).filter_by(basket_id=bid).one().created_at = NOW - timedelta(days=35)

    # SEGF2 joins the basket halfway through the window.
    client.post(f"/api/baskets/{bid}/items", json={"symbol": "SEGF2", "asset_class": "stock"})
    with session_scope() as s:
        newest = (
            s.query(BasketVersion).filter_by(basket_id=bid)
            .order_by(BasketVersion.version_no.desc()).first()
        )
        newest.created_at = NOW - timedelta(days=12)

    _add_trade(sid, "SEGF1", entry_days_ago=20, exit_days_ago=None)
    _add_trade(sid, "SEGF2", entry_days_ago=6, exit_days_ago=None)

    body = _compare(
        client, sid, {"SEGF1": _bars(20, 104.0), "SEGF2": _bars(6, 104.0)}
    )
    with session_scope() as s:
        s.query(BasketVersion).filter_by(basket_id=bid).delete()
        s.query(BasketItem).filter_by(basket_id=bid).delete()
        s.query(Basket).filter_by(id=bid).delete()

    config = body["config"]
    assert config["segmented"] is True
    segments = config["segments"]
    assert len(segments) == 2
    # The first stretch was replayed against the one-symbol basket of the time,
    # the second against the two-symbol one — the point of the whole exercise.
    assert [s["symbols"] for s in segments] == [1, 2]
    assert {m["symbol"] for m in body["matched"]} == {"SEGF1", "SEGF2"}


# --- when there are too many edits to split -------------------------------


def test_the_change_log_says_what_moved_and_how_much_traded_after_it():
    """Werner's ask: show the edits alongside the trades. A flat list of 48
    mismatches is unreadable; "the universe widened on the 24th and 30 of them
    are after that" is a finding."""
    from datetime import datetime, timedelta, timezone

    from qt.api.fidelity import _change_log, _Segment

    now = datetime.now(timezone.utc)
    segments = [
        _Segment(start=now - timedelta(days=10), end=now - timedelta(days=6),
                 config={"universe": "basket", "params": {}}, version_no=1,
                 symbols=["MS"], scanner_replay=False, universe_known=True),
        _Segment(start=now - timedelta(days=6), end=now - timedelta(days=4),
                 config={"universe": "scanner", "params": {}}, version_no=2,
                 symbols=[], scanner_replay=True, universe_known=True),
        # A THIRD stretch, so a count that forgot its upper bound would sweep up
        # these later trades and attribute them to the edit before them.
        _Segment(start=now - timedelta(days=4), end=now,
                 config={"universe": "watchlist", "params": {}}, version_no=3,
                 symbols=[], scanner_replay=False, universe_known=True),
    ]
    live = [
        {"entry_day": (now - timedelta(days=8)).strftime("%Y-%m-%d"), "status": "closed"},
        {"entry_day": (now - timedelta(days=5)).strftime("%Y-%m-%d"), "status": "closed"},
        {"entry_day": (now - timedelta(days=2)).strftime("%Y-%m-%d"), "status": "closed"},
        {"entry_day": (now - timedelta(days=1)).strftime("%Y-%m-%d"), "status": "closed"},
    ]
    log = _change_log(segments, live)
    assert len(log) == 2
    assert log[0]["live_trades_after"] == 1   # that stretch only, not everything after
    assert any(c["field"] == "Universe" for c in log[0]["changed"])


def test_the_suggested_window_is_the_busiest_unedited_stretch():
    """With the window too churned to split, the useful next move is a stretch
    BETWEEN edits. Ranked by trades, not by length: a long quiet stretch proves
    less than a short busy one, because the sample size is what the verdict
    rests on."""
    from datetime import datetime, timedelta, timezone

    from qt.api.fidelity import _stable_window, _Segment

    now = datetime.now(timezone.utc)

    def seg(from_days, to_days):
        return _Segment(start=now - timedelta(days=from_days), end=now - timedelta(days=to_days),
                        config={}, version_no=1, symbols=[], scanner_replay=False,
                        universe_known=True)

    def trade(days_ago):
        return {"entry_day": (now - timedelta(days=days_ago)).strftime("%Y-%m-%d"),
                "status": "closed"}

    segments = [seg(40, 20), seg(20, 15), seg(15, 0)]
    # The LONG stretch (20 days) holds 2 trades; the SHORT one (5 days) holds 3.
    # Both clear the minimum, so the two rankings genuinely disagree — with only
    # one of them qualifying, this test would pass either way.
    live = [trade(35), trade(34), trade(18), trade(17), trade(16)]
    best = _stable_window(segments, live)
    assert best["live_trades"] == 3
    assert best["days"] == 5, "ranked by length instead of by trades"
    assert best["start"].startswith((now - timedelta(days=20)).strftime("%Y-%m-%d"))


def test_a_stretch_with_one_trade_is_not_suggested():
    """A window with a single trade is a smaller anecdote, not a better
    comparison — proposing it would just repeat the same emptiness."""
    from datetime import datetime, timedelta, timezone

    from qt.api.fidelity import _stable_window, _Segment

    now = datetime.now(timezone.utc)
    segments = [
        _Segment(start=now - timedelta(days=10), end=now, config={}, version_no=1,
                 symbols=[], scanner_replay=False, universe_known=True)
    ]
    live = [{"entry_day": (now - timedelta(days=5)).strftime("%Y-%m-%d"), "status": "closed"}]
    assert _stable_window(segments, live) is None


def test_an_explicit_window_bounds_the_trades_at_both_ends(client, configured):
    """Naming a past stretch is the point of the suggestion. If the journal read
    stayed open-ended, every trade AFTER that stretch would be counted as one the
    backtest missed — turning the fix into a worse report than the problem."""
    from datetime import datetime, timedelta, timezone
    from unittest.mock import AsyncMock, patch

    from qt.broker.alpaca import AlpacaClient

    sid = client.post("/api/strategies", json=_strategy_body(3, ["WNDW"], "window bounds")).json()["id"]
    now = datetime.now(timezone.utc)
    with session_scope() as s:
        for days_ago in (30, 5):          # one inside the window, one long after
            s.add(Trade(
                strategy_id=sid, mode="paper", symbol="WNDW", asset_class="stock",
                status="closed", qty=10, notional=1000, entry_price=100.0,
                exit_price=110.0, pnl=100.0, entry_reason="gain 5%",
                exit_reason="take-profit: +10%",
                entry_at=now - timedelta(days=days_ago),
                exit_at=now - timedelta(days=days_ago - 1),
            ))

    bars = [{"t": (now - timedelta(days=n)).strftime("%Y-%m-%dT%H:%M:%SZ"),
             "o": 100, "h": 100, "l": 100, "c": 100, "v": 1e6, "vw": 100} for n in (35, 34, 33)]
    with patch.object(AlpacaClient, "historical_bars", new=AsyncMock(return_value={"WNDW": bars})):
        body = client.post("/api/fidelity/compare", json={
            "strategy_id": sid,
            "window_start": (now - timedelta(days=32)).isoformat(),
            "window_end": (now - timedelta(days=20)).isoformat(),
        }).json()
    assert body["decision"]["live_trades"] == 1, "the trade after the window leaked in"


def test_a_backwards_window_is_refused(client, configured):
    sid = client.post("/api/strategies", json=_strategy_body(3, ["WNDW"], "backwards")).json()["id"]
    now = datetime.now(timezone.utc)
    r = client.post("/api/fidelity/compare", json={
        "strategy_id": sid,
        "window_start": now.isoformat(),
        "window_end": (now - timedelta(days=10)).isoformat(),
    })
    assert r.status_code == 422 and "ends before it starts" in r.json()["detail"]


def test_the_window_never_starts_before_the_strategy_traded(client, configured):
    """Asking for 90 days of a strategy that has run for five replays 85 days in
    which it did not exist — and every trade the replay takes there is scored as
    one it invented. Dozens of them, none about the backtester, burying the days
    that are."""
    from unittest.mock import AsyncMock, patch

    from qt.broker.alpaca import AlpacaClient

    sid = client.post("/api/strategies", json=_strategy_body(3, ["CLMP"], "clamped")).json()["id"]
    now = datetime.now(timezone.utc)
    with session_scope() as s:
        s.add(Trade(
            strategy_id=sid, mode="paper", symbol="CLMP", asset_class="stock",
            status="closed", qty=10, notional=1000, entry_price=100.0, exit_price=110.0,
            pnl=100.0, entry_reason="gain 5%", exit_reason="take-profit: +10%",
            entry_at=now - timedelta(days=5), exit_at=now - timedelta(days=4),
        ))

    bars = [{"t": (now - timedelta(days=n)).strftime("%Y-%m-%dT%H:%M:%SZ"),
             "o": 100, "h": 100, "l": 100, "c": 100, "v": 1e6, "vw": 100} for n in (7, 6, 5)]
    with patch.object(AlpacaClient, "historical_bars", new=AsyncMock(return_value={"CLMP": bars})):
        body = client.post("/api/fidelity/compare", json={"strategy_id": sid, "days": 90}).json()

    assert body["window"]["clamped_to_first_trade"] is True
    assert body["window"]["days"] <= 7, "replayed history the strategy never lived through"
    # The window opens on the DAY of the first trade, not at its exact fill time:
    # trades are placed by day, so a mid-afternoon start would drop that day.
    assert body["window"]["start"].endswith("T00:00:00+00:00")


def test_a_window_ending_before_the_strategy_existed_is_refused(client, configured):
    """Better a clear refusal than a comparison of an empty period against a
    replay that had 90 days to invent trades in."""
    sid = client.post("/api/strategies", json=_strategy_body(3, ["CLMP"], "too early")).json()["id"]
    now = datetime.now(timezone.utc)
    with session_scope() as s:
        s.add(Trade(
            strategy_id=sid, mode="paper", symbol="CLMP", asset_class="stock",
            status="closed", qty=10, notional=1000, entry_price=100.0, exit_price=110.0,
            pnl=100.0, entry_reason="gain 5%", exit_reason="take-profit: +10%",
            entry_at=now - timedelta(days=2), exit_at=now - timedelta(days=1),
        ))
    r = client.post("/api/fidelity/compare", json={
        "strategy_id": sid,
        "window_start": (now - timedelta(days=60)).isoformat(),
        "window_end": (now - timedelta(days=30)).isoformat(),
    })
    assert r.status_code == 422
    assert "no paper history" in r.json()["detail"]


def test_the_window_defaults_to_the_strategys_whole_trading_life(client, configured):
    """No day count is asked for. The window that makes sense is "since this
    strategy started trading", the journal already knows it, and asking invites a
    number wrong in the only direction that matters — too long."""
    from unittest.mock import AsyncMock, patch

    from qt.broker.alpaca import AlpacaClient

    sid = client.post("/api/strategies", json=_strategy_body(3, ["DFLT"], "derived")).json()["id"]
    now = datetime.now(timezone.utc)
    # OLDER than any fixed fallback would reach. On a young strategy the clamp
    # produces the same answer either way, so a short-lived fixture cannot tell
    # "derived from the journal" from "a fixed span that got clamped" — and the
    # mutation proving it survives is what showed that.
    with session_scope() as s:
        for days_ago in (200, 3):
            s.add(Trade(
                strategy_id=sid, mode="paper", symbol="DFLT", asset_class="stock",
                status="closed", qty=10, notional=1000, entry_price=100.0, exit_price=110.0,
                pnl=100.0, entry_reason="gain 5%", exit_reason="take-profit: +10%",
                entry_at=now - timedelta(days=days_ago),
                exit_at=now - timedelta(days=days_ago - 1),
            ))

    bars = [{"t": (now - timedelta(days=n)).strftime("%Y-%m-%dT%H:%M:%SZ"),
             "o": 100, "h": 100, "l": 100, "c": 100, "v": 1e6, "vw": 100} for n in (205, 204, 203)]
    with patch.object(AlpacaClient, "historical_bars", new=AsyncMock(return_value={"DFLT": bars})):
        # No `days` at all — the shape the UI now sends.
        body = client.post("/api/fidelity/compare", json={"strategy_id": sid}).json()

    assert body["window"]["days"] >= 199, "did not reach back to the strategy's first trade"
    assert body["window"]["clamped_to_first_trade"] is False  # nothing to clamp — it IS the start
    assert body["decision"]["live_trades"] == 2, "the oldest trade fell outside the window"


def test_the_replay_cannot_trade_the_morning_before_the_engine_went_live():
    """Werner's case. The strategy goes live at 14:30. The same symbol looked
    BETTER at 10:00 that morning — so a replay opening at midnight buys the 10:00
    setup, which the engine, not yet running, never saw, and the report calls it a
    trade the backtest invented.

    The window still has to OPEN at midnight, because trades are matched by day
    and the go-live day's real trades would otherwise fall outside it. So the two
    bounds are separate: the stretch opens at midnight, the replay starts at the
    bar the engine first acted in."""
    from qt.api.fidelity import _bar_floor

    live_at = datetime(2026, 7, 29, 14, 33, 12, tzinfo=timezone.utc)
    assert _bar_floor(live_at, "15Min") == datetime(2026, 7, 29, 14, 30, tzinfo=timezone.utc)
    # Not midnight — that is the six hours of trades the engine never saw...
    assert _bar_floor(live_at, "15Min").hour == 14
    # ...and not the fill time either: the bar that CAUSED the trade opened at
    # 14:30, so gating at 14:33:12 would skip it and lose the trade being judged.
    assert _bar_floor(live_at, "15Min") < live_at


def test_the_bar_floor_matches_the_replays_own_bar_size():
    from qt.api.fidelity import _bar_floor

    live_at = datetime(2026, 7, 29, 14, 47, 30, tzinfo=timezone.utc)
    assert _bar_floor(live_at, "1Hour") == datetime(2026, 7, 29, 14, 0, tzinfo=timezone.utc)
    # A daily replay gets one decision point for the whole day, so there is no
    # morning to steal a march on — the bar IS the day.
    assert _bar_floor(live_at, "1Day") == datetime(2026, 7, 29, 0, 0, tzinfo=timezone.utc)


def test_the_window_still_opens_at_midnight_so_that_days_trades_count(client, configured):
    """The other half. If the WINDOW moved to 14:30 as well, that day's own
    trades — matched by day, so parsed to midnight — would fall outside it and be
    lost, which is the failure this pair of bounds was introduced to avoid."""
    from unittest.mock import AsyncMock, patch

    from qt.broker.alpaca import AlpacaClient

    sid = client.post("/api/strategies", json=_strategy_body(3, ["GOLV"], "go live")).json()["id"]
    now = datetime.now(timezone.utc)
    first = (now - timedelta(days=5)).replace(hour=14, minute=33, second=12, microsecond=0)
    with session_scope() as s:
        s.add(Trade(
            strategy_id=sid, mode="paper", symbol="GOLV", asset_class="stock",
            status="closed", qty=10, notional=1000, entry_price=100.0, exit_price=110.0,
            pnl=100.0, entry_reason="gain 5%", exit_reason="take-profit: +10%",
            entry_at=first, exit_at=first + timedelta(days=1),
        ))

    bars = [{"t": (now - timedelta(days=n)).strftime("%Y-%m-%dT%H:%M:%SZ"),
             "o": 100, "h": 100, "l": 100, "c": 100, "v": 1e6, "vw": 100} for n in (8, 7, 6)]
    with patch.object(AlpacaClient, "historical_bars", new=AsyncMock(return_value={"GOLV": bars})):
        body = client.post("/api/fidelity/compare", json={"strategy_id": sid}).json()

    assert body["window"]["start"].endswith("T00:00:00+00:00"), "the go-live day's trades would be lost"
    assert body["decision"]["live_trades"] == 1
