"""Benchmark scoreboard: the project's honesty meter. Records daily
snapshots of bot equity plus SPY and BTC closes, and serves a normalized
comparison of "the bot" vs "just bought and held"."""

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from qt.broker.alpaca import AlpacaClient
from qt.models import BenchmarkSnapshot


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


async def record_snapshot(session: Session, client: AlpacaClient) -> None:
    account = await client.account()
    equity = float(account.get("equity") or 0)

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
    else:
        session.add(
            BenchmarkSnapshot(day=day, bot_equity=equity, spy_close=spy_close, btc_close=btc_close)
        )


def series(session: Session) -> dict:
    rows = session.query(BenchmarkSnapshot).order_by(BenchmarkSnapshot.day).all()
    if not rows:
        return {"days": [], "bot": [], "spy": [], "btc": [], "verdict": None}

    base = rows[0]

    def pct(cur: float | None, first: float | None) -> float | None:
        if cur is None or not first:
            return None
        return round((cur / first - 1) * 100, 2)

    out = {
        "days": [r.day for r in rows],
        "bot": [pct(r.bot_equity, base.bot_equity) for r in rows],
        "spy": [pct(r.spy_close, base.spy_close) for r in rows],
        "btc": [pct(r.btc_close, base.btc_close) for r in rows],
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
