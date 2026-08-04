"""Positions the ACCOUNT held that a one-strategy replay cannot know about.

`check_rails` reads `open_positions_total`, `open_exposure_usd` and
`already_open_symbol` as ACCOUNT-wide facts — the rail's own wording is
"position already open for this symbol (any strategy)". The replay fed its own
numbers into all three, so it was strictly FREER than live: it bought names live
had refused, and the fidelity report filed those against the backtester as
trades it invented.

The owner's requirement is a 100% match on POSITIONS (price differences are
explicitly fine), so every one of these has to go.
"""

from datetime import datetime, timedelta, timezone

from qt.services.backtest import _AccountBackdrop, run_backtest
from qt.services.engine import RISK_DEFAULTS

RISK = dict(
    RISK_DEFAULTS,
    max_total_positions=50, max_total_exposure_usd=1_000_000,
    max_daily_loss_usd=1_000_000, max_trades_per_day=1000, wash_sale_guard="off",
)
D0 = datetime(2026, 5, 1, 14, 0, tzinfo=timezone.utc)


def _strategy(**kw) -> dict:
    base = {
        "asset_class": "stock", "swing_mode": False,
        "sizing_usd": 1000.0, "sleeve_usd": 5000.0, "max_positions": 5,
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


def _bars(n: int = 6, step_pct: float = 6.0) -> list[dict]:
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


# --- the backdrop object itself -------------------------------------------

def test_a_position_spanning_the_instant_is_counted():
    b = _AccountBackdrop([
        {"symbol": "AAA", "from": D0, "to": D0 + timedelta(days=3), "notional": 250.0},
    ])
    n, usd, syms = b.at(D0 + timedelta(days=1))
    assert (n, usd, syms) == (1, 250.0, {"AAA"})


def test_a_still_open_position_has_no_end_and_blocks_forever_after():
    b = _AccountBackdrop([{"symbol": "AAA", "from": D0, "to": None, "notional": 10.0}])
    assert b.at(D0 + timedelta(days=900))[0] == 1


def test_the_interval_is_half_open_so_a_close_frees_the_symbol_on_that_bar():
    """A position closed at T must not block a candidate evaluated at T."""
    end = D0 + timedelta(days=2)
    b = _AccountBackdrop([{"symbol": "AAA", "from": D0, "to": end, "notional": 10.0}])
    assert b.at(end - timedelta(seconds=1))[0] == 1
    assert b.at(end)[0] == 0


def test_a_position_that_ended_before_the_instant_is_not_counted():
    b = _AccountBackdrop([
        {"symbol": "AAA", "from": D0, "to": D0 + timedelta(days=1), "notional": 10.0},
    ])
    assert b.at(D0 + timedelta(days=5)) == (0, 0.0, set())


def test_iso_strings_and_naive_datetimes_are_both_accepted():
    """The API delivers ISO strings; stored instants are sometimes naive UTC."""
    b = _AccountBackdrop([
        {"symbol": "AAA", "from": "2026-05-01T14:00:00Z", "to": None, "notional": 1.0},
        {"symbol": "BBB", "from": datetime(2026, 5, 1, 14, 0), "to": None, "notional": 1.0},
    ])
    assert b.at(D0 + timedelta(days=1))[2] == {"AAA", "BBB"}


def test_an_empty_backdrop_reports_nothing():
    assert _AccountBackdrop(None).at(D0) == (0, 0.0, set())


# --- effect on the replay --------------------------------------------------

def test_without_a_backdrop_the_replay_is_unchanged():
    """Anti-vacuity: the symbol below only stays out because of the backdrop."""
    result = run_backtest(_strategy(), {"AAA": _bars()}, RISK)
    assert "AAA" in _entries(result)


def test_a_position_another_strategy_holds_blocks_the_entry():
    """The account-wide already-open rail, which the replay could not see."""
    result = run_backtest(
        _strategy(), {"AAA": _bars()}, RISK,
        account_positions=[{"symbol": "AAA", "from": D0 - timedelta(days=1),
                            "to": None, "notional": 100.0}],
    )
    assert "AAA" not in _entries(result)


def test_a_backdrop_on_a_different_symbol_does_not_block():
    result = run_backtest(
        _strategy(), {"AAA": _bars()}, RISK,
        account_positions=[{"symbol": "ZZZ", "from": D0 - timedelta(days=1),
                            "to": None, "notional": 100.0}],
    )
    assert "AAA" in _entries(result)


def test_the_account_position_cap_counts_other_strategies_holdings():
    tight = dict(RISK, max_total_positions=2)
    others = [
        {"symbol": f"O{i}", "from": D0 - timedelta(days=1), "to": None, "notional": 1.0}
        for i in range(2)
    ]
    assert "AAA" not in _entries(
        run_backtest(_strategy(), {"AAA": _bars()}, tight, account_positions=others))
    # One fewer and the same run gets in — so the cap, not the symbol, refused it.
    assert "AAA" in _entries(
        run_backtest(_strategy(), {"AAA": _bars()}, tight, account_positions=others[:1]))


def test_other_holdings_raise_equity_too_so_no_phantom_leverage_rail():
    """The no-leverage cap is min(max_total_exposure_usd, equity). Counting other
    strategies' EXPOSURE without their EQUITY would invent a rail live never hit
    — the fix must not trade one false verdict for another."""
    risk = dict(RISK, max_total_exposure_usd=1_000_000)
    big = [{"symbol": "ZZZ", "from": D0 - timedelta(days=1), "to": None, "notional": 4_000.0}]
    assert "AAA" in _entries(
        run_backtest(_strategy(), {"AAA": _bars()}, risk, starting_cash=2000.0,
                     account_positions=big))


def test_a_position_that_closes_mid_window_stops_blocking_afterwards():
    """The rail is a span, not a flag: live freed the symbol and so must this."""
    freed = D0 + timedelta(days=2)
    result = run_backtest(
        _strategy(), {"AAA": _bars()}, RISK,
        account_positions=[{"symbol": "AAA", "from": D0 - timedelta(days=1),
                            "to": freed, "notional": 100.0}],
    )
    assert "AAA" in _entries(result)


# --- reconstructing it from the journal ------------------------------------
# The WIRE, not the ends of it. A query that silently returns nothing would make
# every test above pass while the fix did nothing in production — the same shape
# that let `last_trade_at` be None at every call site with a green suite.

from qt.api.fidelity import _account_positions  # noqa: E402
from qt.models import Strategy, Trade  # noqa: E402

WIN_FROM = D0 + timedelta(days=1)
WIN_TO = D0 + timedelta(days=4)


def _trade(session, strategy_id: int, symbol: str, entry, exit_=None, status="open"):
    t = Trade(
        strategy_id=strategy_id, mode="paper", symbol=symbol, asset_class="stock",
        side="long", qty=10.0, notional=100.0, status=status,
        entry_price=10.0, entry_at=entry, exit_at=exit_,
    )
    session.add(t)
    session.flush()
    return t


_SEQ = iter(range(1, 10_000))


def _strat_row(session, name: str) -> Strategy:
    # Unique per call: db_session is a SHARED database and rows from earlier
    # tests survive, so a fixed name collides and a whole-list assertion sees
    # another test's holdings. Every assertion below is scoped to the symbols
    # the test itself created, for the same reason.
    s = Strategy(name=f"{name} {next(_SEQ)}", asset_class="stock", universe="custom",
                 preset="custom", params="{}", sizing_usd=100, sleeve_usd=1000,
                 max_positions=3, enabled=False)
    session.add(s)
    session.flush()
    return s


def _two(session) -> tuple[Strategy, Strategy]:
    """The subject of the replay, and another strategy sharing the account."""
    return _strat_row(session, "backdrop subject"), _strat_row(session, "the other one")


def test_another_strategys_overlapping_position_is_reconstructed(db_session):
    mine, other = _two(db_session)
    _trade(db_session, other.id, "OTHERCO", WIN_FROM, None)
    got = {p["symbol"] for p in _account_positions(db_session, mine, WIN_FROM, WIN_TO, "paper")}
    assert "OTHERCO" in got


def test_my_own_position_opened_before_the_window_is_included(db_session):
    """The replay starts flat, so live's already-open rail could not block it."""
    mine, _other = _two(db_session)
    _trade(db_session, mine.id, "HELDCO", D0, None)
    got = {p["symbol"] for p in _account_positions(db_session, mine, WIN_FROM, WIN_TO, "paper")}
    assert "HELDCO" in got


def test_my_own_position_opened_INSIDE_the_window_is_excluded(db_session):
    """The replay opens that one itself — seeding it too would double-count and
    the strategy would block its own entry."""
    mine, _other = _two(db_session)
    _trade(db_session, mine.id, "SELFCO", WIN_FROM + timedelta(hours=1), None)
    got = {p["symbol"] for p in _account_positions(db_session, mine, WIN_FROM, WIN_TO, "paper")}
    assert "SELFCO" not in got


def test_a_row_that_never_filled_is_not_a_holding(db_session):
    """A rejected candidate, and an order placed but never filled, both held
    nothing. `entry_at` is what separates a holding from a decision — asserting
    on `status` instead would pass without the guard doing any work."""
    mine, other = _two(db_session)
    _trade(db_session, other.id, "NOPECO", None, None, status="rejected")
    _trade(db_session, other.id, "UNFILLEDCO", None, None, status="open")
    got = {p["symbol"] for p in _account_positions(db_session, mine, WIN_FROM, WIN_TO, "paper")}
    assert "NOPECO" not in got
    assert "UNFILLEDCO" not in got


def test_a_position_closed_before_the_window_is_excluded(db_session):
    mine, other = _two(db_session)
    _trade(db_session, other.id, "GONECO", D0, D0 + timedelta(hours=1), status="closed")
    got = {p["symbol"] for p in _account_positions(db_session, mine, WIN_FROM, WIN_TO, "paper")}
    assert "GONECO" not in got


def test_the_other_modes_holdings_are_not_borrowed(db_session):
    """A shadow position never existed as far as a paper replay is concerned."""
    mine, other = _two(db_session)
    t = _trade(db_session, other.id, "SHADOWCO", WIN_FROM, None)
    t.mode = "shadow"
    db_session.flush()
    got = {p["symbol"] for p in _account_positions(db_session, mine, WIN_FROM, WIN_TO, "paper")}
    assert "SHADOWCO" not in got
