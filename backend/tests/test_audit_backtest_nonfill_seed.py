"""`check_rails` has a clock now, and the non-fill cooldown is simulated.

Two of the rails are elapsed-time rules — the after-loss cooldown and the
non-fill circuit breaker — and `check_rails` read `datetime.now()` for both. The
replay worked around the first (it nulled `last_loss_at` and re-checked the
cooldown against sim time by hand) and did not model the second AT ALL. So a
replay was optimistic about exactly the symbols live had benched after repeated
"did not fill" attempts, and any buy it made on one came back as a trade the
backtester invented.

The fix is the same one both rails wanted: an explicit clock. The engine passes
the wall clock (unchanged behaviour, and the default if nobody passes one); the
replay passes the timestamp of the bar it is judging. The non-fill EVIDENCE is
then seeded from the journal, because the replay fills every order at the bar
close by construction and can never produce that evidence itself.
"""

from datetime import datetime, timedelta, timezone

from qt.services.backtest import (
    NONFILL_LOOKBACK_DAYS,
    _NonfillLedger,
    run_backtest,
)
from qt.services.engine import (
    RISK_DEFAULTS,
    NONFILL_STRIKES_BEFORE_COOLDOWN,
    RailContext,
    check_rails,
    nonfill_cooldown_hours,
)

D0 = datetime(2026, 5, 1, 14, 0, tzinfo=timezone.utc)
WIDE = dict(
    RISK_DEFAULTS,
    max_total_positions=50, max_total_exposure_usd=1_000_000,
    max_daily_loss_usd=1_000_000, max_trades_per_day=1000, wash_sale_guard="off",
)
CFG = {"max_positions": 5, "sleeve_usd": 5000.0}


def _ctx(**kw) -> RailContext:
    base = dict(
        equity=10_000.0, open_positions_total=0, open_exposure_usd=0.0,
        open_positions_strategy=0, open_exposure_strategy_usd=0.0,
        entries_today=0, already_open_symbol=False, last_loss_at=None,
        loss_sale_within_31d=False, risk=dict(WIDE),
    )
    base.update(kw)
    return RailContext(**base)


# ---------------------------------------------------------------------------
# 1. THE CLOCK IS AN ARGUMENT
# ---------------------------------------------------------------------------


def test_the_after_loss_cooldown_is_measured_from_the_clock_you_pass():
    """The whole point: "would this have been blocked at 14:02 last Tuesday" now
    has an answer, instead of being measured from this afternoon."""
    risk = dict(WIDE, cooldown_hours_after_loss=24)
    ctx = _ctx(last_loss_at=D0, risk=risk)
    ok_inside, why = check_rails(CFG, 100.0, ctx, D0 + timedelta(hours=1))
    assert ok_inside is False and "cooldown after loss" in why
    ok_after, _ = check_rails(CFG, 100.0, _ctx(last_loss_at=D0, risk=risk),
                              D0 + timedelta(hours=25))
    assert ok_after is True


def test_the_non_fill_cooldown_is_measured_from_the_clock_you_pass():
    """Three strikes buys an hour off (nonfill_cooldown_hours), measured from
    the LAST miss — so it lapses at sim time, not at wall-clock time."""
    assert nonfill_cooldown_hours(3) == 1.0, "this test is about the 1h first step"
    ctx = _ctx(nonfill_strikes=3, last_nonfill_at=D0)
    blocked, why = check_rails(CFG, 100.0, ctx, D0 + timedelta(minutes=30))
    assert blocked is False and "non-fills" in why
    freed, _ = check_rails(CFG, 100.0, _ctx(nonfill_strikes=3, last_nonfill_at=D0),
                           D0 + timedelta(hours=2))
    assert freed is True


def test_two_strikes_do_not_bench_anything():
    """Anti-vacuity for the test above — the STRIKE COUNT is what arms the rail,
    not merely having a `last_nonfill_at`. A genuine blip costs nothing."""
    assert NONFILL_STRIKES_BEFORE_COOLDOWN == 3
    ok, _ = check_rails(CFG, 100.0, _ctx(nonfill_strikes=2, last_nonfill_at=D0),
                        D0 + timedelta(minutes=1))
    assert ok is True


def test_omitting_the_clock_still_means_the_wall_clock():
    """The live engine's behaviour must not have moved: with no `now`, the rails
    are judged against right now, exactly as before the parameter existed."""
    just_now = datetime.now(timezone.utc) - timedelta(minutes=10)
    blocked, why = check_rails(CFG, 100.0, _ctx(nonfill_strikes=3, last_nonfill_at=just_now))
    assert blocked is False and "non-fills" in why
    long_ago = datetime.now(timezone.utc) - timedelta(days=3)
    ok, _ = check_rails(CFG, 100.0, _ctx(nonfill_strikes=3, last_nonfill_at=long_ago))
    assert ok is True


# ---------------------------------------------------------------------------
# 2. THE LEDGER — LIVE'S COUNTING RULE, RESTATED FOR SIM TIME
# ---------------------------------------------------------------------------


def _miss(at) -> dict:
    return {"symbol": "AAA", "at": at, "filled": False}


def _fill(at) -> dict:
    return {"symbol": "AAA", "at": at, "filled": True}


def test_consecutive_misses_are_counted_newest_first():
    led = _NonfillLedger([
        _miss(D0 - timedelta(minutes=3)),
        _miss(D0 - timedelta(minutes=2)),
        _miss(D0 - timedelta(minutes=1)),
    ])
    assert led.at("AAA", D0) == (3, D0 - timedelta(minutes=1))


def test_a_fill_ends_the_streak_whatever_came_before_it():
    """Live's words: "it filled — whatever came before is history"."""
    led = _NonfillLedger([
        _miss(D0 - timedelta(minutes=5)),
        _miss(D0 - timedelta(minutes=4)),
        _miss(D0 - timedelta(minutes=3)),
        _fill(D0 - timedelta(minutes=2)),
    ])
    assert led.at("AAA", D0) == (0, None)


def test_a_fill_before_the_streak_does_not_clear_it():
    """Anti-vacuity for the test above: it is the ORDER that matters, not the
    mere presence of a fill."""
    led = _NonfillLedger([
        _fill(D0 - timedelta(minutes=5)),
        _miss(D0 - timedelta(minutes=4)),
        _miss(D0 - timedelta(minutes=3)),
        _miss(D0 - timedelta(minutes=2)),
    ])
    assert led.at("AAA", D0)[0] == 3


def test_events_at_or_after_the_instant_are_invisible():
    """The engine deciding at 14:00 had not yet seen 14:01's miss. Counting it
    would bench a symbol using evidence from the future."""
    led = _NonfillLedger([_miss(D0), _miss(D0 + timedelta(minutes=1)),
                          _miss(D0 + timedelta(minutes=2))])
    assert led.at("AAA", D0) == (0, None)


def test_misses_older_than_the_lookback_are_forgotten():
    """Live's own query looks back 7 days; an ancient miss must not revive a
    streak."""
    old = D0 - timedelta(days=NONFILL_LOOKBACK_DAYS + 1)
    led = _NonfillLedger([_miss(old), _miss(old), _miss(old)])
    assert led.at("AAA", D0) == (0, None)


def test_a_streak_inside_the_lookback_still_counts():
    """Anti-vacuity for the test above."""
    recent = D0 - timedelta(days=NONFILL_LOOKBACK_DAYS - 1)
    led = _NonfillLedger([_miss(recent), _miss(recent), _miss(recent)])
    assert led.at("AAA", D0)[0] == 3


def test_another_symbols_misses_do_not_bench_this_one():
    led = _NonfillLedger([
        {"symbol": "ZZZ", "at": D0 - timedelta(minutes=i), "filled": False}
        for i in (1, 2, 3)
    ])
    assert led.at("AAA", D0) == (0, None)


def test_an_empty_ledger_leaves_the_rail_dormant():
    """Which is what every ordinary backtest gets."""
    assert _NonfillLedger(None).at("AAA", D0) == (0, None)


# ---------------------------------------------------------------------------
# 3. THE REPLAY ACTUALLY REFUSES A BENCHED SYMBOL
# ---------------------------------------------------------------------------


def _strategy() -> dict:
    return {
        "asset_class": "stock", "swing_mode": False,
        "sizing_usd": 500.0, "sleeve_usd": 5000.0, "max_positions": 5,
        "params": {"entry": {"min_day_gain_pct": 1.0}, "exit": {}},
    }


def _bars() -> list[dict]:
    """Two daily bars — one chance to trade, so a refusal cannot be undone by
    the symbol simply entering the day after."""
    out, close = [], 10.0
    for i in range(2):
        ts = D0 + timedelta(days=i)
        out.append({"t": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "o": close, "h": close, "l": close, "c": close, "v": 1000, "vw": close})
        close *= 1.06
    return out


FIRST_TRADE = D0 + timedelta(days=1)


def _entries(result: dict) -> set[str]:
    return ({t["symbol"] for t in result.get("trade_list") or []}
            | {p["symbol"] for p in result.get("open_positions") or []})


def _run(**kw) -> set[str]:
    return _entries(run_backtest(_strategy(), {"AAA": _bars()}, WIDE, **kw))


def test_without_the_seed_the_replay_buys_a_symbol_live_had_benched():
    """The bug, stated as a test: the replay fills every order by construction,
    so it has no reason of its own to refuse this."""
    assert "AAA" in _run()


def test_three_seeded_non_fills_bench_the_symbol_for_the_replay_too():
    misses = [
        {"symbol": "AAA", "at": FIRST_TRADE - timedelta(minutes=m), "filled": False}
        for m in (3, 2, 1)
    ]
    assert "AAA" not in _run(nonfill_events=misses)


def test_a_seeded_fill_after_the_streak_lets_the_replay_in_again():
    """The breaker releases on evidence, not on time alone — and the replay has
    to release with it, or it becomes STRICTER than live."""
    events = [
        {"symbol": "AAA", "at": FIRST_TRADE - timedelta(minutes=m), "filled": False}
        for m in (5, 4, 3)
    ] + [{"symbol": "AAA", "at": FIRST_TRADE - timedelta(minutes=2), "filled": True}]
    assert "AAA" in _run(nonfill_events=events)


def test_the_bench_lapses_once_the_cooldown_has_run_its_course():
    """Three strikes is one hour, measured from the last miss. A streak that
    ended two hours before the bar must not still be blocking it."""
    misses = [
        {"symbol": "AAA", "at": FIRST_TRADE - timedelta(hours=2, minutes=m), "filled": False}
        for m in (3, 2, 1)
    ]
    assert "AAA" in _run(nonfill_events=misses)


# ---------------------------------------------------------------------------
# 4. THE AFTER-LOSS COOLDOWN STILL WORKS, AND NOW SAYS SO IN LIVE'S WORDS
# ---------------------------------------------------------------------------
# The replay used to lift this rail out of check_rails and re-implement it after
# every other rail had passed. For a STOCK that meant the 31-day wash-sale guard
# — which the 24h cooldown always sits inside — answered first, so a cooled-off
# symbol was reported as a wash-sale block. Live would have said cooldown.


def _cooldown_run(guard: str) -> dict:
    risk = dict(WIDE, cooldown_hours_after_loss=24, wash_sale_guard=guard)
    return run_backtest(
        _strategy(), {"AAA": _bars()}, risk,
        prior_loss_at={"AAA": FIRST_TRADE - timedelta(hours=2)},
    )


def test_a_seeded_prior_loss_still_blocks_the_replay():
    assert "AAA" not in _entries(_cooldown_run("off"))


def test_the_block_is_reported_as_a_cooldown_not_as_a_wash_sale():
    """Both rails apply to a stock inside its cooldown; live checks the cooldown
    FIRST, and a diagnosis that names the wrong one sends the reader to change
    the wrong setting."""
    result = _cooldown_run("block")
    said = " ".join((result.get("no_trade_reasons") or {}).values()).lower()
    assert "cooldown" in said, said
    assert "wash-sale" not in said, said


def test_an_expired_prior_loss_does_not_block():
    """Anti-vacuity: the cooldown is measured from SIM time, so a loss older
    than the window has lapsed by the bar being judged."""
    risk = dict(WIDE, cooldown_hours_after_loss=24, wash_sale_guard="off")
    result = run_backtest(
        _strategy(), {"AAA": _bars()}, risk,
        prior_loss_at={"AAA": FIRST_TRADE - timedelta(days=3)},
    )
    assert "AAA" in _entries(result)


# ---------------------------------------------------------------------------
# 5. RECONSTRUCTING THE EVIDENCE FROM THE JOURNAL
# ---------------------------------------------------------------------------
# The WIRE. A query that quietly returns nothing leaves every test above green
# while the fix does nothing in production.

from qt.api.fidelity import _nonfill_events  # noqa: E402
from qt.models import Strategy, Trade  # noqa: E402

WIN_FROM = D0 + timedelta(days=1)
WIN_TO = D0 + timedelta(days=4)

_SEQ = iter(range(1, 10_000))


def _strat_row(session) -> Strategy:
    s = Strategy(name=f"nonfill seed {next(_SEQ)}", asset_class="crypto",
                 universe="custom", preset="custom", params="{}", sizing_usd=100,
                 sleeve_usd=1000, max_positions=3, enabled=False)
    session.add(s)
    session.flush()
    return s


def _row(session, symbol, created, *, status, reason=None):
    t = Trade(
        strategy_id=_strat_row(session).id, mode="paper", symbol=symbol,
        asset_class="crypto", side="long", qty=1.0, notional=100.0, status=status,
        entry_reason=reason, created_at=created,
    )
    session.add(t)
    session.flush()
    return t


def _events(session, symbol) -> list[dict]:
    return [
        e for e in _nonfill_events(session, WIN_FROM, WIN_TO, "paper")
        if e["symbol"] == symbol
    ]


def test_a_did_not_fill_rejection_is_a_strike(db_session):
    _row(db_session, "MISSA/USD", WIN_FROM + timedelta(minutes=1),
         status="rejected", reason="wanted to buy but market order did not fill in 6s")
    got = _events(db_session, "MISSA/USD")
    assert len(got) == 1 and got[0]["filled"] is False


def test_a_filled_trade_is_a_reset_not_a_strike(db_session):
    _row(db_session, "FILLB/USD", WIN_FROM + timedelta(minutes=2), status="open")
    got = _events(db_session, "FILLB/USD")
    assert len(got) == 1 and got[0]["filled"] is True


def test_a_rail_rejection_is_neither(db_session):
    """Live skips these rather than counting them or treating them as a reset:
    never having placed an order is not the same as having placed one and
    missed. Counting them would bench a symbol the rails had merely paused, and
    treating them as fills would release a symbol that never filled."""
    _row(db_session, "RAILC/USD", WIN_FROM + timedelta(minutes=3),
         status="rejected", reason="wanted to buy but rail: cooldown after loss (2.0h of 24h)")
    assert _events(db_session, "RAILC/USD") == []


def test_another_modes_misses_are_not_borrowed(db_session):
    t = _row(db_session, "SHADOWD/USD", WIN_FROM + timedelta(minutes=4),
             status="rejected", reason="market order did not fill in 6s")
    t.mode = "shadow"
    db_session.flush()
    assert _events(db_session, "SHADOWD/USD") == []


def test_the_week_before_the_window_is_included(db_session):
    """A streak that began on the Friday is what benches a symbol on the Monday
    the comparison starts. Stopping at the window's edge would lose it."""
    _row(db_session, "EARLYE/USD", WIN_FROM - timedelta(days=2),
         status="rejected", reason="market order did not fill in 6s")
    assert len(_events(db_session, "EARLYE/USD")) == 1


def test_rows_from_before_the_lookback_are_not_fetched(db_session):
    """Anti-vacuity for the test above — the reach back is bounded, and by the
    same number live's own query uses."""
    _row(db_session, "ANCIENTF/USD", WIN_FROM - timedelta(days=NONFILL_LOOKBACK_DAYS + 2),
         status="rejected", reason="market order did not fill in 6s")
    assert _events(db_session, "ANCIENTF/USD") == []


def test_rows_after_the_window_are_not_fetched(db_session):
    _row(db_session, "FUTUREG/USD", WIN_TO + timedelta(hours=1),
         status="rejected", reason="market order did not fill in 6s")
    assert _events(db_session, "FUTUREG/USD") == []
