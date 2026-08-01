"""Real broker fees, reconciled from Alpaca's account activities.

WHY THIS EXISTS
---------------
US equities on Alpaca are commission-free; CRYPTO is not — 0.15-0.25% per side,
so a round trip costs roughly half a percent. QT recorded none of it, so every
per-trade P&L in the journal was gross. This module pulls what the broker
actually charged so the account-level numbers stop flattering themselves.

This is about REAL fees. The backtest's modelled `fee_pct` is a separate,
deliberately-estimated thing and is untouched by any of this.

WHAT ALPACA ACTUALLY SENDS  (docs.alpaca.markets/docs/crypto-fees,
/docs/account-activities, /reference/getaccountactivitiesbytype)
----------------------------------------------------------------
A crypto fee arrives as activity_type CFEE, and CFEE is a *non-trade* activity.
The documented example is, verbatim:

    {
        "id": "20220812000000000::53be51ba-46f9-43de-b81f-576f241dc680",
        "activity_type": "CFEE",
        "date": "2022-08-12",
        "net_amount": "0",
        "description": "Coin Pair Transaction Fee (Non USD)",
        "symbol": "ETHUSD",
        "qty": "-0.000195",
        "price": "1884.5",
        "status": "executed"
    }

Three properties of that payload drive every decision below.

1. THERE IS NO ORDER ID. Only *trade* activities (FILL) carry `order_id`; the
   non-trade shape has no such field, and no side either. Alpaca does document
   `group_id` on non-trade activities as "links related activities", but nothing
   states it links a fee to an order, so we do not treat it as one.

2. THERE IS NO TIMESTAMP — only `date`. A fee is stamped to a DAY. Two round
   trips in ETH on the same day produce fees that are indistinguishable from
   each other.

3. THE FEE IS OFTEN NOT IN DOLLARS. Alpaca charges it on the asset you receive:
   buy ETH/USD and the fee is paid in ETH (hence net_amount "0" and a negative
   `qty` above); sell ETH/USD and it is paid in USD. Turning a coin-denominated
   fee into dollars means multiplying by the broker's mark, which is an
   ESTIMATE and is flagged as one.

Fields are read defensively — non-trade activities are thinly documented, values
arrive as strings, and a missing field must degrade to "unknown" rather than
raise or quietly become zero.

WHY WE DO NOT ATTRIBUTE FEES TO TRADES
--------------------------------------
Given (1) and (2), tying a fee to a specific Trade row would mean guessing from
symbol + day + side. On any day with more than one trade in a symbol — the
normal case for a momentum bot, and the whole reason Strategy.allow_concurrent_
symbol exists — that guess is unfalsifiable and wrong often enough to matter.

So Trade.fees stays NULL and the journal renders "—". The fees are recorded at
the granularity Alpaca reports them (activity / day / symbol / account), which
is defensible as a fact. An honest "the account paid $12.40 in fees, but not
which trade owns which slice" beats a confident per-trade number nobody can
check. If Alpaca ever adds an order id, Trade.fees is already there to hold it.

IDEMPOTENCY
-----------
FeeActivity's primary key is Alpaca's own activity id. Re-syncing an overlapping
window (which this job does on purpose — see LOOKBACK_DAYS) re-sees the same ids
and inserts nothing. Nothing here reads or mutates a running total, so there is
no path by which running the job twice doubles a fee.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from sqlalchemy.orm import Session

from qt.models import FeeActivity
from qt.settings_service import get_setting, set_setting

log = logging.getLogger("qt.fees")

# CFEE is the crypto trading fee. FEE is the generic bucket (regulatory/other);
# it is usually empty for a self-directed account, but it costs one request and
# missing a charged fee is the failure mode we care about.
FEE_ACTIVITY_TYPES = ("CFEE", "FEE")

# Watermark: the last day we have swept up to. Same pattern as the other daily
# jobs — it is what lets a restart after downtime back-fill the gap instead of
# silently resuming from today.
WATERMARK_SETTING = "fees_synced_through"

# Always re-scan a few days behind the watermark. Alpaca posts fees END OF DAY
# and has been known to post them later still, so a window that starts exactly
# at the watermark would permanently miss anything that landed after our last
# run. The overlap is free precisely because the activity id is the primary key.
LOOKBACK_DAYS = 3

# How far back the very first sync reaches. Enough to cover a fresh install that
# has been trading for a few weeks, without pulling years of history nobody
# asked for.
INITIAL_BACKFILL_DAYS = 30


@dataclass(frozen=True)
class ParsedFee:
    """One activity, normalised. `usd_amount` is a POSITIVE magnitude (what the
    account paid); `qty` keeps Alpaca's own sign so the raw truth survives."""

    id: str
    activity_type: str
    day: str
    symbol: str | None
    qty: float | None
    price: float | None
    usd_amount: float | None
    usd_is_estimate: bool
    description: str
    raw: str


def _f(value: Any) -> float | None:
    """Alpaca sends numbers as strings, and sometimes not at all. Anything that
    isn't a real number becomes None (unknown) — never 0.0, which would claim a
    fee of zero was charged."""
    if value is None or value == "":
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out and abs(out) != float("inf") else None  # reject NaN/inf


def _day_from_id(activity_id: str) -> str | None:
    """Activity ids are prefixed with a timestamp ('20220812000000000::<uuid>').
    Used only as a fallback when `date` is absent, so a fee still lands on the
    right day rather than being dropped."""
    head = activity_id.split("::", 1)[0]
    if len(head) >= 8 and head[:8].isdigit():
        y, m, d = head[:4], head[4:6], head[6:8]
        if "01" <= m <= "12" and "01" <= d <= "31":
            return f"{y}-{m}-{d}"
    return None


def parse_activity(raw: dict[str, Any], fallback_type: str = "") -> ParsedFee | None:
    """Normalise one activity payload, or None if it can't be trusted.

    Returns None when there is no `id`: the id IS the idempotency key, so a row
    without one could be inserted again on every run. Dropping it loses one fee;
    keeping it would silently inflate the total forever.
    """
    activity_id = str(raw.get("id") or "").strip()
    if not activity_id:
        log.warning("fee activity with no id — skipped (cannot dedupe it safely): %s", raw)
        return None

    day = str(raw.get("date") or "").strip()[:10] or _day_from_id(activity_id)
    if not day:
        log.warning("fee activity %s has no usable date — skipped", activity_id)
        return None

    qty = _f(raw.get("qty"))
    price = _f(raw.get("price"))
    net = _f(raw.get("net_amount"))

    # `net_amount` is the broker's own dollar figure and wins whenever it is a
    # real number. It is "0" for a fee charged in coin, which is not a discount —
    # it means "look at qty/price instead", so zero must fall through, not be
    # recorded as a free trade.
    if net is not None and net != 0.0:
        usd, estimated = abs(net), False
    elif qty is not None and price is not None:
        # Coin-denominated fee valued at the broker's mark. An estimate, and
        # flagged as one all the way to the UI.
        usd, estimated = abs(qty) * price, True
    else:
        usd, estimated = None, False  # unknown; a row still gets stored, showing "—"

    symbol = str(raw.get("symbol") or "").strip().upper() or None
    return ParsedFee(
        id=activity_id,
        activity_type=str(raw.get("activity_type") or fallback_type or "").upper()[:16],
        day=day,
        symbol=symbol,
        qty=qty,
        price=price,
        usd_amount=usd,
        usd_is_estimate=estimated,
        description=str(raw.get("description") or ""),
        raw=json.dumps(raw, sort_keys=True, default=str),
    )


def sync_window(watermark: str | None, today: date) -> tuple[str, str]:
    """The (after, until) day pair to ask Alpaca for, both YYYY-MM-DD.

    Alpaca treats `after`/`until` as EXCLUSIVE, so each end is widened by a day —
    otherwise the boundary day's fees are silently missing.

    After downtime the watermark is old and the window simply spans the whole
    gap; there is no separate back-fill path to get wrong.
    """
    if watermark:
        try:
            start = date.fromisoformat(watermark) - timedelta(days=LOOKBACK_DAYS)
        except ValueError:
            # A corrupt watermark must not wedge the sync forever.
            log.warning("unreadable fee watermark %r — falling back to a full back-fill", watermark)
            start = today - timedelta(days=INITIAL_BACKFILL_DAYS)
    else:
        start = today - timedelta(days=INITIAL_BACKFILL_DAYS)
    # A clock skew or a restored backup could put the watermark in the future;
    # clamp so the window can never invert into an empty request.
    start = min(start, today)
    return (start - timedelta(days=1)).isoformat(), (today + timedelta(days=1)).isoformat()


def record_fees(session: Session, parsed: list[ParsedFee], account_id: str | None) -> int:
    """Insert the activities we haven't seen. Returns how many were new.

    Existing rows are left ALONE rather than refreshed: an activity is a posted
    accounting fact, and re-writing it would only ever let a later bug overwrite
    good data.
    """
    if not parsed:
        return 0
    ids = [p.id for p in parsed]
    known: set[str] = set()
    # Chunked because SQLite caps the number of bound variables in one statement,
    # and a month of back-fill can exceed it.
    for i in range(0, len(ids), 500):
        chunk = ids[i : i + 500]
        known.update(
            row[0]
            for row in session.query(FeeActivity.id).filter(FeeActivity.id.in_(chunk)).all()
        )
    new = 0
    for p in parsed:
        if p.id in known:
            continue
        known.add(p.id)  # the same id twice in one payload is still one fee
        session.add(
            FeeActivity(
                id=p.id,
                account_id=account_id,
                activity_type=p.activity_type,
                day=p.day,
                symbol=p.symbol,
                qty=p.qty,
                price=p.price,
                usd_amount=p.usd_amount,
                usd_is_estimate=p.usd_is_estimate,
                description=p.description,
                raw=p.raw,
            )
        )
        new += 1
    return new


async def sync(session: Session, client, today: date | None = None) -> dict[str, Any]:
    """Pull recent fee activities into the journal. Safe to run repeatedly.

    The watermark is advanced ONLY if every activity type came back cleanly. A
    partial failure that still moved the watermark would step over the days it
    failed on and lose them for good — the next run would start after the gap.
    Leaving it put means the next run simply re-covers the same window, which
    costs one request and is guaranteed harmless by the id-keyed insert.
    """
    today = today or date.today()
    after, until = sync_window(get_setting(session, WATERMARK_SETTING), today)
    account_id = get_setting(session, "current_account_id") or None

    fetched, new, failed = 0, 0, []
    for activity_type in FEE_ACTIVITY_TYPES:
        try:
            rows = await client.account_activities(activity_type, after=after, until=until)
        except Exception:
            # Best-effort by design: one flaky type must not lose the other's data.
            log.exception("fee sync: could not fetch %s activities for %s..%s", activity_type, after, until)
            failed.append(activity_type)
            continue
        parsed = [p for p in (parse_activity(r, activity_type) for r in rows or []) if p]
        fetched += len(parsed)
        new += record_fees(session, parsed, account_id)

    if failed:
        log.warning("fee sync incomplete (%s failed) — watermark held at %r so the gap is retried",
                    ", ".join(failed), get_setting(session, WATERMARK_SETTING))
    else:
        set_setting(session, WATERMARK_SETTING, today.isoformat())

    return {
        "window": [after, until],
        "fetched": fetched,
        "new": new,
        "failed_types": failed,
        "synced_through": get_setting(session, WATERMARK_SETTING),
    }


def summary(session: Session, account_id: str | None = None, days: int | None = None) -> dict[str, Any]:
    """Account-level fee totals — the honest answer to "what did fees cost me?".

    Deliberately not per-trade (see the module docstring). `total_usd` is None,
    not 0.0, when there is nothing to report, so the UI shows "—": no synced
    activities means we don't KNOW the fees were zero.

    `is_estimate` is true if ANY component was valued from a coin-denominated
    fee at the broker's mark, so the caller can label the figure honestly rather
    than implying broker-exact precision.
    """
    q = session.query(FeeActivity)
    if account_id == "untagged":
        q = q.filter(FeeActivity.account_id.is_(None))
    elif account_id and account_id != "all":
        q = q.filter(FeeActivity.account_id == account_id)
    if days:
        q = q.filter(FeeActivity.day >= (date.today() - timedelta(days=days)).isoformat())
    rows = q.all()

    known = [r for r in rows if r.usd_amount is not None]
    by_symbol: dict[str, float] = {}
    for r in known:
        by_symbol[r.symbol or "—"] = by_symbol.get(r.symbol or "—", 0.0) + r.usd_amount
    return {
        "total_usd": round(sum(r.usd_amount for r in known), 4) if known else None,
        "is_estimate": any(r.usd_is_estimate for r in known),
        "activities": len(rows),
        # Rows Alpaca sent but we could not value in dollars. Surfaced rather
        # than hidden: the total is incomplete by exactly this many.
        "unvalued": len(rows) - len(known),
        "synced_through": get_setting(session, WATERMARK_SETTING),
        "by_symbol": sorted(
            ({"symbol": s, "usd": round(v, 4)} for s, v in by_symbol.items()),
            key=lambda d: -d["usd"],
        ),
    }
