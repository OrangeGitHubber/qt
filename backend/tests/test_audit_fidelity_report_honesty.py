"""The instrument's own honesty: does the fidelity report ever say a thing its
evidence does not support?

Every case here came out of one real run — strategy 25, "Basic FAANGs and friends
- optimized 2 aug", a stock strategy over an 11-symbol custom universe, segmented
into a silent stretch and a stretch that traded five times. The report it produced
made four claims of a kind this file exists to prevent:

  * a NO-TRADE EXPLANATION beside `backtest_trades: 5` — a sentence naming a rule
    ("try disabling the VWAP rule") that was true of one silent stretch and quoted
    over a comparison that traded;
  * a PRIOR-LOSS SEED listing 29 symbols, eleven of them crypto pairs, for a stock
    strategy that would never evaluate one of them;
  * `symbol_bars_unrankable: 29` — a tenth of the sample — presented as a bare
    integer with no stated consequence, when the two possible consequences are
    opposite in seriousness;
  * `suggested_spread_pct: 0.3345` emitted beside `enough_to_judge: false` at two
    fills, which the panel renders as an instruction to change a setting that
    biases every backtest the app runs.

The shape they share is that the report was CONFIDENT past its evidence. A wrong
number here is worse than no number, because this page is what decides whether
every other number in the app gets believed.

Symbols are unique to this file: the endpoint fetches through the real bar cache,
which lives for the whole session.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from qt import security
from qt.api.fidelity import _ranking_report
from qt.broker.alpaca import SECRET_KEY_ID, SECRET_KEY_SECRET, AlpacaClient
from qt.db import session_scope
from qt.models import Strategy, StrategyConfigVersion, Trade
from qt.services import backtest as bt
from qt.services.fidelity import compare

NOW = datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# 1. A NO-TRADE EXPLANATION FOR A RUN THAT TRADED
# ---------------------------------------------------------------------------


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


def _strategy_body(min_gain: float, symbols: list[str], name: str) -> dict:
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
    """Daily bars for 35 days: flat at 100, then `to_price` from `rise_days_ago`
    onward. `rise_days_ago=0` never rises at all."""
    out = []
    for n in range(35, 0, -1):
        c = 100.0 if n > rise_days_ago else to_price
        ts = (NOW - timedelta(days=n)).replace(hour=14, minute=0, second=0, microsecond=0)
        out.append({"t": ts.strftime("%Y-%m-%dT%H:%M:%SZ"), "o": c, "h": c, "l": c,
                    "c": c, "v": 1000, "vw": c})
    return out


def _add_trade(sid: int, symbol: str, entry_days_ago: int) -> None:
    with session_scope() as s:
        s.add(Trade(
            strategy_id=sid, mode="paper", symbol=symbol, asset_class="stock",
            status="open", qty=10, notional=1000, entry_price=100.0,
            entry_reason="gain",
            entry_at=(NOW - timedelta(days=entry_days_ago)).replace(hour=14, minute=5),
        ))


def _backdate_versions(sid: int, ages: list[int]) -> None:
    with session_scope() as s:
        rows = (
            s.query(StrategyConfigVersion)
            .filter_by(strategy_id=sid)
            .order_by(StrategyConfigVersion.version_no)
            .all()
        )
        for row, age in zip(rows, ages):
            row.created_at = NOW - timedelta(days=age)


def _compare(client, sid: int, bars: dict[str, list[dict]], **extra) -> dict:
    with patch.object(AlpacaClient, "historical_bars", new=AsyncMock(return_value=bars)):
        response = client.post(
            "/api/fidelity/compare", json={"strategy_id": sid, "days": 30, **extra}
        )
    assert response.status_code == 200, response.text
    return response.json()


@pytest.fixture()
def split_run(client, configured) -> dict:
    """Strategy 25's exact shape: a window cut in two, where the FIRST stretch
    holds most of the real trades and replayed nothing, and the second replayed
    something.

    NTRA never moves, so the first stretch's replay finds no qualifying bar and
    the backtester writes a diagnosis for it. NTRB rises 8% inside the second
    stretch, so that one trades. The report-wide reason is chosen from whichever
    stretch has the most LIVE trades — which is the silent one — and that is how a
    no-trade sentence came to be printed over a comparison that traded."""
    sid = client.post(
        "/api/strategies", json=_strategy_body(3, ["NTRA", "NTRB"], "no-trade leak")
    ).json()["id"]
    client.put(f"/api/strategies/{sid}", json=_strategy_body(6, ["NTRA", "NTRB"], "no-trade leak"))
    _backdate_versions(sid, [25, 12])
    _add_trade(sid, "NTRA", entry_days_ago=20)
    _add_trade(sid, "NTRA", entry_days_ago=18)
    _add_trade(sid, "NTRB", entry_days_ago=6)
    return _compare(client, sid, {"NTRA": _bars(0, 100.0), "NTRB": _bars(6, 108.0)})


def test_a_run_that_traded_carries_no_no_trade_explanation(split_run):
    """The contradiction as reported: `backtest_no_trade_reason` describing why
    nothing was bought, beside a decision count saying five things were.

    The reader acts on that string — it names a rule and tells him to switch it
    off — so it is worse than a missing field. It is only ever true of a run that
    opened nothing."""
    body = split_run
    assert body["config"]["segmented"] is True, "the fixture must actually split"
    assert body["decision"]["backtest_trades"] > 0, "and the replay must have traded"
    assert body["backtest_no_trade_reason"] is None


def test_the_silent_stretchs_reason_survives_on_the_stretch_it_describes(split_run):
    """Dropping the report-wide string must not throw the diagnosis away. It is a
    true and useful sentence — about ONE stretch — so it travels on that stretch's
    row, where it cannot be read as a verdict on the whole comparison."""
    rows = split_run["config"]["segments"]
    silent = [r for r in rows if r["backtest_trades"] == 0]
    assert silent, "the fixture must hold a stretch that replayed nothing"
    assert all(r["no_trade_reason"] for r in silent)


def test_a_stretch_that_traded_carries_no_reason_because_there_is_none_to_carry(split_run):
    """The invariant the report-level gate is built on, pinned where it can be
    seen: the backtester writes `diagnosis.summary` only for a run that opened
    nothing, and sets it to None otherwise (services/backtest.py, the `else`
    beside the summary ladder).

    Guarding for this a second time in _segment_rows was tried and could not be
    made to fail, so the guard was removed and this test took its place. If the
    backtester ever starts explaining a run that traded, the leak this file exists
    to close reopens on the segment rows — and this is what notices."""
    traded = [r for r in split_run["config"]["segments"] if r["backtest_trades"] > 0]
    assert traded, "the fixture must hold a stretch that replayed something"
    assert all(r["no_trade_reason"] is None for r in traded)


# ---------------------------------------------------------------------------
# 2. A PRIOR-LOSS SEED FULL OF SYMBOLS THE REPLAY WILL NEVER SEE
# ---------------------------------------------------------------------------


def _loss(sid: int, symbol: str, when: datetime) -> None:
    with session_scope() as s:
        s.add(Trade(
            strategy_id=sid, mode="paper", symbol=symbol,
            asset_class="crypto" if "/" in symbol else "stock",
            status="closed", qty=1, notional=100, entry_price=100.0, exit_price=90.0,
            pnl=-10.0, entry_reason="gain", exit_reason="stop-loss: -10%",
            entry_at=when - timedelta(hours=1), exit_at=when,
        ))


@pytest.fixture()
def seeded_run(client, configured) -> tuple[dict, list]:
    """A stock strategy over one symbol, and an account that lost on two: the one
    it trades, and a crypto pair it has never heard of.

    Both losses are inside the 31-day wash-sale horizon, which is what made the
    real list 29 rows long — it reaches far past the 24h cooldown and sweeps up
    everything the account lost on in a month. Returns the report and every
    `prior_loss_at` the simulator was actually handed, because the fix must be to
    the REPORT and not to the seed."""
    sid = client.post(
        "/api/strategies", json=_strategy_body(3, ["RLIN"], "rail scope")
    ).json()["id"]
    other = client.post(
        "/api/strategies", json=_strategy_body(3, ["RLIN"], "rail scope other")
    ).json()["id"]
    _add_trade(sid, "RLIN", entry_days_ago=8)
    opens = (NOW - timedelta(days=8)).replace(hour=0, minute=0, second=0, microsecond=0)
    _loss(other, "RLIN", opens - timedelta(hours=2))
    _loss(other, "BTC/USD", opens - timedelta(days=5))

    seen: list = []
    real = bt.run_backtest

    def spy(*args, **kwargs):
        seen.append(kwargs.get("prior_loss_at"))
        return real(*args, **kwargs)

    with patch.object(bt, "run_backtest", side_effect=spy):
        body = _compare(client, sid, {"RLIN": _bars(8, 108.0)})
    return body, seen


def test_a_loss_on_a_symbol_this_replay_trades_is_still_reported(seeded_run):
    """The row that matters, and the reason the section exists at all: another
    strategy lost on RLIN two hours before the window, the account-wide cooldown
    was still running, and the replay had to start knowing it."""
    body, _ = seeded_run
    assert [s["symbol"] for s in body["rails"]["seeded_losses"]] == ["RLIN"]


def test_a_loss_on_a_symbol_outside_the_universe_is_counted_not_listed(seeded_run):
    """BTC/USD cannot refuse a single entry in a stock strategy's replay — the
    replay never evaluates it. Twenty-nine such rows buried the one that could,
    which is a report failing at the only job it has. The count stays, because
    "some were omitted" is information and a silent filter is not."""
    body, _ = seeded_run
    listed = [s["symbol"] for s in body["rails"]["seeded_losses"]]
    assert "BTC/USD" not in listed
    assert body["rails"]["seeded_losses_outside_universe"] == 1


def test_the_seed_itself_is_still_account_wide_because_the_live_rail_is(seeded_run):
    """THE claim the fix rests on, and the one that would quietly break it.

    engine._build_rail_context takes max(exit_at) over every closed losing trade
    for the symbol in this mode with NO strategy filter, and the note above
    check_rails says the cooldown and the wash-sale guard stay portfolio-wide
    whatever a strategy is set to. So a loss another strategy took really does
    block this one, the seed is right to be global, and only its PRESENTATION was
    wrong. Narrowing the seed to this strategy would silently un-block the replay
    and turn correctly-refused entries into "trades the backtest invented"."""
    _, seen = seeded_run
    assert seen, "the simulator must have been called"
    assert all("RLIN" in (s or {}) for s in seen)
    assert any("BTC/USD" in (s or {}) for s in seen), (
        "the seed stays account-wide; only the report is scoped"
    )


def test_the_rails_section_declares_what_it_still_cannot_seed(seeded_run):
    """Everything the replay cannot reproduce has to be declared, or a surviving
    mismatch gets blamed on the backtester. Two of the four originally listed
    here — other strategies' positions, and the account-wide position/exposure
    caps — ARE seeded now (see _account_positions), so they moved to `seeded`.
    The remaining two genuinely cannot be, and both push the same way: the
    replay has room the live account did not."""
    body, _ = seeded_run
    said = " ".join(body["rails"]["not_seeded"]).lower()
    for missing in ("daily trade limit", "kill switch", "non-fill"):
        assert missing in said, f"undeclared: {missing}"


def test_the_rails_section_does_not_still_disown_what_it_now_seeds(seeded_run):
    """A stale caveat is worse than none: it tells the reader to discount a
    difference that has already been removed."""
    body, _ = seeded_run
    still_disowned = " ".join(body["rails"]["not_seeded"]).lower()
    claimed = " ".join(body["rails"]["seeded"]).lower()
    assert "other strategies" not in still_disowned
    assert "open-position cap" not in still_disowned
    assert "other strategies" in claimed
    assert "open-position cap" in claimed


# ---------------------------------------------------------------------------
# 3. AN UNRANKABLE SYMBOL-BAR, AND WHICH OF THE TWO THINGS IT MEANS
# ---------------------------------------------------------------------------


def _ranking(**over) -> list[dict]:
    return [{"ranking": {
        "applied": True, "rank_by": "return_30d", "top_n": 5, "pool_size": 11,
        "symbol_bars_ranked": 261, "symbol_bars_cut": 116,
        "symbol_bars_unrankable": 29, "benchmark_missing": False, "warning": None,
        **over,
    }}]


def test_an_unrankable_bar_is_reported_as_dropped_rather_than_left_ambiguous():
    """Strategy 25 reported 261 ranked, 116 cut and 29 unrankable, and nothing
    said which of the two possible meanings the third one had. They are opposite
    in seriousness: passed through as though ranked, it is a hole in the top-N cut
    and exactly how a replay invents a trade live would never have looked at;
    dropped, the pool quietly shrinks. A tenth of the sample deserves better than
    a bare integer."""
    effect = _ranking_report(_ranking())["unrankable_effect"]
    assert "DROPPED" in effect
    assert "11.1%" in effect, "a tenth of the sample, stated as such"
    assert "cannot invent a trade" in effect


def test_the_unrankable_bars_are_not_double_counted_against_the_cut():
    """The other misreading a bare pair of integers invites. `excluded` is
    `len(bars) - len(ranked)` and the unrankable never reach `ranked`, so 29 is
    INSIDE the 116, not beside it — 145 would otherwise look like the pool."""
    assert "already counted inside the 116" in _ranking_report(_ranking())["unrankable_effect"]


def test_a_run_with_nothing_unrankable_says_nothing_about_it():
    """A caveat printed when it does not apply is noise, and noise is how the one
    that mattered got buried in the first place."""
    report = _ranking_report(_ranking(symbol_bars_unrankable=0))
    assert report["unrankable_effect"] is None
    assert report["symbol_bars_ranked"] == 261, "the raw counters still travel"


def test_the_claim_that_an_unrankable_bar_is_dropped_is_true_of_the_ranker():
    """The prose above asserts a fact about someone else's code, so pin it here:
    if ranking ever starts letting a None through, this report becomes a lie in
    the more dangerous direction and something has to fail."""
    from qt.services.ranking import rank_symbols

    ranked = rank_symbols(
        {"A": {"return_30d": 5.0}, "B": {"return_30d": None}}, "return_30d", 5
    )
    assert [symbol for symbol, _ in ranked] == ["A"]


# ---------------------------------------------------------------------------
# 4. VERDICTS DELIVERED WITHOUT A COMPARISON
# ---------------------------------------------------------------------------


def _live(symbol, day, **over):
    row = {"symbol": symbol, "entry_day": day, "status": "closed", "entry_price": 100.0,
           "exit_price": 110.0, "exit_day": "2026-05-06", "pnl": 10.0,
           "entry_reason": "gain 5%", "exit_reason": "take-profit: +10%"}
    return {**row, **over}


def _sim(symbol, day, **over):
    row = {"symbol": symbol, "entry_day": day, "entry_price": 100.0, "exit_price": 110.0,
           "exit_day": "2026-05-06", "pnl": 10.0, "exit_reason": "take-profit: +10%"}
    return {**row, **over}


def test_a_position_both_sides_still_hold_is_not_an_exit_disagreement():
    """`exit_day_matches` demands two real days, so None-vs-None read as False and
    a trade nobody has sold was scored as an exit the replay got wrong. A report
    where nothing had been sold yet came out at 0% exit-day agreement — a verdict
    reached without a comparison, and the harshest one available."""
    out = compare(
        [_live("AAVE/USD", "2026-08-03", status="open", exit_day=None,
               exit_price=None, pnl=None, exit_reason="")],
        {"trade_list": [], "open_positions": [
            _sim("AAVE/USD", "2026-08-03", exit_day=None, exit_price=None,
                 pnl=None, exit_reason=None)]},
    )
    d = out["decision"]
    assert d["matched"] == 1, "the entry still matched"
    assert d["same_exit_day_pct"] is None, "no exits, so no percentage"
    assert d["exits_compared"] == 0
    assert d["both_still_open"] == 1


def test_an_unsold_position_is_left_out_of_the_exit_day_denominator():
    """One agreeing exit and one position both sides still hold is 100% agreement
    over the exits that exist, not 50%. Counting the open one halves a score on
    the strength of a sale neither side made."""
    out = compare(
        [_live("AAA", "2026-05-05"),
         _live("BBB", "2026-05-05", status="open", exit_day=None, exit_price=None,
               pnl=None, exit_reason="")],
        {"trade_list": [_sim("AAA", "2026-05-05")],
         "open_positions": [_sim("BBB", "2026-05-05", exit_day=None, exit_price=None,
                                 pnl=None, exit_reason=None)]},
    )
    assert out["decision"]["same_exit_day_pct"] == 100.0
    assert out["decision"]["exits_compared"] == 1


def test_a_same_day_exit_with_one_reason_missing_does_not_claim_they_agree():
    """The log's fallback was `sim_exit_reason or 'same reason'`, so a row whose
    reasons were never compared — one side recorded none — went out reading "Both
    sold NVDA, same reason." `exit_reason_matches` is None exactly there. Saying
    two things agree when one of them is unknown is the fault this whole file is
    about, in its smallest form."""
    out = compare(
        [_live("NVDA", "2026-05-05")],
        {"trade_list": [_sim("NVDA", "2026-05-05", exit_reason=None)], "open_positions": []},
    )
    assert out["matched"][0]["exit_reason_matches"] is None
    sold = next(r for r in out["log"] if r["action"] == "sold")
    assert sold["verdict"] == "same day, reason unknown"
    assert "same reason" not in sold["detail"]
    assert "weren't compared" in sold["detail"]


def test_a_symbol_traded_twice_in_one_day_is_declared_rather_than_dropped():
    """Matching is by (symbol, day), so a second trade on the same name the same
    day overwrites the first and vanishes — from `matched`, from `live_only`, from
    the log and from `live_trades`. The report then quietly describes fewer
    decisions than the journal holds, which is the sample silently shrinking
    under a reader who has no way to notice.

    Changing the key is not the answer: a live fill at 14:03 and a 14:00 bar are
    the same decision, and demanding equal timestamps would report every trade as
    a mismatch. So the loss is counted and stated."""
    out = compare(
        [_live("SOL/USD", "2026-05-05", entry_price=100.0),
         _live("SOL/USD", "2026-05-05", entry_price=140.0)],
        {"trade_list": [_sim("SOL/USD", "2026-05-05")], "open_positions": []},
    )
    assert out["decision"]["live_trades"] == 1, "the collapse itself is unchanged"
    assert out["same_day_duplicates"]["live"] == 1, "…but it is no longer silent"
    assert out["same_day_duplicates"]["backtest"] == 0


def test_a_replay_that_trades_a_name_twice_in_a_day_is_declared_too():
    """The same hole on the other side. A fix that landed on the live dict and
    not the sim one would hide exactly the case that matters most — invented
    trades are what the top-N and rail questions turn on."""
    out = compare(
        [],
        {"trade_list": [_sim("SOL/USD", "2026-05-05", entry_price=100.0),
                        _sim("SOL/USD", "2026-05-05", entry_price=140.0)],
         "open_positions": []},
    )
    assert out["decision"]["backtest_trades"] == 1
    assert out["same_day_duplicates"]["backtest"] == 1
    assert out["same_day_duplicates"]["live"] == 0


def test_two_recorded_reasons_that_do_agree_are_still_reported_as_a_match():
    """The positive control. Withholding the verdict when the evidence IS there
    would be the same failure pointing the other way."""
    out = compare(
        [_live("NVDA", "2026-05-05", exit_reason="stop-loss: -4.10% <= -4%")],
        {"trade_list": [_sim("NVDA", "2026-05-05", exit_reason="stop-loss: -4.32% <= -4%")],
         "open_positions": []},
    )
    sold = next(r for r in out["log"] if r["action"] == "sold")
    assert sold["verdict"] == "match"
    assert "stop-loss" in sold["detail"]
