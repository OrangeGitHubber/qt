"""The two DAILY rails, and the day they reset on.

`check_rails` counts both of them across the WHOLE ACCOUNT — the trade-rate
limiter reads every non-rejected entry since the day boundary with no strategy
filter at all (engine._build_rail_context), and the kill switch sums every
closed trade's P&L the same way (engine._daily_loss). A replay of ONE strategy
fed its own numbers into both, so it carried most of a fresh daily budget and
most of a fresh loss allowance that live did not have, and it entered where live
had already stopped.

And they reset at MIDNIGHT NEW YORK. engine._trading_day_start has no
asset-class branch — its docstring says in as many words that a UTC reset would
hand a 24/7 book a fresh budget mid-session — while the replay bucketed both
rails with `_day_fn`, which is `_utc_day` for crypto. So for exactly the asset
class that trades through both boundaries, the two systems' counters rolled over
4–5 hours apart and the extra (or missing) trade came back as signal
disagreement.

The owner's requirement is a 100% match on POSITIONS. Both of these move
positions.
"""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from qt.services.backtest import _AccountBackdrop, _rail_day_start, run_backtest
from qt.services.engine import RISK_DEFAULTS, _trading_day_start

ET = ZoneInfo("America/New_York")

WIDE = dict(
    RISK_DEFAULTS,
    max_total_positions=50, max_total_exposure_usd=1_000_000,
    max_daily_loss_usd=1_000_000, max_trades_per_day=1000, wash_sale_guard="off",
    cooldown_hours_after_loss=0,
)
D0 = datetime(2026, 5, 1, 14, 0, tzinfo=timezone.utc)  # 10:00 ET — mid-session


def _strategy(**kw) -> dict:
    base = {
        "asset_class": "stock", "swing_mode": False,
        "sizing_usd": 500.0, "sleeve_usd": 5000.0, "max_positions": 5,
        "params": {
            "entry": {"min_day_gain_pct": 1.0, "require_above_vwap": False,
                      "entry_window_start": None, "entry_window_end": None},
            "exit": {"trailing_stop_pct": 0, "stop_loss_pct": 0, "take_profit_pct": 0,
                     "max_holding_hours": 0, "flatten_before_close": False,
                     "exit_below_vwap": False},
        },
    }
    base.update(kw)
    return base


def _bars(n: int = 2, step_pct: float = 6.0) -> list[dict]:
    """Exactly TWO daily bars by default, which is exactly ONE chance to trade:
    the first has no previous close so its day-gain is None. A longer series
    would let a candidate blocked on the target day simply enter the day after,
    and every "it was refused" assertion below would pass without the rail
    having refused anything."""
    out, close = [], 10.0
    for i in range(n):
        ts = D0 + timedelta(days=i)
        out.append({"t": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "o": close, "h": close, "l": close, "c": close, "v": 1000, "vw": close})
        close *= 1 + step_pct / 100
    return out


def _entries(result: dict) -> set[str]:
    return ({t["symbol"] for t in result.get("trade_list") or []}
            | {p["symbol"] for p in result.get("open_positions") or []})


# The first bar of a daily series has no previous close, so its day-gain is None
# and it can never trade — the earliest entry `_bars()` can produce is the second
# bar. Every seed below is timed relative to THAT bar, not to D0: a seed placed a
# day early lands on a different trading day and proves nothing.
FIRST_TRADE = D0 + timedelta(days=1)


# ---------------------------------------------------------------------------
# 1. THE BACKDROP'S TWO NEW COUNTERS
# ---------------------------------------------------------------------------


def test_entries_are_counted_only_inside_the_day_and_only_up_to_now():
    """[day_start, ts] — inclusive at `ts` because live's own count has no upper
    bound at all (a fill this very tick is already in the total the next
    candidate is judged against), and empty before `day_start` because the
    limiter resets there."""
    day_start = D0 - timedelta(hours=10)
    b = _AccountBackdrop(None, entries=[
        {"at": day_start - timedelta(minutes=1)},  # yesterday's budget
        {"at": day_start},                          # the boundary itself counts
        {"at": D0},                                 # this instant counts
        {"at": D0 + timedelta(minutes=1)},          # the future does not
    ])
    assert b.entries_in(day_start, D0) == 2


def test_realized_pnl_is_summed_signed_so_a_winner_offsets_a_loser():
    """Live sums the whole account's P&L and clamps ONCE, at the end. Clamping
    each row instead would invent a loss the account never had and refuse
    entries live allowed."""
    day_start = D0 - timedelta(hours=10)
    b = _AccountBackdrop(None, realized=[
        {"at": D0 - timedelta(hours=2), "pnl": -150.0},
        {"at": D0 - timedelta(hours=1), "pnl": 90.0},
    ])
    assert b.realized_in(day_start, D0) == -60.0


def test_realized_pnl_outside_the_day_is_ignored():
    day_start = D0 - timedelta(hours=10)
    b = _AccountBackdrop(None, realized=[
        {"at": day_start - timedelta(hours=1), "pnl": -500.0},
        {"at": D0 + timedelta(hours=1), "pnl": -500.0},
    ])
    assert b.realized_in(day_start, D0) == 0.0


def test_an_unseeded_backdrop_reports_nothing_on_either_counter():
    """The whole point of opt-in seeding: an ordinary backtest is untouched."""
    b = _AccountBackdrop(None)
    assert b.entries_in(D0 - timedelta(hours=10), D0) == 0
    assert b.realized_in(D0 - timedelta(hours=10), D0) == 0.0


# ---------------------------------------------------------------------------
# 2. THE TRADE-RATE LIMITER IS CROSS-STRATEGY
# ---------------------------------------------------------------------------

ONE_TRADE = dict(WIDE, max_trades_per_day=1)


def test_without_the_seed_the_replay_has_the_whole_daily_budget():
    """Anti-vacuity for the test below."""
    assert "AAA" in _entries(run_backtest(_strategy(), {"AAA": _bars()}, ONE_TRADE))


def test_another_strategys_entry_spends_the_accounts_daily_budget():
    """Live counts entries by EVERY strategy against max_trades_per_day."""
    result = run_backtest(
        _strategy(), {"AAA": _bars()}, ONE_TRADE,
        account_entries=[{"at": FIRST_TRADE - timedelta(hours=1)}],
    )
    assert "AAA" not in _entries(result)


def test_an_entry_on_a_different_day_does_not_spend_todays_budget():
    """The limiter is daily, not cumulative — seeding it as a total would refuse
    everything after a busy Tuesday."""
    result = run_backtest(
        _strategy(), {"AAA": _bars()}, ONE_TRADE,
        account_entries=[{"at": FIRST_TRADE - timedelta(days=1)}],
    )
    assert "AAA" in _entries(result)


def test_an_entry_later_the_same_day_cannot_block_an_earlier_bar():
    """The replay must not see the account's future. A fill at 16:00 does not
    retroactively refuse the candidate evaluated at 10:00."""
    result = run_backtest(
        _strategy(), {"AAA": _bars()}, ONE_TRADE,
        account_entries=[{"at": FIRST_TRADE + timedelta(hours=4)}],
    )
    assert "AAA" in _entries(result)


# ---------------------------------------------------------------------------
# 3. THE KILL SWITCH MEASURES THE WHOLE ACCOUNT
# ---------------------------------------------------------------------------

# min(max_daily_loss_usd, equity × max_daily_loss_pct/100) → $100 at any equity
# above $100, so the dollar cap is the one under test.
TIGHT_LOSS = dict(WIDE, max_daily_loss_usd=100.0, max_daily_loss_pct=100.0)


def test_without_the_seed_the_kill_switch_has_not_tripped():
    """Anti-vacuity for the two tests below."""
    assert "AAA" in _entries(run_backtest(_strategy(), {"AAA": _bars()}, TIGHT_LOSS))


def test_another_strategys_realised_loss_trips_the_kill_switch():
    result = run_backtest(
        _strategy(), {"AAA": _bars()}, TIGHT_LOSS,
        account_realized=[{"at": FIRST_TRADE - timedelta(hours=1), "pnl": -150.0}],
    )
    assert "AAA" not in _entries(result)


def test_a_winner_elsewhere_in_the_account_buys_headroom_back():
    """Signed, exactly as live sums it: −150 + 100 = −50, under the $100 cap.
    A seed that clamped each row would leave this blocked."""
    result = run_backtest(
        _strategy(), {"AAA": _bars()}, TIGHT_LOSS,
        account_realized=[
            {"at": FIRST_TRADE - timedelta(hours=2), "pnl": -150.0},
            {"at": FIRST_TRADE - timedelta(hours=1), "pnl": 100.0},
        ],
    )
    assert "AAA" in _entries(result)


def test_yesterdays_loss_does_not_trip_todays_kill_switch():
    result = run_backtest(
        _strategy(), {"AAA": _bars()}, TIGHT_LOSS,
        account_realized=[{"at": FIRST_TRADE - timedelta(days=1), "pnl": -500.0}],
    )
    assert "AAA" in _entries(result)


# ---------------------------------------------------------------------------
# 4. THE DAY BOUNDARY: MIDNIGHT NEW YORK, FOR CRYPTO TOO
# ---------------------------------------------------------------------------


def test_the_replays_rail_day_start_is_the_engines_trading_day_start():
    """Not "similar to" — the same instant, which is the only way the two
    systems' counters can reset together. Checked across the ET-evening
    rollover, where a UTC boundary and an ET one disagree by a calendar day."""
    for probe in (
        datetime(2026, 5, 1, 14, 0, tzinfo=timezone.utc),   # 10:00 ET, mid-session
        datetime(2026, 5, 2, 2, 0, tzinfo=timezone.utc),    # 22:00 ET the PREVIOUS day
        datetime(2026, 1, 15, 3, 0, tzinfo=timezone.utc),   # EST, not EDT
    ):
        assert _rail_day_start(probe) == _trading_day_start(probe)


# A crypto book, hourly bars, straddling both boundaries. AAA spikes at 20:00Z
# on 1 May (16:00 ET) and takes the account's only trade for the day; BBB spikes
# later, at an instant whose UTC day and ET day disagree.
CRYPTO_START = datetime(2026, 4, 30, 0, 0, tzinfo=timezone.utc)
AAA_SPIKE = datetime(2026, 5, 1, 20, 0, tzinfo=timezone.utc)     # 16:00 ET, 1 May
SAME_ET_DAY = datetime(2026, 5, 2, 2, 0, tzinfo=timezone.utc)    # 22:00 ET, 1 May
NEXT_ET_DAY = datetime(2026, 5, 2, 5, 0, tzinfo=timezone.utc)    # 01:00 ET, 2 May


def _hourly(spike_at: datetime, hours: int = 56) -> list[dict]:
    """Flat at $10 with a single $11 hour — a +10% rolling-24h reading on that
    bar alone, and nothing else in the series can clear a 5% gate."""
    out = []
    for i in range(hours):
        ts = CRYPTO_START + timedelta(hours=i)
        close = 11.0 if ts == spike_at else 10.0
        out.append({"t": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "o": close, "h": close, "l": close, "c": close, "v": 1000, "vw": close})
    return out


def _crypto_strategy() -> dict:
    return _strategy(asset_class="crypto",
                     params={"entry": {"min_day_gain_pct": 5.0},
                             "exit": {"stop_loss_pct": 0, "take_profit_pct": 0}})


def _crypto_run(bbb_spike: datetime) -> set[str]:
    return _entries(run_backtest(
        _crypto_strategy(),
        {"AAA": _hourly(AAA_SPIKE), "BBB": _hourly(bbb_spike)},
        ONE_TRADE,
        market="crypto",
    ))


def test_the_crypto_premise_one_trade_a_day_is_taken_by_the_first_spike():
    """The fixture works: AAA is bought, so the budget really is spent."""
    assert "AAA" in _crypto_run(NEXT_ET_DAY)


def test_a_crypto_evening_trade_is_still_on_the_SAME_trading_day():
    """22:00 ET on 1 May is 02:00 UTC on 2 May. Live says the budget is spent;
    a UTC-bucketed replay says it is a brand new day and buys. That extra trade
    is what the comparison was filing as signal disagreement."""
    assert "BBB" not in _crypto_run(SAME_ET_DAY)


def test_a_crypto_trade_past_ET_midnight_does_get_a_fresh_budget():
    """Anti-vacuity: the rail is a DAY boundary, not a blanket refusal. 01:00 ET
    on 2 May is a new trading day for live, so the replay must buy here."""
    assert "BBB" in _crypto_run(NEXT_ET_DAY)


def test_the_kill_switch_uses_the_same_boundary_as_the_limiter():
    """One boundary for both counters, because live derives both from one
    `today_start`. A loss at 22:00 ET on 1 May still blocks at 01:00 UTC the
    next morning — the UTC day has turned, the trading day has not."""
    loss_at = datetime(2026, 5, 2, 2, 0, tzinfo=timezone.utc)     # 22:00 ET, 1 May
    blocked = _entries(run_backtest(
        _crypto_strategy(), {"BBB": _hourly(datetime(2026, 5, 2, 3, 0, tzinfo=timezone.utc))},
        TIGHT_LOSS, market="crypto",
        account_realized=[{"at": loss_at, "pnl": -150.0}],
    ))
    assert "BBB" not in blocked
    # …and past ET midnight the switch has reset, so the same seed lets it in.
    freed = _entries(run_backtest(
        _crypto_strategy(), {"BBB": _hourly(NEXT_ET_DAY)},
        TIGHT_LOSS, market="crypto",
        account_realized=[{"at": loss_at, "pnl": -150.0}],
    ))
    assert "BBB" in freed


def test_the_replays_OWN_loss_is_banked_under_the_same_day_the_rail_reads():
    """Both ends of the kill switch have to agree on what a day is.

    The exit leg banks realised P&L and the entry leg reads it back; if one used
    the bar day and the other the trading day, a crypto loss taken at 22:00 ET —
    already "tomorrow" in UTC — would be filed under a day the rail never looks
    at, and the switch would simply not trip. That is a silent failure: nothing
    reports a counter that was written to the wrong bucket.

    AAA is bought at 16:00 ET and stopped out for ~$200 at 22:00 ET (02:00 UTC
    the next day). BBB then qualifies an hour later, still inside the same
    trading day, with a $100 cap already blown."""
    entry_at = datetime(2026, 5, 1, 20, 0, tzinfo=timezone.utc)   # 16:00 ET, 1 May
    crash_at = datetime(2026, 5, 2, 2, 0, tzinfo=timezone.utc)    # 22:00 ET, 1 May
    bbb_at = datetime(2026, 5, 2, 3, 0, tzinfo=timezone.utc)      # 23:00 ET, 1 May

    def aaa() -> list[dict]:
        out = []
        for i in range(56):
            ts = CRYPTO_START + timedelta(hours=i)
            if ts < entry_at:
                close = 10.0
            elif ts < crash_at:
                close = 11.0   # entered here: +10% on the rolling-24h reading
            else:
                close = 8.0    # 27% below entry — through a 20% stop
            out.append({"t": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "o": close, "h": close, "l": close, "c": close,
                        "v": 1000, "vw": close})
        return out

    strategy = _strategy(
        asset_class="crypto", sizing_usd=1000.0,
        params={"entry": {"min_day_gain_pct": 5.0},
                "exit": {"stop_loss_pct": 20, "take_profit_pct": 0}},
    )
    result = run_backtest(
        strategy, {"AAA": aaa(), "BBB": _hourly(bbb_at)}, TIGHT_LOSS, market="crypto",
    )
    closed = [t for t in result["trade_list"] if t["symbol"] == "AAA"]
    assert closed and closed[0]["pnl"] < -100, closed  # the premise: a real loss
    assert "BBB" not in _entries(result)


# ---------------------------------------------------------------------------
# 5. THE PORTFOLIO SIMULATOR USES THE SAME BOUNDARY
# ---------------------------------------------------------------------------
# It simulates the whole account already, so it needs no seeding — but it owns a
# second copy of both daily counters, and a copy that rolls over at a different
# hour is the same bug wearing different clothes.

from qt.services.backtest import run_portfolio_backtest  # noqa: E402


def _portfolio_strategy(sid: int, name: str) -> dict:
    return {
        "id": sid, "name": name, "asset_class": "crypto", "swing_mode": False,
        "sizing_usd": 500.0, "sleeve_usd": 5000.0, "max_positions": 5,
        "params": {"entry": {"min_day_gain_pct": 5.0},
                   "exit": {"stop_loss_pct": 0, "take_profit_pct": 0}},
    }


def _portfolio_symbols(bbb_spike: datetime) -> set[str]:
    result = run_portfolio_backtest(
        [_portfolio_strategy(1, "Alpha"), _portfolio_strategy(2, "Beta")],
        {1: {"AAA": _hourly(AAA_SPIKE)}, 2: {"BBB": _hourly(bbb_spike)}},
        ONE_TRADE, market="crypto",
    )
    return ({t["symbol"] for t in result.get("trade_list") or []}
            | {p["symbol"] for p in result.get("open_positions") or []})


def test_the_portfolio_premise_the_first_strategy_takes_the_days_one_trade():
    assert "AAA" in _portfolio_symbols(NEXT_ET_DAY)


def test_the_portfolio_limiter_also_rolls_over_at_midnight_new_york():
    """22:00 ET on 1 May is 02:00 UTC on 2 May: the UTC day has turned, the
    trading day has not, and the account's one trade is already spent."""
    assert "BBB" not in _portfolio_symbols(SAME_ET_DAY)


def test_the_portfolio_limiter_does_reset_on_the_next_trading_day():
    """Anti-vacuity — a boundary, not a blanket refusal."""
    assert "BBB" in _portfolio_symbols(NEXT_ET_DAY)


def test_the_portfolios_kill_switch_banks_a_loss_under_the_day_it_reads():
    """The portfolio's own copy of the run_backtest test above: one strategy is
    stopped out at 22:00 ET, and the strategy beside it must find the account's
    loss allowance already spent an hour later. Both ends of the counter have to
    agree on what a day is, or a loss lands in a bucket the rail never reads and
    the switch simply does not trip."""
    entry_at = datetime(2026, 5, 1, 20, 0, tzinfo=timezone.utc)   # 16:00 ET, 1 May
    crash_at = datetime(2026, 5, 2, 2, 0, tzinfo=timezone.utc)    # 22:00 ET, 1 May
    bbb_at = datetime(2026, 5, 2, 3, 0, tzinfo=timezone.utc)      # 23:00 ET, 1 May

    def aaa() -> list[dict]:
        out = []
        for i in range(56):
            ts = CRYPTO_START + timedelta(hours=i)
            close = 10.0 if ts < entry_at else (11.0 if ts < crash_at else 8.0)
            out.append({"t": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "o": close, "h": close, "l": close, "c": close,
                        "v": 1000, "vw": close})
        return out

    loser = _portfolio_strategy(1, "Alpha")
    loser["sizing_usd"] = 1000.0
    loser["params"] = {"entry": {"min_day_gain_pct": 5.0},
                       "exit": {"stop_loss_pct": 20, "take_profit_pct": 0}}
    result = run_portfolio_backtest(
        [loser, _portfolio_strategy(2, "Beta")],
        {1: {"AAA": aaa()}, 2: {"BBB": _hourly(bbb_at)}},
        TIGHT_LOSS, market="crypto",
    )
    closed = [t for t in result["trade_list"] if t["symbol"] == "AAA"]
    assert closed and closed[0]["pnl"] < -100, closed  # the premise: a real loss
    symbols = ({t["symbol"] for t in result["trade_list"]}
               | {p["symbol"] for p in result["open_positions"]})
    assert "BBB" not in symbols


# ---------------------------------------------------------------------------
# 6. RECONSTRUCTING BOTH FROM THE JOURNAL
# ---------------------------------------------------------------------------
# The WIRE, not just the ends of it. A query that silently returns nothing would
# leave every test above green while the fix did nothing in production.

from qt.api.fidelity import _account_entries, _account_realized  # noqa: E402
from qt.models import Strategy, Trade  # noqa: E402

WIN_FROM = D0 + timedelta(days=1)
WIN_TO = D0 + timedelta(days=4)

_SEQ = iter(range(1, 10_000))


def _strat_row(session, name: str) -> Strategy:
    # Unique per call: db_session is a SHARED database and rows from earlier
    # tests survive. Every assertion below is scoped to symbols this file coins.
    s = Strategy(name=f"{name} {next(_SEQ)}", asset_class="stock", universe="custom",
                 preset="custom", params="{}", sizing_usd=100, sleeve_usd=1000,
                 max_positions=3, enabled=False)
    session.add(s)
    session.flush()
    return s


def _two(session) -> tuple[Strategy, Strategy]:
    return _strat_row(session, "daily rails subject"), _strat_row(session, "the other one")


def _trade(session, strategy_id, symbol, entry, exit_=None, *, status="open", pnl=None):
    t = Trade(
        strategy_id=strategy_id, mode="paper", symbol=symbol, asset_class="stock",
        side="long", qty=10.0, notional=100.0, status=status,
        entry_price=10.0, entry_at=entry, exit_at=exit_, pnl=pnl,
    )
    session.add(t)
    session.flush()
    return t


def _at(value) -> datetime:
    """SQLite hands back NAIVE datetimes, so the reconstructed instants are
    naive-UTC ISO strings while the fixtures below are aware. Compare the
    instants, not the spellings — `_AccountBackdrop` normalises them anyway
    (`_as_utc`), which is the behaviour this stands in for."""
    dt = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _entry_times(session, mine) -> set[datetime]:
    return {_at(e["at"]) for e in _account_entries(session, mine, WIN_FROM, WIN_TO, "paper")}


def test_another_strategys_entry_is_reconstructed(db_session):
    mine, other = _two(db_session)
    when = WIN_FROM + timedelta(hours=2)
    _trade(db_session, other.id, "OTHERENT", when)
    assert when in _entry_times(db_session, mine)


def test_my_own_in_window_entry_is_not_counted_twice(db_session):
    """The replay makes that one itself; seeding it would spend the day's budget
    twice and the strategy would block its own second trade."""
    mine, _other = _two(db_session)
    when = WIN_FROM + timedelta(minutes=11)
    _trade(db_session, mine.id, "SELFENT", when)
    assert when not in _entry_times(db_session, mine)


def test_a_rejected_row_is_not_an_entry(db_session):
    """Live counts `status != 'rejected'`, and a rejected row CAN carry an
    entry_at (open_trade's did-not-fill path writes one), so this filter is not
    implied by the NULL check beside it."""
    mine, other = _two(db_session)
    when = WIN_FROM + timedelta(minutes=13)
    _trade(db_session, other.id, "NOFILLENT", when, status="rejected")
    assert when not in _entry_times(db_session, mine)


def test_another_modes_entries_are_not_borrowed(db_session):
    mine, other = _two(db_session)
    when = WIN_FROM + timedelta(minutes=17)
    t = _trade(db_session, other.id, "SHADOWENT", when)
    t.mode = "shadow"
    db_session.flush()
    assert when not in _entry_times(db_session, mine)


def _realized(session, mine) -> list[dict]:
    return _account_realized(session, mine, WIN_FROM, WIN_TO, "paper")


def test_another_strategys_closed_loss_is_reconstructed_signed(db_session):
    mine, other = _two(db_session)
    out = WIN_FROM + timedelta(hours=5)
    _trade(db_session, other.id, "OTHERPNL", WIN_FROM, out, status="closed", pnl=-42.5)
    got = [r for r in _realized(db_session, mine) if _at(r["at"]) == out]
    assert got and got[0]["pnl"] == -42.5


def test_a_winner_is_reconstructed_too_not_only_losses(db_session):
    """Clamping here would refuse entries live allowed — see the signed-sum test
    above. The seed has to carry the good news as well."""
    mine, other = _two(db_session)
    out = WIN_FROM + timedelta(hours=6)
    _trade(db_session, other.id, "WINPNL", WIN_FROM, out, status="closed", pnl=77.0)
    got = [r for r in _realized(db_session, mine) if _at(r["at"]) == out]
    assert got and got[0]["pnl"] == 77.0


def test_my_own_trade_opened_and_closed_inside_the_window_is_excluded(db_session):
    """The replay opens and closes that one itself."""
    mine, _other = _two(db_session)
    out = WIN_FROM + timedelta(minutes=19)
    _trade(db_session, mine.id, "SELFPNL", WIN_FROM, out, status="closed", pnl=-99.0)
    assert out not in {_at(r["at"]) for r in _realized(db_session, mine)}


def test_my_own_trade_opened_BEFORE_the_window_is_included(db_session):
    """The replay starts flat, so it never holds that position and never books
    its exit — live did both."""
    mine, _other = _two(db_session)
    out = WIN_FROM + timedelta(hours=8)
    _trade(db_session, mine.id, "CARRIEDPNL", D0, out, status="closed", pnl=-31.0)
    assert out in {_at(r["at"]) for r in _realized(db_session, mine)}


def test_an_open_trade_has_realised_nothing(db_session):
    """No exit, nothing realised — live's `_daily_loss` sums CLOSED trades only."""
    mine, other = _two(db_session)
    _trade(db_session, other.id, "STILLOPEN", WIN_FROM, None, status="open", pnl=-500.0)
    assert all(r["pnl"] != -500.0 for r in _realized(db_session, mine))


def test_a_row_with_an_exit_time_but_not_CLOSED_is_still_not_realised(db_session):
    """`status == "closed"` is copied from `_daily_loss` and is doing its own
    work, not merely restating the `exit_at` bound beside it: a row can carry an
    exit timestamp without being a settled close — `close_trade` writes the two
    in sequence — and counting a mid-flight exit would trip the kill switch on
    a loss the account has not booked."""
    mine, other = _two(db_session)
    out = WIN_FROM + timedelta(minutes=23)
    _trade(db_session, other.id, "MIDCLOSE", WIN_FROM, out, status="open", pnl=-500.0)
    assert out not in {_at(r["at"]) for r in _realized(db_session, mine)}
