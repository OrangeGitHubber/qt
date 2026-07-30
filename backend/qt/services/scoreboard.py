"""Benchmark scoreboard: the project's honesty meter. Records daily
snapshots of bot equity plus SPY and BTC closes, and serves a normalized
comparison of "the bot" vs "just bought and held"."""

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from qt.broker.alpaca import AlpacaClient
from qt.models import BenchmarkSnapshot
from qt.settings_service import get_setting


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


async def record_snapshot(session: Session, client: AlpacaClient) -> None:
    account = await client.account()
    equity = float(account.get("equity") or 0)
    # Tag the account this equity belongs to. Prefer the number the broker just
    # reported over the stored setting — on the first tick after a key swap the
    # setting may still name the OLD account, and mislabelling one row is exactly
    # what puts a fake cliff in the chart.
    account_id = account.get("account_number") or get_setting(session, "current_account_id")

    spy_close = btc_close = None
    try:
        snaps = await client.stock_snapshots(["SPY"])
        spy_close = (snaps.get("SPY", {}).get("dailyBar") or {}).get("c")
    except Exception:
        pass
    try:
        snaps = await client.crypto_snapshots(["BTC/USD"])
        btc_close = (snaps.get("BTC/USD", {}).get("dailyBar") or {}).get("c")
    except Exception:
        pass

    day = _today()
    row = session.get(BenchmarkSnapshot, day)
    if row:
        row.bot_equity = equity
        row.spy_close = spy_close or row.spy_close
        row.btc_close = btc_close or row.btc_close
        # `day` is the primary key, so the switch-day row is shared: stamp it with
        # whichever account is trading NOW rather than leaving the old account's tag
        # on the new account's equity.
        row.account_id = account_id
    else:
        session.add(
            BenchmarkSnapshot(
                day=day, bot_equity=equity, spy_close=spy_close, btc_close=btc_close,
                account_id=account_id,
            )
        )


# A day-over-day equity move this large is an ACCOUNT CHANGE, not trading. The
# risk rails cap a real day's loss far below this, so the gap is unambiguous.
ACCOUNT_STEP_PCT = 25.0


def _with_untagged_history(every: list, tagged: list) -> list:
    """Extend the account's series BACKWARDS through untagged rows that are
    continuous in equity.

    Snapshots only started carrying an account in migration 0008, so every row
    recorded before that is untagged — including the current account's own recent
    history. Scoping strictly by account would throw that away and show a
    one-point chart on an account that has been running for days.

    Adopting *all* untagged rows would be worse: the previous account's rows are
    in there too, and pulling them in resurrects the very cliff this fix removed.
    So we walk backwards from the account's first tagged day and keep going only
    while consecutive equity is continuous — stopping dead at the step that marks
    the switch. Read-only: nothing is restamped, so a wrong guess costs a redraw,
    not data."""
    if not tagged or not every:
        return tagged
    first = tagged[0]
    idx = {id(r): i for i, r in enumerate(every)}.get(id(first))
    if idx is None:
        return tagged
    prefix: list = []
    later = first
    for row in reversed(every[:idx]):
        if row.account_id is not None:  # a DIFFERENT account's row — stop
            break
        prev, cur = row.bot_equity, later.bot_equity
        if not prev or abs(cur / prev - 1) * 100 >= ACCOUNT_STEP_PCT:
            break  # the switch
        prefix.append(row)
        later = row
    return list(reversed(prefix)) + tagged


def series(session: Session, account: str | None = None) -> dict:
    """The scoreboard, scoped to ONE broker account.

    Equity is only comparable within an account: normalising across a switch made
    the balance step read as a trading loss (a real case showed −80% the day a
    paper account was replaced). So the series is filtered to one account and
    rebased on THAT account's first day.

    `account`: None = the current account (what the dashboard wants); "all" = no
    filter (the legacy behaviour, still cross-account and still misleading —
    offered only for completeness); "untagged" = rows recorded before accounts
    were stamped. Mirrors the journal's selector."""
    q = session.query(BenchmarkSnapshot)
    if account == "untagged":
        q = q.filter(BenchmarkSnapshot.account_id.is_(None))
        rows = q.order_by(BenchmarkSnapshot.day).all()
    elif account == "all":
        rows = q.order_by(BenchmarkSnapshot.day).all()
    else:
        current = account or get_setting(session, "current_account_id")
        every = q.order_by(BenchmarkSnapshot.day).all()
        rows = [r for r in every if r.account_id == current] if current else every
        rows = _with_untagged_history(every, rows)
    if not rows:
        return {"days": [], "bot": [], "spy": [], "btc": [], "verdict": None, "account": account}

    base = rows[0]

    def pct(cur: float | None, first: float | None) -> float | None:
        if cur is None or not first:
            return None
        return round((cur / first - 1) * 100, 2)

    # All three lines rebase on the SAME first day of the scoped series, so the
    # comparison reads "since this account started" — bot and benchmarks alike.
    out = {
        "days": [r.day for r in rows],
        "bot": [pct(r.bot_equity, base.bot_equity) for r in rows],
        "spy": [pct(r.spy_close, base.spy_close) for r in rows],
        "btc": [pct(r.btc_close, base.btc_close) for r in rows],
        "account": base.account_id,
        # The equity every point is measured against. Exposed so the UI can turn
        # a strategy's DOLLAR P&L into the same percentage points the chart plots
        # — otherwise per-strategy attribution can't be compared to the line.
        "base_equity": base.bot_equity,
    }
    last = rows[-1]
    bot_r = pct(last.bot_equity, base.bot_equity)
    spy_r = pct(last.spy_close, base.spy_close)
    if bot_r is None or spy_r is None:
        out["verdict"] = None
    elif bot_r > spy_r:
        out["verdict"] = f"Bot is beating buy-and-hold SPY by {bot_r - spy_r:.2f} points."
    else:
        out["verdict"] = f"Buy-and-hold SPY is beating the bot by {spy_r - bot_r:.2f} points."
    return out
