"""Chunking has to reach the REPLAY — and a stretch that never ran must say so.

`_chunk_for_minute_replay` shipped in 70d6c4c with eight unit tests, six killed
mutations, and not one line of coverage through `POST /api/fidelity/compare`.
It did not fix the symptom it was written for. Strategy 25 was re-compared the
next morning and returned the identical verdicts — MSFT 09:58, AMZN 10:01 and
SPY 11:25 all "the replay missed it — this is the kind that points at a real
bug" — and the owner's note was **"it ran fast"**, which a cold minute cache
over two chunks cannot be.

The cause is in this file's first test. Chunking produced its pieces correctly
and `compare` threw them away: the replay path was gated on how many
CONFIGURATION stretches there were, and strategy 25 had never been edited, so
one stretch meant one replay over the whole 30-hour window at 15-minute bars.
The chunks were computed, counted, and discarded.

Everything here goes through the endpoint and watches what the replay is
actually asked for, because that is the only thing the unit tests could not see.

Also pinned here, and independent of the cause: a stretch whose replay ERRORED
must never have its live trades reported as "the replay was watching this
symbol and passed". That sentence sends someone after a signal bug that does
not exist, in a window nothing ever looked at.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from qt import security
from qt.api import fidelity as fidelity_api
from qt.api.backtest import MAX_HOURS_FOR_MINUTE_REPLAY
from qt.api.fidelity import MAX_MINUTE_CHUNKS
from qt.broker.alpaca import SECRET_KEY_ID, SECRET_KEY_SECRET, AlpacaClient
from qt.db import session_scope
from qt.models import Strategy, StrategyConfigVersion, Trade
from qt.services import barcache

UTC = timezone.utc
CAP = timedelta(hours=MAX_HOURS_FOR_MINUTE_REPLAY)
SYMBOL = "CHK/USD"


@pytest.fixture()
def cache(monkeypatch):
    """An empty in-memory bar cache wired in as the global one, so every bar the
    replay sees comes from the fake broker below rather than from whatever an
    earlier test left on disk."""
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False},
                        poolclass=StaticPool)
    barcache.CacheBase.metadata.create_all(eng)
    monkeypatch.setattr(barcache, "_engine", eng)
    monkeypatch.setattr(barcache, "_Session", sessionmaker(bind=eng, expire_on_commit=False))


@pytest.fixture()
def configured(client):
    """Defined here rather than imported: CI runs pytest from the repo root,
    where `tests` is not an importable package."""
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


# --- the fake broker ---------------------------------------------------------


def _bar(at: datetime, close: float = 100.0) -> dict:
    return {"t": at.strftime("%Y-%m-%dT%H:%M:%SZ"), "o": close, "h": close,
            "l": close, "c": close, "v": 1e5, "vw": close}


async def _bars(self, symbols, asset_class, timeframe, start, end=None):
    """Flat bars for whatever is asked for, at the resolution asked for.

    Flat on purpose: no test here is about whether the replay TRADES. They are
    about which window it was handed and at what resolution, and a series that
    triggers entries would put the replay's own decisions between the assertion
    and the thing being asserted."""
    first = datetime.fromisoformat(start.replace("Z", "+00:00"))
    last = datetime.now(UTC) + timedelta(days=1)
    step = {"1Day": timedelta(days=1), "1Hour": timedelta(hours=1),
            "15Min": timedelta(minutes=15)}.get(timeframe, timedelta(minutes=5))
    if timeframe == "1Day":
        first = first.replace(hour=0, minute=0, second=0, microsecond=0)
    out, cursor = [], first
    while cursor < last:
        out.append(_bar(cursor))
        cursor += step
    return {s: list(out) for s in symbols}


# --- the comparison, and every replay it ran ---------------------------------


class _Replay:
    """One call into the backtest, as the assertions want to read it."""

    def __init__(self, body):
        self.timeframe = body.timeframe
        self.start, self.end = body.window()

    def covers(self, at: datetime) -> bool:
        return self.start <= at < self.end

    @property
    def hours(self) -> float:
        return (self.end - self.start).total_seconds() / 3600

    def __repr__(self) -> str:
        return (f"<{self.timeframe} {self.start:%m-%d %H:%M}→{self.end:%m-%d %H:%M} "
                f"({self.hours:.1f}h)>")


def _strategy(client, name: str) -> int:
    """A crypto strategy on a fixed one-name universe, with the VWAP rule on so
    the replay is INTRADAY at any window length — which makes every assertion
    below about the bar SIZE and never about intraday-versus-daily."""
    return client.post("/api/strategies", json={
        "name": name, "asset_class": "crypto", "universe": "custom",
        "symbols": [SYMBOL], "preset": "custom",
        "params": {
            "entry": {"min_day_gain_pct": 3.0, "require_above_vwap": True,
                      "entry_window_start": None, "entry_window_end": None},
            "exit": {"trailing_stop_pct": 5, "stop_loss_pct": 4, "take_profit_pct": 0,
                     "max_holding_hours": 0, "flatten_before_close": False,
                     "exit_below_vwap": False},
        },
        "sizing_usd": 100, "sleeve_usd": 1000, "max_positions": 3,
        "swing_mode": True, "ignore_regime": True,
    }).json()["id"]


def _trade(sid: int, at: datetime, *, symbol: str = SYMBOL, closed_at=None) -> None:
    with session_scope() as s:
        s.add(Trade(
            strategy_id=sid, mode="paper", symbol=symbol, asset_class="crypto",
            status="closed" if closed_at else "open", qty=10, notional=100,
            entry_price=100.0, exit_price=101.0 if closed_at else None,
            pnl=10.0 if closed_at else None,
            entry_reason="gain", exit_reason="take-profit" if closed_at else None,
            entry_at=at, exit_at=closed_at,
        ))


def _compare(client, sid: int, start: datetime, end: datetime, *, fail_from=None):
    """The comparison, plus every replay it ran.

    `fail_from` makes the replay of the stretch starting at that moment raise —
    which is what a stretch whose bars could not be loaded really does (see
    `_replay_segments`, which records the failure and carries on)."""
    seen: list[_Replay] = []
    real_run, real_replay = fidelity_api.run, fidelity_api.replay

    async def run_spy(body, *args, **kwargs):
        seen.append(_Replay(body))
        return await real_run(body, *args, **kwargs)

    async def replay_spy(body, *args, **kwargs):
        seen.append(_Replay(body))
        if fail_from is not None and body.window()[0] >= fail_from:
            raise HTTPException(status_code=422, detail="No bars for this stretch.")
        return await real_replay(body, *args, **kwargs)

    with patch.object(AlpacaClient, "historical_bars", new=_bars), \
            patch.multiple(fidelity_api, run=run_spy, replay=replay_spy):
        response = client.post("/api/fidelity/compare", json={
            "strategy_id": sid, "mode": "paper",
            "window_start": start.isoformat(), "window_end": end.isoformat(),
        })
    assert response.status_code == 200, response.text
    return response.json(), seen


def _window(hours: float) -> tuple[datetime, datetime]:
    """A window of `hours` ending at yesterday's 23:00, so every day it touches
    has a completed daily bar behind it."""
    end = (datetime.now(UTC) - timedelta(days=1)).replace(
        hour=23, minute=0, second=0, microsecond=0
    )
    return end - timedelta(hours=hours), end


# ---------------------------------------------------------------------------
# 1. THE DIAGNOSIS — chunking reaching the replay at all
# ---------------------------------------------------------------------------


def test_a_never_edited_window_past_the_cap_replays_the_trade_at_minute_bars(
    client, configured, cache
):
    """Strategy 25's exact shape: one configuration, never edited, and a window
    that grew past a day while the strategy stayed switched on.

    The whole window cannot have minute bars — that is what the cap says — but
    the piece holding the trade can, and that piece is the only part of the
    window this comparison has anything to say about. A 15-minute replay samples
    fifteen times less often than the engine it is grading, and the three trades
    that started this were all bought between two of its bars."""
    start, end = _window(30)
    sid = _strategy(client, "chunk never edited")
    bought_at = start + timedelta(hours=2)
    _trade(sid, bought_at)

    report, replays = _compare(client, sid, start, end)

    holding = [r for r in replays if r.covers(bought_at)]
    assert holding, f"no replay covered the live trade at all — replays: {replays}"
    assert [r.timeframe for r in holding] == ["1Min"], (
        f"the stretch holding the live trade was replayed at "
        f"{[r.timeframe for r in holding]}, not at 1Min. Replays: {replays}. "
        "One replay spanning the whole window means the chunks were computed and "
        "discarded — the split is gated on CONFIG stretches, and this strategy "
        "was never edited."
    )
    assert report["timeframe"] == "1Min", report["timeframe"]


def test_the_hold_back_is_floored_to_the_bar_the_stretch_will_really_use(
    client, configured, cache
):
    """The replay may not open before the engine did, and the floor is the start
    of the bar CONTAINING go-live — so it has to be the bar the stretch is
    actually replayed at.

    Floored at 15 minutes and then replayed at one, the backtest is handed up to
    fourteen minutes the engine never had, and whatever it buys in them comes
    back as a trade it invented. That is the exact false verdict the hold-back
    exists to prevent, and chunking is what put the two out of step: the floor
    was chosen from the whole window's length, the replay from the chunk's."""
    start, end = _window(30)
    sid = _strategy(client, "chunk hold-back")
    # :07 past the hour — not on a 15-minute boundary, so a floor taken at the
    # coarser size lands six minutes early and is visible.
    bought_at = (start + timedelta(hours=2)).replace(minute=7, second=18, microsecond=0)
    _trade(sid, bought_at)

    _, replays = _compare(client, sid, start, end)

    opening = min(replays, key=lambda r: r.start)
    assert opening.start == bought_at.replace(second=0, microsecond=0), (
        f"the replay opened at {opening.start}, the engine at {bought_at} — "
        f"{(bought_at - opening.start).total_seconds() / 60:.0f} free minutes. "
        f"Replays: {replays}"
    )


def test_a_trade_free_remainder_is_not_paid_for_in_minute_bars(
    client, configured, cache
):
    """The cost has to track how often you traded, not how long the strategy has
    been switched on. Minute bars run 1,440 per symbol per day, and a stretch
    with no live trade in it has nothing to compare at any resolution."""
    start, end = _window(24 * 6)
    sid = _strategy(client, "chunk remainder")
    bought_at = start + timedelta(hours=2)
    _trade(sid, bought_at)

    _, replays = _compare(client, sid, start, end)

    assert [r.timeframe for r in replays if r.covers(bought_at)] == ["1Min"]
    empty = [r for r in replays if not r.covers(bought_at)]
    assert empty, f"the five trade-free days were not replayed at all: {replays}"
    assert all(r.timeframe != "1Min" for r in empty), (
        f"a trade-free stretch was replayed at minute bars: {replays}"
    )
    assert any(r.hours > MAX_HOURS_FOR_MINUTE_REPLAY for r in empty), (
        f"the trade-free days were cut up rather than left whole: {replays}"
    )


def test_the_report_says_how_much_of_it_was_graded_on_coarser_bars(
    client, configured, cache
):
    """`timeframe` is ONE string for a window that now holds stretches replayed
    at different sizes. Quoting the busiest stretch's size over the whole run
    would say "1Min" about a report whose older half was graded fifteen times
    more coarsely, and a "the replay missed it" over there would be read as a
    signal difference rather than as the resolution artefact it probably is.

    Past MAX_MINUTE_CHUNKS trade-days the oldest are deliberately left coarse —
    this is that cap, said out loud rather than left to be inferred."""
    days = MAX_MINUTE_CHUNKS + 2
    start, end = _window(24 * days)
    sid = _strategy(client, "chunk resolution said")
    for day in range(days):
        _trade(sid, start + timedelta(hours=24 * day + 2))

    report, _ = _compare(client, sid, start, end)

    resolution = report["config"]["resolution"]
    assert resolution["minute_stretches"] == MAX_MINUTE_CHUNKS, resolution
    # The two oldest trade-days fell outside the budget and were coalesced into
    # one coarse stretch, which the report has to admit rather than quoting the
    # busiest stretch's "1Min" over the lot.
    assert resolution["coarser_stretches"] == 1, resolution
    assert resolution["live_trades_graded_coarse"] == 2, resolution


def test_a_window_cut_into_many_pieces_is_still_replayed_piece_by_piece(
    client, configured, cache
):
    """MAX_SEGMENTS is about EDITS: past about eight of them the fresh-account
    reset at each boundary says more than the comparison does, and the honest
    answer is to narrow the window. Counting resolution chunks against it would
    refuse to split a strategy nobody had ever edited, purely for having been
    switched on for a fortnight — and then tell the owner "the configuration
    changed 11 times", which is a lie about something they can check."""
    days = 10
    start, end = _window(24 * days)
    sid = _strategy(client, "chunk many pieces")
    for day in range(days):
        _trade(sid, start + timedelta(hours=24 * day + 2))

    report, replays = _compare(client, sid, start, end)

    assert len(replays) > 8, replays
    assert report["config"]["not_segmented_reason"] is None
    assert report["config"]["segmented"] is False
    assert report["config"]["resolution"]["live_trades_graded_coarse"] == 0


def test_each_chunk_gets_its_own_trades_and_not_the_whole_window_s(
    client, configured, cache
):
    """Found by running chunking through the endpoint for the first time, and
    invisible to every unit test that held the pieces still and read their spans.

    `dataclasses.replace` copies field VALUES, so the pieces cut from one stretch
    were handed the SAME empty list for `live`. Filing a trade under one chunk
    filed it under all of them: every chunk was then seeded with every trade,
    each stretch reported the whole window's trade count as its own, and a
    failure recorded on one chunk reached its neighbours' trades."""
    start, end = _window(30)
    sid = _strategy(client, "chunk own trades")
    _trade(sid, start + timedelta(hours=2))
    _trade(sid, start + timedelta(hours=26))

    report, replays = _compare(client, sid, start, end)

    assert len(replays) == 2, replays
    # One trade fell in each chunk. Shared lists made every chunk hand its whole
    # stretch's journal back, so the same fill arrived once per chunk — which the
    # comparison collapses by (symbol, day) and counts here rather than hiding.
    assert report["same_day_duplicates"]["live"] == 0, (
        "a live trade reached the comparison more than once — the chunks are "
        "sharing one journal"
    )
    assert report["decision"]["live_trades"] == 2


def test_cutting_for_resolution_does_not_claim_the_strategy_was_edited(
    client, configured, cache
):
    """`segmented` means "your configuration changed mid-window" and the UI says
    exactly that beside it. A window cut so it could keep minute bars has not
    been edited at all, and a report that said so would invent edits the owner
    never made — on every comparison older than a day."""
    start, end = _window(30)
    sid = _strategy(client, "chunk not an edit")
    _trade(sid, start + timedelta(hours=2))

    report, replays = _compare(client, sid, start, end)

    assert len(replays) > 1, "this test says nothing unless the window was split"
    assert report["config"]["segmented"] is False
    assert report["config"]["changes"] == []
    assert report["config"]["segments"] == []


# ---------------------------------------------------------------------------
# 2. A STRETCH THAT NEVER RAN — the reporting fault, whatever the cause
# ---------------------------------------------------------------------------


def test_a_trade_in_a_stretch_whose_replay_failed_is_not_called_a_real_bug(
    client, configured, cache
):
    """THE VERDICT THAT COST AN EVENING. "The replay was watching this symbol and
    passed — this is the kind that points at a real bug" is the strongest thing
    this report can say about a trade, and it was being said about windows no
    replay ever ran over.

    The symbol IS in the replayed universe — the stretch that succeeded reported
    it — so nothing about coverage rescues this. What is missing is that the
    stretch this trade fell in errored, which the segment rows have carried all
    along and the per-trade verdict never consulted."""
    start, end = _window(30)
    sid = _strategy(client, "chunk failed stretch")
    early = start + timedelta(hours=2)
    late = start + timedelta(hours=26)      # in the second chunk, which will fail
    _trade(sid, early)
    _trade(sid, late)

    report, replays = _compare(client, sid, start, end, fail_from=start + CAP)

    failed = [r for r in report["timeline"]
              if r["kind"] == "trade" and r["action"] == "bought"
              and r["at"].startswith(late.strftime("%Y-%m-%dT%H:%M"))]
    assert failed, f"the trade in the failed stretch is missing from the log: {report['timeline']}"
    assert "points at a real bug" not in failed[0]["detail"], failed[0]
    assert "watching this symbol and passed" not in failed[0]["detail"], failed[0]
    assert "No bars for this stretch." in failed[0]["detail"], failed[0]


def test_the_trade_in_the_stretch_that_did_run_still_gets_the_real_verdict(
    client, configured, cache
):
    """The control for the test above. Silencing the verdict for every trade
    would be the same fault pointing the other way — and this is the one report
    line worth chasing, so it has to survive."""
    start, end = _window(30)
    sid = _strategy(client, "chunk good stretch")
    early = start + timedelta(hours=2)
    _trade(sid, early)
    _trade(sid, start + timedelta(hours=26))

    report, _ = _compare(client, sid, start, end, fail_from=start + CAP)

    good = [r for r in report["timeline"]
            if r["kind"] == "trade" and r["action"] == "bought"
            and r["at"].startswith(early.strftime("%Y-%m-%dT%H:%M"))]
    assert good, report["timeline"]
    assert good[0]["verdict"] == "replay missed it", good[0]
    assert "watching this symbol and passed" in good[0]["detail"], good[0]
    assert "did not run" not in good[0]["detail"], good[0]
    # …and only ONE of the two trades was excused, not both.
    assert report["decision"]["missed_replay_failed"] == 1, report["decision"]


def test_a_failed_stretch_is_named_on_the_report_rather_than_only_in_a_trade(
    client, configured, cache
):
    """A comparison with a stretch missing is a comparison of less than it says.
    The per-trade verdict only reaches trades that were MADE in that stretch —
    a stretch that failed and held no live trades would otherwise vanish, and
    the replay's silence there would read as agreement."""
    start, end = _window(30)
    sid = _strategy(client, "chunk failure named")
    _trade(sid, start + timedelta(hours=2))

    report, _ = _compare(client, sid, start, end, fail_from=start + CAP)

    failures = report["replay_failures"]
    assert len(failures) == 1, failures
    assert failures[0]["error"] == "No bars for this stretch."
    assert failures[0]["from"] and failures[0]["to"]
