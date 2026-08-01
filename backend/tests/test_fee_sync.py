"""Real broker fees, reconciled from Alpaca account activities.

These protect the honesty rules, not just the plumbing: a fee may never be
double-counted, an unknown fee may never render as zero, and a fee may never be
attributed to a trade Alpaca didn't say it belonged to.
"""

import asyncio
from datetime import date

import pytest

from qt.db import session_scope
from qt.models import FeeActivity, Trade
from qt.services import fees
from qt.settings_service import set_setting

# The documented CFEE shape (docs.alpaca.markets/docs/crypto-fees). Note what is
# absent as much as what is present: no order_id, no side, no time — only a date.
CFEE = {
    "id": "20220812000000000::53be51ba-46f9-43de-b81f-576f241dc680",
    "activity_type": "CFEE",
    "date": "2022-08-12",
    "net_amount": "0",
    "description": "Coin Pair Transaction Fee (Non USD)",
    "symbol": "ETHUSD",
    "qty": "-0.000195",
    "price": "1884.5",
    "status": "executed",
}
# A fee charged in dollars (selling ETH/USD credits you USD, so the fee is USD).
CFEE_USD = {
    "id": "20220812000000000::aaaaaaaa-0000-0000-0000-000000000001",
    "activity_type": "CFEE",
    "date": "2022-08-12",
    "net_amount": "-0.42",
    "description": "Coin Pair Transaction Fee",
    "symbol": "ETHUSD",
    "status": "executed",
}


class FakeAlpaca:
    """Records the windows it was asked for, so the back-fill test can assert on
    them. No network, no credentials."""

    def __init__(self, by_type=None, fail_types=()):
        self.by_type = by_type or {}
        self.fail_types = set(fail_types)
        self.calls = []

    async def account_activities(self, activity_type, after, until, page_size=100):
        self.calls.append((activity_type, after, until))
        if activity_type in self.fail_types:
            raise RuntimeError("alpaca is down")
        return list(self.by_type.get(activity_type, []))


@pytest.fixture()
def clean_fees():
    with session_scope() as s:
        s.query(FeeActivity).delete()
        set_setting(s, fees.WATERMARK_SETTING, "")
        set_setting(s, "current_account_id", "")
    yield


def _sync(client, today=date(2022, 8, 13)):
    with session_scope() as s:
        return asyncio.run(fees.sync(s, client, today=today))


# --- parsing: the payload must never be over-read -------------------------


def test_a_coin_denominated_fee_is_valued_at_the_mark_and_flagged_an_estimate(clean_fees):
    p = fees.parse_activity(CFEE)
    assert p.usd_amount == pytest.approx(0.000195 * 1884.5)
    # The dollar figure came from multiplying by the broker's mark, not from
    # Alpaca. Anything that reports it must be able to say so.
    assert p.usd_is_estimate is True


def test_a_dollar_denominated_fee_is_taken_from_the_broker_and_is_not_an_estimate(clean_fees):
    p = fees.parse_activity(CFEE_USD)
    assert p.usd_amount == pytest.approx(0.42)  # stored as a positive magnitude
    assert p.usd_is_estimate is False


def test_net_amount_zero_means_look_at_qty_not_that_the_trade_was_free(clean_fees):
    # The documented non-USD example carries net_amount "0". Reading that as a
    # zero fee would report a free crypto trade, which Alpaca does not offer.
    assert fees.parse_activity(CFEE).usd_amount > 0


def test_a_fee_we_cannot_value_in_dollars_is_unknown_rather_than_zero(clean_fees):
    p = fees.parse_activity({"id": "x::1", "date": "2022-08-12", "activity_type": "FEE"})
    assert p is not None  # the fee is still recorded — it happened
    assert p.usd_amount is None  # but its size is unknown, and must not become 0.0


def test_an_activity_without_an_id_is_dropped_because_it_cannot_be_deduped(clean_fees):
    # No id means no idempotency key, so it would re-insert on every single run
    # and inflate the total forever. Losing one fee beats that.
    assert fees.parse_activity({"date": "2022-08-12", "net_amount": "-1"}) is None


def test_a_missing_date_falls_back_to_the_timestamp_in_the_activity_id(clean_fees):
    raw = dict(CFEE)
    del raw["date"]
    assert fees.parse_activity(raw).day == "2022-08-12"


def test_garbage_numbers_do_not_crash_the_sync_or_become_zero(clean_fees):
    p = fees.parse_activity({**CFEE, "qty": "n/a", "price": None, "net_amount": ""})
    assert p is not None and p.usd_amount is None


# --- idempotency: the rule that matters most ------------------------------


def test_running_the_sync_twice_does_not_double_the_fees(clean_fees):
    client = FakeAlpaca({"CFEE": [CFEE, CFEE_USD]})
    first = _sync(client)
    second = _sync(client)

    assert first["new"] == 2
    assert second["new"] == 0  # the same activity ids — nothing to insert
    with session_scope() as s:
        assert s.query(FeeActivity).count() == 2
        total = fees.summary(s)["total_usd"]
    # abs=1e-4: the summary rounds to the fourth decimal, which is well below a
    # cent and is the point at which reporting more digits would be false
    # precision on an estimated coin-denominated fee.
    assert total == pytest.approx(0.42 + 0.000195 * 1884.5, abs=1e-4)


def test_the_same_activity_repeated_inside_one_payload_counts_once(clean_fees):
    # Overlapping pages can repeat a row; that must not be a second fee.
    _sync(FakeAlpaca({"CFEE": [CFEE, CFEE, CFEE]}))
    with session_scope() as s:
        assert s.query(FeeActivity).count() == 1


def test_an_overlapping_window_reimports_nothing(clean_fees):
    # The job deliberately re-scans LOOKBACK_DAYS behind the watermark because
    # fees post late. That overlap is only safe if re-seen ids are inert.
    client = FakeAlpaca({"CFEE": [CFEE]})
    _sync(client, today=date(2022, 8, 13))
    _sync(client, today=date(2022, 8, 14))
    _sync(client, today=date(2022, 8, 15))
    with session_scope() as s:
        assert s.query(FeeActivity).count() == 1


# --- windows and back-fill ------------------------------------------------


def test_a_gap_after_downtime_is_back_filled_rather_than_skipped(clean_fees):
    # QT was off for three weeks. Resuming from "today" would lose every fee
    # charged in between, and nothing would ever go back for them.
    with session_scope() as s:
        set_setting(s, fees.WATERMARK_SETTING, "2022-07-01")
    client = FakeAlpaca({"CFEE": []})
    result = _sync(client, today=date(2022, 7, 22))
    after, until = result["window"]
    assert after <= "2022-06-28"  # reaches back past the watermark
    assert until >= "2022-07-22"  # and forward to today


def test_the_first_ever_sync_reaches_back_a_month_not_just_today(clean_fees):
    result = _sync(FakeAlpaca(), today=date(2022, 8, 13))
    assert result["window"][0] <= "2022-07-14"


def test_the_window_is_widened_because_alpaca_treats_the_bounds_as_exclusive(clean_fees):
    # after/until are exclusive, so an un-widened window drops the fees charged
    # on the boundary days themselves.
    after, until = fees.sync_window("2022-08-10", date(2022, 8, 13))
    assert after < "2022-08-07"  # watermark - LOOKBACK, minus one
    assert until > "2022-08-13"


def test_a_corrupt_watermark_falls_back_to_a_backfill_instead_of_wedging(clean_fees):
    after, _ = fees.sync_window("not-a-date", date(2022, 8, 13))
    assert after <= "2022-07-14"


def test_a_watermark_in_the_future_cannot_invert_the_window(clean_fees):
    # A restored backup or a clock skew must not produce after > until, which
    # would quietly return nothing forever.
    after, until = fees.sync_window("2030-01-01", date(2022, 8, 13))
    assert after < until


def test_a_failed_fetch_holds_the_watermark_so_the_gap_is_retried(clean_fees):
    # Advancing past days we never actually read would step over them for good.
    with session_scope() as s:
        set_setting(s, fees.WATERMARK_SETTING, "2022-08-01")
    result = _sync(FakeAlpaca({"CFEE": [CFEE]}, fail_types={"FEE"}), today=date(2022, 8, 13))
    assert result["failed_types"] == ["FEE"]
    assert result["synced_through"] == "2022-08-01"  # not advanced
    with session_scope() as s:
        assert s.query(FeeActivity).count() == 1  # the type that DID work still landed


def test_a_clean_run_advances_the_watermark(clean_fees):
    result = _sync(FakeAlpaca({"CFEE": [CFEE]}), today=date(2022, 8, 13))
    assert result["synced_through"] == "2022-08-13"


def test_both_fee_activity_types_are_requested(clean_fees):
    client = FakeAlpaca()
    _sync(client)
    assert {c[0] for c in client.calls} == {"CFEE", "FEE"}


# --- attribution: fees stay off individual trades -------------------------


def test_syncing_fees_never_writes_a_guess_onto_a_trade(clean_fees):
    # Alpaca's fee activity has no order id and no time — only a date. Two ETH
    # round trips on one day are indistinguishable, so any per-trade split would
    # be inference presented as bookkeeping. Trade.fees must stay null.
    _sync(FakeAlpaca({"CFEE": [CFEE, CFEE_USD]}))
    with session_scope() as s:
        assert s.query(Trade).filter(Trade.fees.isnot(None)).count() == 0


def test_the_account_total_is_unknown_rather_than_zero_before_any_sync(clean_fees):
    # "No fees synced" and "no fees charged" are different claims. Only one of
    # them is something we know.
    with session_scope() as s:
        assert fees.summary(s)["total_usd"] is None


def test_the_summary_admits_when_its_total_is_an_estimate(clean_fees):
    _sync(FakeAlpaca({"CFEE": [CFEE]}))
    with session_scope() as s:
        assert fees.summary(s)["is_estimate"] is True


def test_the_summary_admits_when_its_total_is_exact(clean_fees):
    _sync(FakeAlpaca({"CFEE": [CFEE_USD]}))
    with session_scope() as s:
        assert fees.summary(s)["is_estimate"] is False


def test_fees_we_could_not_value_are_counted_so_the_total_is_not_read_as_complete(clean_fees):
    _sync(FakeAlpaca({"CFEE": [CFEE_USD], "FEE": [{"id": "z::1", "date": "2022-08-12"}]}))
    with session_scope() as s:
        out = fees.summary(s)
    assert out["unvalued"] == 1
    assert out["total_usd"] == pytest.approx(0.42)  # the known part only


def test_fees_are_scoped_to_the_broker_account_that_paid_them(clean_fees):
    with session_scope() as s:
        set_setting(s, "current_account_id", "ACCT-A")
    _sync(FakeAlpaca({"CFEE": [CFEE_USD]}))
    with session_scope() as s:
        assert fees.summary(s, account_id="ACCT-A")["total_usd"] == pytest.approx(0.42)
        assert fees.summary(s, account_id="ACCT-B")["total_usd"] is None


# --- API + schema ---------------------------------------------------------


def test_the_journal_reports_fees_as_unknown_not_zero(client, clean_fees):
    rows = client.get("/api/engine/journal?account=all").json()
    # Every row must carry the key (so the UI can render "—") and none may claim
    # a $0.00 fee it cannot substantiate.
    assert all("fees" in r and r["fees"] is None for r in rows)


def test_the_fees_endpoint_reports_account_level_totals(client, clean_fees):
    _sync(FakeAlpaca({"CFEE": [CFEE_USD]}))
    body = client.get("/api/engine/fees?account=all").json()
    assert body["total_usd"] == pytest.approx(0.42)
    assert body["by_symbol"][0]["symbol"] == "ETHUSD"


def test_the_fee_activities_table_exists(_db):
    from sqlalchemy import inspect

    from qt.db import engine

    assert "fee_activities" in set(inspect(engine).get_table_names())
    assert "fees" in {c["name"] for c in inspect(engine).get_columns("trades")}
