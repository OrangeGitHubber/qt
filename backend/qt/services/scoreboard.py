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
    elif account != "all":
        current = account or get_setting(session, "current_account_id")
        if current:
            q = q.filter(BenchmarkSnapshot.account_id == current)
    rows = q.order_by(BenchmarkSnapshot.day).all()
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
