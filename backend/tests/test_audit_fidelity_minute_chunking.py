"""Cutting a long comparison into pieces a MINUTE replay can cover.

The live engine decides every 60 seconds. `_timeframe_for` only asks for minute
bars while a stretch fits inside MAX_HOURS_FOR_MINUTE_REPLAY — and a comparison's
window grows for as long as the strategy stays switched on. So a strategy live
for a day and a bit silently dropped to 15-minute bars and began sampling
fifteen times less often than the engine it was grading.

Measured: strategy 25 compared clean at 1Min over 12 hours; overnight the same
stretch reached 26 hours, fell to 15Min, and three real trades came back as "the
replay missed it — this is the kind that points at a real bug".
"""

from datetime import datetime, timedelta, timezone

from qt.api.backtest import MAX_HOURS_FOR_MINUTE_REPLAY
from qt.api.fidelity import (
    MAX_MINUTE_CHUNKS,
    _chunk_for_minute_replay,
    _config_stretches,
    _Segment,
)

T0 = datetime(2026, 8, 1, 0, 0, tzinfo=timezone.utc)
CAP = timedelta(hours=MAX_HOURS_FOR_MINUTE_REPLAY)


# The VWAP rule needs intraday bars at any window length, so this is a config a
# cut can actually buy minute bars for. Cutting is refused for anything replayed
# on DAILY bars (see _cutting_buys_minute_bars and the test at the bottom), so a
# fixture without it would exercise the refusal rather than the chunking.
INTRADAY = {
    "entry": {"min_day_gain_pct": 3.0, "require_above_vwap": True},
    "exit": {"stop_loss_pct": 4, "trailing_stop_pct": 5, "take_profit_pct": 0},
}
DAILY = {
    "entry": {"min_day_gain_pct": 3.0, "require_above_vwap": False},
    "exit": {"stop_loss_pct": 4, "trailing_stop_pct": 5, "take_profit_pct": 0},
}


def _seg(start_h: float, end_h: float, version: int = 1, params: dict = INTRADAY) -> _Segment:
    return _Segment(
        start=T0 + timedelta(hours=start_h), end=T0 + timedelta(hours=end_h),
        config={"name": f"v{version}", "params": params}, version_no=version,
        symbols=["AAA"], scanner_replay=False,
    )


def _trade(hours: float) -> dict:
    return {"entry_at": (T0 + timedelta(hours=hours)).isoformat()}


def test_a_short_stretch_is_left_exactly_as_it_was():
    """Anti-vacuity: chunking must not fire on the common case."""
    segs = [_seg(0, 5)]
    assert _chunk_for_minute_replay(segs, [_trade(1)]) == segs


def test_a_long_stretch_with_a_trade_is_cut_to_something_minute_bars_can_cover():
    out = _chunk_for_minute_replay([_seg(0, 40)], [_trade(1), _trade(30)])
    assert len(out) > 1
    assert all(s.end - s.start <= CAP for s in out), \
        "a piece longer than the cap cannot get minute bars — the whole point"


def test_trade_free_stretches_are_left_long_so_nobody_pays_for_minute_bars():
    """Minute bars run 1,440 per symbol per day. A strategy live for months must
    not ask for millions of them to compare a handful of trades."""
    out = _chunk_for_minute_replay([_seg(0, 24 * 30)], [_trade(1)])
    assert len(out) == 2, [(s.start, s.end) for s in out]
    assert out[0].end - out[0].start <= CAP        # the piece holding the trade
    assert out[1].end - out[1].start > CAP         # the empty month, left whole


def test_the_piece_holding_the_trade_is_the_one_kept_small():
    out = _chunk_for_minute_replay([_seg(0, 24 * 5)], [_trade(80)])
    holding = [s for s in out if s.start <= T0 + timedelta(hours=80) < s.end]
    assert len(holding) == 1
    assert holding[0].end - holding[0].start <= CAP


def test_each_piece_keeps_its_own_configuration():
    """A stretch replayed under version 2 must stay version 2 — chunking is about
    resolution, and must never quietly re-date a configuration."""
    out = _chunk_for_minute_replay([_seg(0, 40, version=2)], [_trade(1), _trade(30)])
    assert {s.version_no for s in out} == {2}
    assert all(s.symbols == ["AAA"] for s in out)


def test_the_pieces_tile_the_original_span_without_gap_or_overlap():
    out = _chunk_for_minute_replay([_seg(0, 100)], [_trade(5), _trade(60)])
    assert out[0].start == T0
    assert out[-1].end == T0 + timedelta(hours=100)
    for a, b in zip(out, out[1:]):
        assert a.end == b.start, "a gap or overlap would lose or double-count trades"


def test_only_the_first_piece_counts_as_a_configuration_stretch():
    """`segmented` means "your strategy changed mid-window". Cutting for
    resolution must not start claiming edits the user never made."""
    out = _chunk_for_minute_replay([_seg(0, 40)], [_trade(1), _trade(30)])
    assert out[0].resolution_chunk is False
    assert all(s.resolution_chunk for s in out[1:])
    assert len(_config_stretches(out)) == 1


def test_a_stretch_replayed_on_daily_bars_is_not_cut_at_all():
    """Cutting a daily replay into 24-hour pieces does not refine it, it CONVERTS
    it: `_timeframe_for` puts a window shorter than MIN_HOURS_FOR_DAILY_REPLAY on
    intraday bars, because a daily bar is stamped at the start of its day and a
    window of hours contains none. So every piece would ask for intraday bars
    that a MACD/RSI strategy has no signals for — measured when chunking first
    reached the replay: four segment comparisons went from matching their trades
    to matching none of them."""
    month = _seg(0, 24 * 30, params=DAILY)
    assert _chunk_for_minute_replay([month], [_trade(24 * d + 5) for d in range(30)]) == [month]
    # The guard is about the RESOLUTION, not about the length: the same window
    # under a strategy that replays intraday is cut.
    assert len(_chunk_for_minute_replay([_seg(0, 24 * 30)], [_trade(5)])) > 1


def test_the_number_of_minute_sized_pieces_is_capped():
    """Each piece is a separate replay — its own dataset, its own fetch, its own
    day of 1,440 bars per symbol. A strategy that has traded every day since May
    would set off ninety of them on one click, which is a quarter of an hour of
    waiting for resolution nobody asked about that far back."""
    days = MAX_MINUTE_CHUNKS * 3
    out = _chunk_for_minute_replay(
        [_seg(0, 24 * days)], [_trade(24 * d + 5) for d in range(days)]
    )
    small = [s for s in out if s.end - s.start <= CAP]
    assert len(small) == MAX_MINUTE_CHUNKS, [(s.start, s.end) for s in out]


def test_the_pieces_kept_small_are_the_most_recent_ones():
    """A comparison is nearly always read about a strategy running NOW, so the
    resolution goes where the reader is looking. The older trades are still
    compared — on the coarser bars their coalesced stretch gets."""
    days = MAX_MINUTE_CHUNKS + 4
    out = _chunk_for_minute_replay(
        [_seg(0, 24 * days)], [_trade(24 * d + 5) for d in range(days)]
    )
    small = [s for s in out if s.end - s.start <= CAP]
    assert small[-1].end == T0 + timedelta(hours=24 * days)
    assert small[0].start >= T0 + timedelta(hours=24 * 4), \
        "the oldest days were kept small and the newest coalesced — backwards"


def test_every_piece_gets_its_own_journal_rather_than_the_stretch_s():
    """`dataclasses.replace` copies field VALUES. Every piece cut from one
    stretch was handed the SAME list, so filing a trade under one chunk filed it
    under all of them — each chunk was seeded with the whole window's trades,
    every stretch reported the window's trade count as its own, and a replay
    failure recorded on one chunk reached its neighbours' trades."""
    out = _chunk_for_minute_replay([_seg(0, 40)], [_trade(1), _trade(30)])
    assert len(out) > 1
    out[0].live.append({"symbol": "AAA"})
    assert [len(s.live) for s in out[1:]] == [0] * len(out[1:])
    out[0].rails_seed["AAA"] = T0
    assert [s.rails_seed for s in out[1:]] == [{}] * len(out[1:])


def test_two_real_configurations_stay_two_after_chunking():
    out = _chunk_for_minute_replay(
        [_seg(0, 40, version=1), _seg(40, 80, version=2)],
        [_trade(1), _trade(30), _trade(50)],
    )
    stretches = _config_stretches(out)
    assert [s.version_no for s in stretches] == [1, 2]
    assert stretches[0].start == T0
    assert stretches[-1].end == T0 + timedelta(hours=80)
