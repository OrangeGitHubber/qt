"""A journal full of REFUSED orders must not look like a full account.

MEASURED, and it is the whole reason strategy 25 could not be compared. The
comparison hands the replay an account backdrop — what the rest of the account
held, traded and lost — so the replay hits the same account-wide rails live did.
`_account_positions` built that from every journal row with an `entry_at` and no
`exit_at`, on the argument that a rejected row can never have one.

It can. `open_trade`'s did-not-fill path writes a rejected row that carries an
entry_at, and the account had 4,612 of them: 4,492 concurrent phantom positions
against a cap of 50. Every candidate the replay looked at died at "rail: max
open positions reached" before any entry logic ran — on every bar, at every
resolution, in every stretch. The report said `backtest_trades: 0` and listed
three real trades as "the replay missed it — this is the kind that points at a
real bug", which is true and useless: the replay never got to have an opinion.

Three separate fixes for the resolution of a replay that was never going to
trade went out before anyone read what the replay itself said it was doing. This
test asserts the input, through the endpoint, because that is where the fault
lived — the unit test one directory over passed throughout, on a fixture that
built rejected rows with a NULL entry_at.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from qt import security
from qt.api import fidelity as fidelity_api
from qt.broker.alpaca import SECRET_KEY_ID, SECRET_KEY_SECRET, AlpacaClient
from qt.db import session_scope
from qt.models import Strategy, StrategyConfigVersion, Trade
from qt.services import barcache

UTC = timezone.utc
SYMBOL = "PHNT/USD"


@pytest.fixture()
def cache(monkeypatch):
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


async def _bars(self, symbols, asset_class, timeframe, start, end=None):
    first = datetime.fromisoformat(start.replace("Z", "+00:00"))
    step = timedelta(days=1) if timeframe == "1Day" else timedelta(minutes=15)
    out, cursor, last = [], first, datetime.now(UTC) + timedelta(days=1)
    while cursor < last:
        out.append({"t": cursor.strftime("%Y-%m-%dT%H:%M:%SZ"), "o": 100.0, "h": 100.0,
                    "l": 100.0, "c": 100.0, "v": 1e5, "vw": 100.0})
        cursor += step
    return {s: list(out) for s in symbols}


def _strategy(client, name: str) -> int:
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


def _row(sid: int, symbol: str, entry_at, *, status: str) -> None:
    with session_scope() as s:
        s.add(Trade(
            strategy_id=sid, mode="paper", symbol=symbol, asset_class="crypto",
            status=status, qty=10, notional=1000, entry_price=100.0,
            entry_reason="gain", entry_at=entry_at,
        ))


def _backdrops(client, sid: int, start: datetime, end: datetime) -> list[list[dict]]:
    """Every `account_positions` list the endpoint handed a replay."""
    seen: list[list[dict]] = []
    real_run, real_replay = fidelity_api.run, fidelity_api.replay

    async def run_spy(body, *a, **k):
        seen.append(body.account_positions)
        return await real_run(body, *a, **k)

    async def replay_spy(body, *a, **k):
        seen.append(body.account_positions)
        return await real_replay(body, *a, **k)

    with patch.object(AlpacaClient, "historical_bars", new=_bars), \
            patch.multiple(fidelity_api, run=run_spy, replay=replay_spy):
        response = client.post("/api/fidelity/compare", json={
            "strategy_id": sid, "mode": "paper",
            "window_start": start.isoformat(), "window_end": end.isoformat(),
        })
    assert response.status_code == 200, response.text
    return seen


def _window() -> tuple[datetime, datetime]:
    end = (datetime.now(UTC) - timedelta(days=1)).replace(
        hour=23, minute=0, second=0, microsecond=0
    )
    return end - timedelta(hours=6), end


def test_refused_orders_are_not_handed_to_the_replay_as_open_positions(
    client, configured, cache
):
    """The account-wide position cap is the rail this trips, and once it is
    tripped nothing else about the replay matters — no resolution, no stretch
    boundary and no bar can get past a rail that fires before the entry logic
    is reached."""
    start, end = _window()
    sid = _strategy(client, "phantom mine")
    other = _strategy(client, "phantom other")
    _row(sid, SYMBOL, start + timedelta(hours=1), status="open")
    # Sixty refusals by another strategy, each carrying the entry_at that
    # `open_trade`'s did-not-fill path really stamps on them. Comfortably past
    # any sane position cap, which is the point: one leaking through is a
    # miscount, sixty is a comparison that cannot happen.
    for n in range(60):
        _row(other, f"REJ{n}/USD", start - timedelta(hours=2), status="rejected")

    handed = _backdrops(client, sid, start, end)

    assert handed, "no replay ran at all"
    assert all(not backdrop for backdrop in handed), (
        f"the replay was handed {max(len(b) for b in handed)} phantom positions built "
        "from orders that never filled — it will hit the account position cap on "
        "every bar and reject every candidate before reading the entry rules"
    )


def test_a_real_holding_is_still_handed_over(
    client, configured, cache
):
    """The control, and it has to be here: excluding refused orders by excluding
    everything would silence the rail this backdrop exists to reproduce, and the
    replay would go back to being freer than live — which is the fault the
    backdrop was built to fix in the first place."""
    start, end = _window()
    sid = _strategy(client, "phantom real mine")
    other = _strategy(client, "phantom real other")
    _row(sid, SYMBOL, start + timedelta(hours=1), status="open")
    _row(other, "HELD/USD", start - timedelta(hours=2), status="open")
    _row(other, "REJ/USD", start - timedelta(hours=2), status="rejected")

    handed = _backdrops(client, sid, start, end)

    assert handed, "no replay ran at all"
    held = {p["symbol"] for backdrop in handed for p in backdrop}
    assert held == {"HELD/USD"}, held
