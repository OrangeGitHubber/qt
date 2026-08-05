"""Backtest fidelity: does the replay actually reproduce what really happened?

A backtest is only worth the trust you put in it, and the only way to earn that
trust is to point it at a period you ALREADY traded and check whether it agrees
with reality. This module does the comparison.

QT is unusually well placed for it. `evaluate_entry`, `evaluate_exit` and
`check_rails` are the SAME functions in the live engine and the backtester, so
the two cannot disagree about strategy logic — it is literally one implementation.
Anything that does diverge is therefore either DATA (the replay saw a different
market) or EXECUTION (the same decision filled at a different price). Those need
opposite fixes, so they are reported apart:

  DECISION FIDELITY — did the replay pick the same names on the same days, and
  leave for the same reasons? A mismatch here means the replay's view of history
  is wrong: missing bars, a different day boundary, an indicator read off the
  wrong series. Every bug found in the replay this month lived here.

  EXECUTION FIDELITY — given the same decision, how far off was the fill? This is
  the spread/fee/slippage model, and its error is a straight, measurable bias in
  every backtest you have ever run.

Deliberately NOT a score. The output is four buckets and a set of measured
differences, because "87% accurate" invites you to stop reading, and the useful
information is always in which trades disagreed and why.

Matching is by (symbol, entry day), never by timestamp. A live entry at 14:03:11
and a replayed one on the 15-minute bar starting 14:00 are the same decision;
demanding equal timestamps would report every single trade as a mismatch.
"""

from __future__ import annotations

from datetime import datetime
from statistics import median

# Below this many observations a difference is an anecdote, not a measurement.
# Named rather than inlined because the same bar has to be applied in two
# different places to two different samples, and an inlined `30` in one of them
# was how `suggested_spread_pct` came to be published off two fills while the
# very next field admitted the sample was too thin to judge.
MIN_MATCHES_TO_JUDGE = 30
MIN_FILLS_TO_JUDGE = 30


def _pct_delta(live: float | None, sim: float | None) -> float | None:
    """How far the simulated price sat from the live one, as a % of the live
    price. Positive = the backtest got a BETTER price than reality (it flatters
    itself); negative = it was more pessimistic than real life."""
    # A ZERO on either side is a missing price, never a real one — nothing fills
    # at $0. The `sim == 0` half is not hypothetical: serialization used to round
    # every price to four decimals, so a SHIB/USD fill near $0.00001 arrived here
    # as 0.0 and this returned a clean, plausible -100%, which then went into the
    # slippage median that the backtest's spread setting is copied from. The
    # rounding is fixed at source (backtest._price); this is the second lock,
    # because a fabricated -100% is far worse than a gap in the sample.
    if live in (None, 0) or sim in (None, 0):
        return None
    return round((sim - live) / live * 100, 4)


# Exits the replay CANNOT reproduce, because no strategy rule produced them:
# you pressed a button, the account was reset, or reconciliation found the broker
# no longer holding the position. The engine writes each with a fixed prefix.
_NON_STRATEGY_EXITS = ("force-closed", "manual liquidation", "reconciled:")


def _is_strategy_exit(reason: str | None) -> bool:
    """Whether an exit came from the strategy's own rules.

    A force exit is a HUMAN decision. The backtester has no way to know you
    clicked sell on a Tuesday, so it will always disagree — and counting that as
    a disagreement is doubly wrong:

      - it drags the exit-rule and exit-day agreement down as though the replay's
        exit logic were faulty, when the replay never had a chance;
      - and far worse, the price difference between your discretionary exit and
        the rule-based one lands in the SLIPPAGE median. That number exists to be
        typed into the backtest's spread setting, so polluting it with a
        different decision would push every future backtest wrong on the basis of
        a button press.

    The ENTRY of such a trade is untouched by this: it was a genuine strategy
    decision and its fill is real slippage. Only the exit half is set aside."""
    if not reason:
        return True  # nothing recorded — treat as ordinary rather than special-case it away
    low = reason.strip().lower()
    return not any(low.startswith(p) for p in _NON_STRATEGY_EXITS)


def _key(symbol: str, day: str | None) -> tuple[str, str]:
    return (symbol.upper(), day or "")


def compare(
    live_trades: list[dict],
    backtest_result: dict,
    *,
    assumed_spread_pct: float = 0.0,
    assumed_fee_pct: float = 0.0,
    replayed_symbols: list[str] | None = None,
    seeded_symbols: list[str] | None = None,
    timing_tolerance_seconds: float | None = None,
    bar_seconds: float | None = None,
) -> dict:
    """Diff what really happened against what the replay says would have.

    `live_trades` are journal rows: symbol, entry_day, exit_day, entry_price,
    exit_price, pnl, status ("open"/"closed"/"rejected"), entry_reason,
    exit_reason. Rejected rows matter as much as filled ones — they are how a
    backtest-only trade is told apart from a rail doing its job.

    `seeded_symbols` are names that reached the replay's universe because THESE
    trades put them there, not because the replay reconstructed them (see
    qt.api.fidelity._seed_by_day). The distinction has to survive into the log:
    "the replay was watching this and passed" is a much weaker claim when the
    only reason it was watching is that we told it to — and a much stronger one
    about the signal, since the coverage excuse is gone.

    Every number is rounded where it is produced, so the API layer has nothing
    to decide and the UI cannot render 14 decimal places of false precision.
    """
    universe = (
        {s.upper() for s in replayed_symbols} if replayed_symbols else None
    )
    seeded = {s.upper() for s in (seeded_symbols or [])}
    sim_trades = list(backtest_result.get("trade_list") or [])
    sim_open = list(backtest_result.get("open_positions") or [])

    # A position still open at the window's end is a real decision — the entry
    # happened. Excluding it would count every held winner as "the backtest
    # missed this trade", which is the opposite of the truth.
    sim_by_key: dict[tuple[str, str], list[dict]] = {}
    for t in sim_trades + sim_open:
        sim_by_key.setdefault(_key(t.get("symbol", ""), t.get("entry_day")), []).append(t)

    live_filled = [t for t in live_trades if t.get("status") in ("open", "closed")]
    live_rejected = [t for t in live_trades if t.get("status") == "rejected"]
    live_by_key: dict[tuple[str, str], list[dict]] = {}
    for t in live_filled:
        live_by_key.setdefault(_key(t.get("symbol", ""), t.get("entry_day")), []).append(t)
    # PAIRED NEAREST IN TIME, not first-come. Both sides can trade one symbol more
    # than once in a day — a crypto day is 24 hours, so this is ordinary — and
    # keeping whichever the replay took FIRST paired the wrong two.
    #
    # Measured on AVAX/USD, 2026-08-04. The replay traded it twice: 00:38→01:40
    # for +5.15%, then 01:40→02:08 for -1.04%. Live traded it once, 01:42→01:48
    # for -1.13% — the same price to a tenth of a cent as the replay's SECOND
    # entry, two minutes apart, the same rule firing. The report paired it with
    # the first and announced "Both sold AVAX the same day, but you on stop-loss
    # and the replay on take-profit": two systems that agreed, described as
    # opposites. And the replay's first trade — one it took and live did not,
    # which is the definition of an invented trade — vanished, because only the
    # paired row survived to be reported.
    #
    # The DAY key itself is untouched and right: a live fill at 14:03 and a 14:00
    # bar are the same decision, and demanding equal timestamps would report
    # every trade as a mismatch. What was wrong is which of several candidates a
    # key resolves to.
    pairs, unpaired_live, unpaired_sim = _pair_by_nearest(live_by_key, sim_by_key)
    # Which (symbol, day) pairs the engine WANTED but a rail refused. Without
    # this, every correctly-blocked trade would be filed as a backtest error.
    rejected_keys = {_key(t.get("symbol", ""), t.get("entry_day")) for t in live_rejected}

    matched: list[dict] = []
    live_only: list[dict] = []
    for key, live, sim in (
        [(k, t, None) for k, t in unpaired_live] + [(k, l, s) for k, l, s in pairs]
    ):
        if sim is None:
            live_only.append(
                {
                    "symbol": live.get("symbol"),
                    "day": key[1],
                    "entry_price": live.get("entry_price"),
                    "pnl": live.get("pnl"),
                    "entry_reason": live.get("entry_reason"),
                    "entry_at": live.get("entry_at"),
                    "exit_at": live.get("exit_at"),
                    "exit_day": live.get("exit_day"),
                    "exit_reason": live.get("exit_reason"),
                    # Whether the replay was even LOOKING at this symbol. A name
                    # outside the replayed universe was never going to be found,
                    # so calling it "the backtest missed a trade" blames the
                    # replay for being pointed somewhere else — usually because
                    # the strategy's universe was edited after these trades, or
                    # the comparison resolved a different one.
                    # A stretch whose replay ERRORED reported no universe at all,
                    # so nothing is known about this symbol's coverage there —
                    # and the merged universe below belongs to the stretches that
                    # DID run. Letting those answer for this one is how "the
                    # replay was watching this symbol and passed" came to be said
                    # about a window no replay ever covered.
                    "in_replayed_universe": (
                        None
                        if universe is None or live.get("replay_error")
                        else live.get("symbol", "").upper() in universe
                    ),
                    # And whether it was only there because this comparison put
                    # it there. See `seeded_symbols`.
                    "universe_seeded": live.get("symbol", "").upper() in seeded,
                    # WAS THE REPLAY ALREADY HOLDING IT at that moment? Then it
                    # did not pass on the signal — `run_backtest` refuses the
                    # candidate outright ("this strategy already holds this
                    # symbol") before any entry rule is read. Measured on SPY:
                    # live rotated in and out three times in thirty-five minutes
                    # on relative-strength ranking while the replay held one
                    # position through, and every re-entry came back "the replay
                    # was watching this symbol and passed — the kind that points
                    # at a real bug". It is the replay's EXIT that differed; the
                    # entry could not have happened either way.
                    "replay_held_it": _held_at(
                        sim_trades + sim_open, live.get("symbol"), live.get("entry_at")
                    ),
                    # Why there is nothing to compare this against: the stretch
                    # it falls in did not replay. Set by the API layer, which is
                    # the half that knows a window was split at all.
                    "replay_error": live.get("replay_error"),
                }
            )
            continue
        # HOW FAR APART THE TWO SIDES OPENED IT, and whether that is more than
        # the sampling difference can account for. Computed before the row rather
        # than inside it because BOTH the entry verdict and `exit_comparable`
        # turn on it — an exit is only judgeable when the trade under it is the
        # same trade. See `timing_tolerance_seconds`.
        gap = _entry_gap_seconds(live.get("entry_at"), sim.get("entry_at"))
        timing_differs = (
            gap is not None
            and timing_tolerance_seconds is not None
            and gap > max(timing_tolerance_seconds, ENTRY_TIMING_FLOOR_SECONDS)
        )
        matched.append(
            {
                "symbol": live.get("symbol"),
                "day": key[1],
                "live_entry": live.get("entry_price"),
                "sim_entry": sim.get("entry_price"),
                "entry_delta_pct": _pct_delta(live.get("entry_price"), sim.get("entry_price")),
                "live_exit": live.get("exit_price"),
                "sim_exit": sim.get("exit_price"),
                "exit_delta_pct": _pct_delta(live.get("exit_price"), sim.get("exit_price")),
                "live_exit_day": live.get("exit_day"),
                "sim_exit_day": sim.get("exit_day"),
                # Same trade, different day out: the replay's exit rules fired at
                # a different moment, which is a DECISION difference even though
                # the entry agreed.
                "exit_day_matches": bool(live.get("exit_day"))
                and live.get("exit_day") == sim.get("exit_day"),
                "live_pnl": live.get("pnl"),
                "sim_pnl": sim.get("pnl"),
                "live_entry_at": live.get("entry_at"),
                "sim_entry_at": sim.get("entry_at"),
                "live_exit_at": live.get("exit_at"),
                "sim_exit_at": sim.get("exit_at"),
                "live_exit_reason": live.get("exit_reason"),
                "sim_exit_reason": sim.get("exit_reason"),
                # False when a human or the broker ended this trade, not a rule —
                # and false when the two sides did not open the SAME trade. A
                # trailing stop trails from the entry price, so two positions
                # opened hours apart carry different stop levels and are simply
                # different trades; judging one exit against the other reports
                # the entry difference a second time, dressed as an exit fault.
                #
                # Measured: SOL's live trade ran 00:57→08:27 while its paired
                # replay position was not bought until 13:55, five hours after
                # live had already sold. "The replay was still holding it when
                # the window ended" was true and told nobody anything.
                #
                # The entry still counts either way; every exit-side comparison
                # skips it.
                "exit_comparable": _is_strategy_exit(live.get("exit_reason"))
                and not live.get("spans_segment_boundary")
                and not timing_differs,
                # Set when the comparison was SEGMENTED (the strategy was edited
                # mid-window, so each stretch is replayed with its own config) and
                # this trade opened in one stretch and closed in another. No
                # segment's replay can reproduce that: each starts with no
                # positions and stops at its own end. Same treatment as a
                # hand-closed exit — the entry counts, the exit is set aside —
                # but a different claim, so it is counted apart from those.
                "exit_spans_boundary": bool(live.get("spans_segment_boundary")),
                # Reasons are prose with numbers in them, so compare the RULE
                # that fired (its first few words), not the whole sentence.
                "exit_reason_matches": _same_rule(
                    live.get("exit_reason"), sim.get("exit_reason")
                ),
                # HOW FAR APART THE TWO SIDES OPENED IT, and whether that is more
                # than the sampling difference can account for. `entry_day` is
                # what pairs them and a crypto day is 24 hours long, so this is
                # the only thing standing between "same trade" and "same trade,
                # most of a day later". See `timing_tolerance_seconds`.
                "entry_gap_seconds": gap,
                "entry_timing_differs": timing_differs,
            }
        )

    backtest_only: list[dict] = []
    rails_blocked: list[dict] = []
    for key, sim in unpaired_sim:
        row = {
            "symbol": sim.get("symbol"),
            "day": key[1],
            "sim_entry": sim.get("entry_price"),
            "sim_pnl": sim.get("pnl"),
            "sim_entry_at": sim.get("entry_at"),
            "sim_exit_reason": sim.get("exit_reason"),
        }
        if key in rejected_keys:
            # NOT a backtest error: the engine wanted this trade and a rail said
            # no (daily loss cap, trade-rate limiter, wash-sale guard, no cash).
            # The replay applies the same rails but from a different starting
            # state, so it can legitimately have room where live did not.
            row["blocked_by"] = next(
                (
                    t.get("entry_reason")
                    for t in live_rejected
                    if _key(t.get("symbol", ""), t.get("entry_day")) == key
                ),
                None,
            )
            rails_blocked.append(row)
        else:
            backtest_only.append(row)

    return {
        # DECISIONS THIS COMPARISON COULD NOT SEE — now structurally none, and
        # kept as the tripwire that says so.
        #
        # A symbol traded twice in one day used to collapse to a single key: the
        # live dict kept the later row, the sim dict the earlier one, and the
        # rest disappeared from every bucket, from the log and from
        # `live_trades`. The report described fewer decisions than were made
        # without saying so, and for a round-the-clock crypto strategy that was
        # not hypothetical — AVAX on 2026-08-04 lost a genuinely invented trade
        # that way while its real counterpart was mispaired into a false
        # disagreement.
        #
        # `_pair_by_nearest` matches the groups one-to-one and reports the
        # leftovers as misses and inventions, so every row now lands somewhere
        # and these should both read zero. They are computed rather than
        # hardcoded precisely so that a future pairing bug shows up here as a
        # number instead of as a quietly shorter report.
        "same_day_duplicates": {
            "live": len(live_filled) - len(pairs) - len(unpaired_live),
            "backtest": len(sim_trades) + len(sim_open) - len(pairs) - len(unpaired_sim),
        },
        # The comparison told trade by trade, in order, each with a verdict in
        # plain words. The buckets above summarise; this is the thing you can
        # actually read and act on — "the replay bought it a day late" is a bug
        # report, "48 invented" is a number.
        "log": _trade_log(matched, live_only, backtest_only, bar_seconds),
        "matched": sorted(matched, key=lambda r: (r["day"], r["symbol"])),
        "live_only": sorted(live_only, key=lambda r: (r["day"], r["symbol"])),
        "backtest_only": sorted(backtest_only, key=lambda r: (r["day"], r["symbol"])),
        "rails_blocked": sorted(rails_blocked, key=lambda r: (r["day"], r["symbol"])),
        "decision": _decision_stats(matched, live_only, backtest_only),
        # What the replay was actually pointed at. Without this a universe
        # mismatch is invisible and reads as a broken backtest.
        "replayed_symbols": sorted(universe) if universe else [],
        "execution": _execution_stats(matched, assumed_spread_pct, assumed_fee_pct),
    }


def _same_rule(live_reason: str | None, sim_reason: str | None) -> bool | None:
    """Whether the same EXIT RULE fired, ignoring the numbers in the sentence.

    "stop-loss: -4.10% <= -4%" and "stop-loss: -4.32% <= -4%" are the same rule
    doing the same job on slightly different prices; calling that a mismatch
    would bury the real disagreements — a trailing stop where live took a
    take-profit — under noise.

    Split on the colon rather than counting words: every reason the engine writes
    is "<rule>: <detail>", so the colon IS the boundary, and rule names run from
    one word ("stop-loss") to three ("ATR stop-loss", "max holding period"). A
    fixed word count would either cut those short or swallow the first number."""
    if not live_reason or not sim_reason:
        return None
    def head(s: str) -> str:
        return s.split(":")[0].strip().lower()
    return head(live_reason) == head(sim_reason)


# BELOW THIS, TWO ENTRIES ARE THE SAME EVENT — a judgement about noise, not a
# physical constant, and the only number in this file that is neither measured
# nor derived.
#
# The derived tolerance (one bar + one poll) is 120 seconds on a 1-minute replay,
# and real gaps land right on top of it: XRP at 118s read "match" while ADA at
# 129s read "timing differs", two rows a reader cannot tell apart given opposite
# verdicts by three seconds. The band is noisy for a reason nothing models —
# live's `entry_at` is a FILL and the replay's is a BAR CLOSE, with a 60-second
# poll at an unrecorded phase and an order-to-fill delay in between, all of which
# pushes live later than the replay without either side disagreeing.
#
# Five minutes is chosen to sit clear of that band while staying far below the
# smallest gap worth reading — the measured ones were 18 minutes, 51 minutes, 2
# hours and 13 hours. Crying wolf at 2 minutes is what buries those.
ENTRY_TIMING_FLOOR_SECONDS = 300.0


def _pair_by_nearest(
    live_by_key: dict[tuple[str, str], list[dict]],
    sim_by_key: dict[tuple[str, str], list[dict]],
) -> tuple[list[tuple[tuple[str, str], dict, dict]], list[tuple[tuple[str, str], dict]],
           list[tuple[tuple[str, str], dict]]]:
    """Match each (symbol, day) group one-to-one, closest entry times first.

    Greedy on the smallest gap, which is right here rather than merely simple:
    the groups are tiny (a handful of trades in one symbol on one day) and the
    nearest pair is the one nobody would argue about. Taking it first cannot
    strand a better pairing for the rows left over, because any other assignment
    would have to move that pair further apart to bring another closer.

    Rows with no usable timestamp on either side pair in ORDER, after every timed
    pair is settled. An imported journal carries days rather than moments and
    nearest-in-time cannot rank those — order is what the old code effectively
    used for everything, and it is still the best available answer when there is
    nothing finer to go on.

    Returns (pairs, unpaired_live, unpaired_sim): the leftovers are trades one
    side made and the other did not, and they belong in the report as misses and
    inventions rather than dropped."""
    pairs: list[tuple[tuple[str, str], dict, dict]] = []
    left_over_live: list[tuple[tuple[str, str], dict]] = []
    left_over_sim: list[tuple[tuple[str, str], dict]] = []

    for key in live_by_key.keys() | sim_by_key.keys():
        lives = list(live_by_key.get(key) or [])
        sims = list(sim_by_key.get(key) or [])
        candidates = sorted(
            (
                (gap, li, si)
                for li, live in enumerate(lives)
                for si, sim in enumerate(sims)
                if (gap := _entry_gap_seconds(live.get("entry_at"), sim.get("entry_at")))
                is not None
            ),
            key=lambda c: (c[0], c[1], c[2]),
        )
        used_live: set[int] = set()
        used_sim: set[int] = set()
        for gap, li, si in candidates:
            if li in used_live or si in used_sim:
                continue
            used_live.add(li)
            used_sim.add(si)
            pairs.append((key, lives[li], sims[si]))
        rest_live = [t for i, t in enumerate(lives) if i not in used_live]
        rest_sim = [t for i, t in enumerate(sims) if i not in used_sim]
        for live, sim in zip(rest_live, rest_sim):
            pairs.append((key, live, sim))
        left_over_live += [(key, t) for t in rest_live[len(rest_sim):]]
        left_over_sim += [(key, t) for t in rest_sim[len(rest_live):]]
    return pairs, left_over_live, left_over_sim


def _held_at(sim_rows: list[dict], symbol: str | None, when: str | None) -> bool:
    """Was the replay holding `symbol` at `when`?

    A position it opened earlier and had not closed yet, which for an unclosed
    one means all the way to the window's end. Interval is half-open at the
    close, matching the simulator: an exit frees the symbol on the bar it
    happens, and the engine may re-enter on the next."""
    if not symbol or not _has_clock(when):
        return False
    for row in sim_rows:
        if (row.get("symbol") or "").upper() != symbol.upper():
            continue
        opened = _entry_gap_seconds(when, row.get("entry_at"))
        if opened is None or _is_after(row.get("entry_at"), when):
            continue  # opened after this moment, so it was not held yet
        if row.get("exit_at") is None or _is_after(row.get("exit_at"), when):
            return True
    return False


def _is_after(iso: str | None, other: str | None) -> bool:
    """`iso` strictly later than `other`; False when either cannot be read."""
    gap = _entry_gap_seconds(iso, other)
    if gap is None or gap == 0:
        return False
    try:
        a = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        b = datetime.fromisoformat(str(other).replace("Z", "+00:00"))
    except ValueError:
        return False
    return a > b


def _entry_gap_seconds(live_at: str | None, sim_at: str | None) -> float | None:
    """How far apart the two sides opened the same trade, in seconds, or None
    when either lacks a usable instant."""
    if not (_has_clock(live_at) and _has_clock(sim_at)):
        return None
    try:
        a = datetime.fromisoformat(str(live_at).replace("Z", "+00:00"))
        b = datetime.fromisoformat(str(sim_at).replace("Z", "+00:00"))
    except ValueError:
        return None
    if (a.tzinfo is None) != (b.tzinfo is None):
        # One naive, one aware: subtracting raises, and guessing a zone for the
        # naive one would invent an offset. Unknown is the honest answer.
        return None
    return abs((a - b).total_seconds())


def _gap_words(seconds: float) -> str:
    """The gap in the coarsest unit that still says something, for a sentence
    the reader can act on. Never a clock — see _has_clock for why the server
    must not format one."""
    if seconds < 90:
        return f"{round(seconds)} seconds"
    if seconds < 5400:
        minutes = round(seconds / 60)
        return f"{minutes} minute{'' if minutes == 1 else 's'}"
    hours = round(seconds / 3600)
    return f"{hours} hour{'' if hours == 1 else 's'}"


def _has_clock(iso: str | None) -> bool:
    """Whether this timestamp carries a time of day at all.

    It used to RETURN one — `iso.split("T")[1][:5]`, sliced straight out of the
    UTC string and pasted into the sentence. The row's own "When" column is
    converted to the reader's chosen zone by the frontend, so one row said
    "10:01" in the column and "the replay was 14:01" in the text: the same
    instant, four hours apart, in one line.

    The server cannot format a clock, because it does not know which zone the
    page is being read in — that is the whole point of the display-timezone
    setting, and this file was the one place still ignoring it. So the instants
    go out as ISO and the frontend formats them with the same converter it uses
    for the column. All this decides is whether there is a time worth showing:
    a daily replay has none, and inventing one would imply precision it lacks.
    """
    return bool(iso and "T" in iso)


def _bar_words(seconds: float | None) -> str | None:
    """The replay's bar length in words, or None when it is not known."""
    if not seconds:
        return None
    if seconds < 3600:
        minutes = int(round(seconds / 60))
        return f"{minutes} minute{'' if minutes == 1 else 's'}"
    if seconds < 86_400:
        hours = int(round(seconds / 3600))
        return f"{hours} hour{'' if hours == 1 else 's'}"
    return "a day"


# WHY A MISSED ENTRY MIGHT NOT BE A SIGNAL DIFFERENCE AT ALL, said on the row
# rather than left for the reader to deduce.
#
# The simulator is asymmetric, and deliberately so. `evaluate_exit` is handed
# `bar_high` and `bar_low` and `_fill_price` clamps into the bar's range, so a
# stop breached mid-bar fires — an exit cannot hide inside a bar. `evaluate_entry`
# gets `price=bar["close"]` and a `change_pct` computed from that close, and no
# high ever reaches it. A move that appeared and vanished inside one bar is
# therefore invisible to an ENTRY while the live engine, looking every sixty
# seconds, could act on it.
#
# Measured before minute bars existed: FIL bought live at 13:18:18, between the
# replay's 13:15 and 13:30 bars, reported as a trade the replay missed. Finer
# bars shrink that window; they never close it.
#
# Naming it does not soften the verdict. The trade really was not reproduced —
# what changes is that the reader can tell "your rules disagree" from "your rules
# could not be evaluated at this resolution", which are opposite things to do
# next. Judging entries on the bar's HIGH instead would make the replay strictly
# more permissive and start inventing trades on wicks, which is a worse error
# than the one being explained.
# Two possibilities, named as two rather than one asserted and then argued
# against. The first version appended this to a sentence ending "this is the kind
# that points at a real bug", so the row claimed a bug and undercut itself in the
# next breath — which is not something a reader can act on either way.
_CLOSE_ONLY_CAVEAT = (
    " Either its rules genuinely disagreed with yours, or the price qualified at a moment"
    " between bar closes: the replay judges each bar at its CLOSE and its bars were {bar}"
    " long, while your engine looked every 60 seconds at an offset nothing records. The"
    " shorter the bar, the less room there is for the second explanation. Exits are checked"
    " against each bar's high and low, so this can only affect entries."
)
# What the row says when the resolution is UNKNOWN: there is no second
# possibility to offer, so the strong claim stands on its own rather than being
# hedged against a caveat nothing supports.
_UNQUALIFIED_MISS = " This is the kind that points at a real bug."


def _close_only_note(bar_seconds: float | None) -> str:
    """The caveat, or nothing at all when the resolution is unknown — an unknown
    bar size cannot support a claim about what a bar could hide."""
    bar = _bar_words(bar_seconds)
    return _CLOSE_ONLY_CAVEAT.format(bar=bar) if bar else ""


def _trade_log(matched: list[dict], live_only: list[dict], backtest_only: list[dict],
               bar_seconds: float | None = None) -> list[dict]:
    """Every buy and sell either side made, as separate events at their own
    timestamps.

    One event per action, not one row per trade: a position bought on Monday and
    sold on Thursday is two things that happened, and collapsing them into a row
    dated Monday hides when the sell actually landed. Timestamps are exact and
    unrounded — "the replay sold three hours later" and "the replay sold a day
    later" are different findings, and a day-grouped log cannot tell them apart.
    """
    rows: list[dict] = []

    def event(at, day, symbol, action, verdict, detail, live_at=None, sim_at=None):
        # live_at/sim_at go out as ISO instants, never as formatted clocks: only
        # the browser knows which timezone the reader chose. Present ONLY when
        # the two differ meaningfully and both are timed.
        row = {"at": at, "day": day, "symbol": symbol, "action": action,
               "verdict": verdict, "detail": detail}
        if live_at and sim_at and live_at != sim_at:
            row["live_at"] = live_at
            row["sim_at"] = sim_at
        rows.append(row)

    for m in matched:
        live_in, sim_in = m.get("live_entry_at"), m.get("sim_entry_at")
        both_timed = _has_clock(live_in) and _has_clock(sim_in)
        # SAME TRADE, POSSIBLY NOT THE SAME MOMENT. Pairing is by (symbol, day)
        # and a crypto day is 24 hours, so "match" was being printed over gaps of
        # thirteen and fifteen hours in exactly the same green as a two-minute
        # one. Both instants were on the row the whole time; the verdict simply
        # did not read them. See `timing_tolerance_seconds`.
        late = m.get("entry_timing_differs")
        detail = (
            f"Both bought {m['symbol']}, but {_gap_words(m['entry_gap_seconds'])} apart."
            if late
            else f"Both bought {m['symbol']}." if both_timed
            else f"Both bought {m['symbol']} at the same point."
        )
        event(
            live_in or m["day"], m["day"], m["symbol"], "bought",
            "timing differs" if late else "match", detail,
            live_at=live_in if both_timed else None,
            sim_at=sim_in if both_timed else None,
        )
        if m.get("entry_timing_differs") and m["live_exit_day"]:
            # Set aside for the ENTRY's sake, not the exit's. A trailing stop
            # trails from the entry price, so two positions opened this far apart
            # carry different stop levels and are different trades — judging one
            # exit against the other reports the entry difference twice.
            event(m.get("live_exit_at") or m["live_exit_day"], m["live_exit_day"], m["symbol"],
                  "sold", "not compared",
                  f"You sold {m['symbol']} ({m['live_exit_reason'] or 'no reason recorded'}). "
                  f"The replay's position was opened {_gap_words(m['entry_gap_seconds'])} apart "
                  "from yours, so this is a different trade and its exit isn't judged against it.")
        elif m["exit_comparable"] is False:
            event(m.get("live_exit_at") or m["live_exit_day"], m["live_exit_day"], m["symbol"],
                  "sold", "not compared",
                  f"You closed {m['symbol']} by hand, so its exit isn't judged against the replay.")
        elif m["live_exit_day"] and not m["sim_exit_day"]:
            event(m.get("live_exit_at") or m["live_exit_day"], m["live_exit_day"], m["symbol"],
                  "sold", "replay never sold",
                  f"You sold {m['symbol']} ({m['live_exit_reason']}). The replay was still "
                  "holding it when the window ended.")
        elif m["exit_day_matches"] and m["exit_reason_matches"] is not False:
            live_x, sim_x = m.get("live_exit_at"), m.get("sim_exit_at")
            timed = _has_clock(live_x) and _has_clock(sim_x)
            # "same reason" used to be the fallback when a reason was MISSING on
            # one side, so a row whose reasons were never compared still went out
            # asserting they agreed. `exit_reason_matches` is None exactly then;
            # say so instead of claiming a comparison nobody made.
            detail = (
                f"Both sold {m['symbol']}, {m['sim_exit_reason']}."
                if m["exit_reason_matches"] is True
                else f"Both sold {m['symbol']} on the same day. Only one side recorded why, "
                     "so the reasons weren't compared."
            )
            event(live_x or m["live_exit_day"], m["live_exit_day"], m["symbol"], "sold",
                  "match" if m["exit_reason_matches"] is True else "same day, reason unknown",
                  detail,
                  live_at=live_x if timed else None, sim_at=sim_x if timed else None)
        elif not m["live_exit_day"] and not m["sim_exit_day"]:
            # NEITHER side has exited — both are still holding. There is no exit
            # to compare, so there is no row. `exit_day_matches` is false when
            # both are None (it demands two real days), so this fell through to
            # the timing branch and printed "You sold AAVE/USD on None (); the
            # replay held until None (None)" — a sale that didn't happen, on
            # neither side, about a position both still hold.
            pass
        elif not m["live_exit_day"] and m["sim_exit_day"]:
            # The replay is out and you are still in. Without this the row fell
            # through to "timing differs" and read "You sold ETH/USD on None ()"
            # — a sale that never happened, with no date, about a position still
            # open. Stamped with the REPLAY's exit, because that is the only
            # moment this row is about.
            event(m.get("sim_exit_at") or m["sim_exit_day"], m["sim_exit_day"], m["symbol"],
                  "sold", "replay sold, you held",
                  f"The replay sold {m['symbol']} on {m['sim_exit_day']} "
                  f"({m['sim_exit_reason']}). You are still holding it.")
        elif not m["exit_day_matches"]:
            event(m.get("live_exit_at") or m["live_exit_day"], m["live_exit_day"], m["symbol"],
                  "sold", "timing differs",
                  f"You sold {m['symbol']} on {m['live_exit_day']} ({m['live_exit_reason']}); "
                  f"the replay held until {m['sim_exit_day']} ({m['sim_exit_reason']}).")
        else:
            event(m.get("live_exit_at") or m["live_exit_day"], m["live_exit_day"], m["symbol"],
                  "sold", "reason differs",
                  f"Both sold {m['symbol']} the same day, but you on {m['live_exit_reason']} "
                  f"and the replay on {m['sim_exit_reason']}.")

    for r in live_only:
        # THREE states, not two. `None` means the replay never reported which
        # symbols it covered, so whether it was even looking at this one is
        # unknown — and saying "it was watching and passed" there asserts
        # something nobody checked. That wording sent a real investigation after
        # a signal difference that did not exist.
        covered = r.get("in_replayed_universe")
        if r.get("replay_error"):
            # NOT a miss at all: the stretch this trade falls in never replayed,
            # so there was nothing to miss it with. Said before the coverage
            # question because coverage does not arise — and the verdict says
            # "not compared" rather than "replay missed it", which is both the
            # truth and what the UI reads to stop colouring the row as a
            # disagreement.
            event(
                r.get("entry_at") or r["day"], r["day"], r["symbol"], "bought",
                "not compared",
                f"You bought {r['symbol']}. The replay of the stretch this falls in did not "
                f"run — {r['replay_error']} — so nothing was watching this symbol here. "
                "That is a gap in the comparison, not a disagreement with it.",
            )
            if r.get("exit_day"):
                event(r.get("exit_at") or r["exit_day"], r["exit_day"], r["symbol"], "sold",
                      "not compared",
                      f"You sold {r['symbol']} "
                      f"({r.get('exit_reason') or 'no reason recorded'}). The stretch it was "
                      "held in never replayed, so there is no exit to judge this against.")
            continue
        if r.get("replay_held_it"):
            # NOT a missed signal. The replay could not buy what it had never
            # sold — the entry was refused before any rule was consulted — so the
            # difference is in the EXIT, and pointing at the entry sends someone
            # after a bug in the wrong half of the strategy.
            event(
                r.get("entry_at") or r["day"], r["day"], r["symbol"], "bought",
                "replay still held it",
                f"You bought {r['symbol']} again. The replay was already holding it from an "
                "earlier entry and could not buy twice, so this is a difference in when the "
                "two SOLD, not in what they saw.",
            )
            if r.get("exit_day"):
                event(r.get("exit_at") or r["exit_day"], r["exit_day"], r["symbol"], "sold",
                      "nothing to compare",
                      f"You sold {r['symbol']} "
                      f"({r.get('exit_reason') or 'no reason recorded'}). The replay was still "
                      "holding its own earlier position, so there is no matching exit.")
            continue
        if covered and r.get("universe_seeded"):
            # A FOURTH state, and the most informative one. This name is in the
            # replay's universe only because your own trade put it there — the
            # cached-movers reconstruction did not produce it. So the coverage
            # question is settled by construction and what remains is the signal.
            why = (
                " It was added to the replay's universe because you traded it — the replay's"
                " own reconstruction of that day's movers did not include it. So it could see"
                " this symbol and still didn't buy: that is a signal difference, not a"
                " coverage gap."
            ) + _close_only_note(bar_seconds)
        elif covered is False:
            why = " It wasn't in the universe the replay covered, so it was never looking for it."
        elif covered is None:
            why = (
                " Whether the replay was even looking at this symbol is unknown — it didn't"
                " report its universe for this stretch, so this may be a coverage gap rather"
                " than a disagreement."
            )
        else:
            note = _close_only_note(bar_seconds)
            why = (
                " The replay was watching this symbol and passed — no bars for that day, or a"
                " different view of it."
            ) + (note or _UNQUALIFIED_MISS)
        event(r.get("entry_at") or r["day"], r["day"], r["symbol"], "bought",
              "replay missed it", f"You bought {r['symbol']}. The replay did not." + why)
        # The sale really happened, so it belongs in the log even though there is
        # nothing to compare it against — a timeline that quietly drops the exits
        # of trades the replay missed is not a record of what happened.
        if r.get("exit_day"):
            event(r.get("exit_at") or r["exit_day"], r["exit_day"], r["symbol"], "sold",
                  "nothing to compare",
                  f"You sold {r['symbol']} ({r.get('exit_reason') or 'no reason recorded'}). "
                  "The replay never held it, so there is no exit to judge this against.")

    for r in backtest_only:
        event(r.get("sim_entry_at") or r["day"], r["day"], r["symbol"], "bought",
              "replay invented it",
              f"The replay bought {r['symbol']}. You never did — it believes something was "
              "tradable that wasn't.")

    return sorted(rows, key=lambda r: (str(r["at"] or r["day"]), r["symbol"]))


def _decision_stats(matched: list[dict], live_only: list[dict], backtest_only: list[dict]) -> dict:
    """How well the replay reproduced the DECISIONS.

    `match_rate` is deliberately measured against every decision either side
    made, so a replay that finds the right trades but also invents ten others
    cannot score well by ignoring its own inventions."""
    total = len(matched) + len(live_only) + len(backtest_only)
    # Only exits a RULE produced. A force exit is a human decision the replay
    # could never have made, so scoring it would measure your button presses.
    comparable = [m for m in matched if m["exit_comparable"]]
    exits = [m for m in comparable if m["live_exit_reason"] and m["sim_exit_reason"]]
    same_rule = [m for m in exits if m["exit_reason_matches"]]
    # Only trades where SOMEBODY has sold. `exit_day_matches` demands two real
    # days, so a position both sides are still holding scored as a disagreement
    # about an exit neither of them made — and a report where nothing had been
    # sold yet read as 0% exit-day agreement, which is a verdict delivered
    # without a comparison. There is nothing to agree or disagree about until one
    # side leaves.
    ended = [m for m in comparable if m["live_exit_day"] or m["sim_exit_day"]]
    same_day = [m for m in ended if m["exit_day_matches"]]
    return {
        "live_trades": len(matched) + len(live_only),
        "backtest_trades": len(matched) + len(backtest_only),
        "matched": len(matched),
        "missed_by_backtest": len(live_only),
        "invented_by_backtest": len(backtest_only),
        # Of the trades the replay didn't find, how many it could never have
        # found because the symbol wasn't in the universe it replayed. A high
        # number here means the comparison is mismatched, not the backtester.
        "missed_outside_universe": sum(
            1 for r in live_only if r.get("in_replayed_universe") is False
        ),
        # Of the trades the replay didn't find, how many it was HANDED the symbol
        # for — seeded from the journal precisely so the universe could not be the
        # excuse. These are the only misses that are unambiguously about the
        # signal, and therefore the only ones worth chasing as a replay bug.
        "missed_despite_seeding": sum(
            1
            for r in live_only
            if r.get("universe_seeded") and r.get("in_replayed_universe")
        ),
        # And how many were not missed by anything: the stretch they fall in
        # never replayed. They still count against the match rate — the
        # comparison genuinely failed to reproduce them, and quietly excusing
        # them would flatter a run that covered less than it claimed — but a
        # reader deciding whether to go hunting needs to know how much of the
        # gap is a failed replay rather than a disagreement.
        "missed_replay_failed": sum(1 for r in live_only if r.get("replay_error")),
        # Matched trades the two sides opened FURTHER APART than sampling can
        # explain. They are still matches — the same decision on the same day —
        # but a match rate is the number people read, and one built partly on
        # fills thirteen hours apart says more than it knows. Counted here so the
        # figure above it can be read with that in mind.
        "entries_timing_differs": sum(1 for m in matched if m.get("entry_timing_differs")),
        "match_rate_pct": round(len(matched) / total * 100, 1) if total else None,
        "same_exit_rule_pct": round(len(same_rule) / len(exits) * 100, 1) if exits else None,
        "same_exit_day_pct": round(len(same_day) / len(ended) * 100, 1) if ended else None,
        # How many exits that percentage was computed over. Without it a 100%
        # built on one exit is indistinguishable from one built on forty.
        "exits_compared": len(ended),
        # Matched trades NEITHER side has sold yet. Excluded from the exit-day
        # percentage above rather than counted as a disagreement, and named here
        # so their absence from the denominator is visible rather than silent.
        "both_still_open": sum(
            1
            for m in comparable
            if not m["live_exit_day"] and not m["sim_exit_day"]
        ),
        # Trades whose EXIT was a force-close, an account reset or a
        # reconciliation. Surfaced rather than silently dropped: if most of your
        # exits were by hand, the exit half of this report is describing very
        # little, and you should know that before trusting it.
        #
        # Counted apart from the boundary-spanning ones below, because they are
        # different claims: "you closed this yourself" and "no segment of a split
        # comparison could see this trade end" call for different responses.
        "manual_exits": sum(
            1 for m in matched
            if not m["exit_comparable"]
            and not m["exit_spans_boundary"]
            and not m.get("entry_timing_differs")
        ),
        # Matched trades whose exits were set aside because the two sides opened
        # the position too far apart to be the same trade. Counted apart from the
        # hand-closed ones deliberately: "you closed these yourself" and "the
        # replay was holding a different position" call for opposite responses,
        # and folding a timing difference into `manual_exits` would blame the
        # user for the replay's entry.
        "exits_entry_mismatched": sum(
            1 for m in matched if m.get("entry_timing_differs") and m["live_exit_day"]
        ),
        # Matched trades whose exit fell in a later segment than their entry, on a
        # segmented comparison. Their exits are excluded from every percentage
        # above and from the trading cost below — see `exit_spans_boundary`.
        "boundary_spanning_exits": sum(1 for m in matched if m["exit_spans_boundary"]),
        # Below this, differences are anecdotes. The same "count the coins"
        # discipline the optimizer applies to its own winners.
        "enough_to_judge": len(matched) >= MIN_MATCHES_TO_JUDGE,
    }


def _execution_stats(matched: list[dict], assumed_spread_pct: float, assumed_fee_pct: float) -> dict:
    """How far the replay's FILLS sat from the real ones, and what that implies
    for the cost settings every future backtest uses.

    Reported as medians, not means: one bad fill on an illiquid name would drag
    a mean somewhere unrepresentative, and the point of this number is to be
    used as a setting.
    """
    # ENTRY deltas come from every matched trade — the entry was a strategy
    # decision even when the exit was yours.
    entry_deltas = [m["entry_delta_pct"] for m in matched if m["entry_delta_pct"] is not None]
    # EXIT deltas only from rule-driven exits. Comparing a discretionary sell
    # against the rule-based one measures the gap between two different
    # decisions, not the cost of executing one — and this median is the number
    # the backtest's spread setting is meant to take.
    comparable = [m for m in matched if m["exit_comparable"]]
    exit_deltas = [m["exit_delta_pct"] for m in comparable if m["exit_delta_pct"] is not None]
    both = entry_deltas + exit_deltas
    # Same reasoning for the P&L gap: a hand-closed trade's profit was decided by
    # when you pressed the button, so it says nothing about the backtest's bias.
    pnl_pairs = [
        (m["live_pnl"], m["sim_pnl"])
        for m in comparable
        if m["live_pnl"] is not None and m["sim_pnl"] is not None
    ]
    # The headline: over the trades that DID match, how much better did the
    # backtest think it did? Anything positive is the optimism baked into every
    # result the app has ever shown you.
    pnl_gap = (
        round(sum(sim for _, sim in pnl_pairs) - sum(live for live, _ in pnl_pairs), 2)
        if pnl_pairs
        else None
    )
    median_abs = round(median(abs(d) for d in both), 4) if both else None
    enough = len(both) >= MIN_FILLS_TO_JUDGE
    return {
        "fills_compared": len(both),
        "median_entry_delta_pct": round(median(entry_deltas), 4) if entry_deltas else None,
        "median_exit_delta_pct": round(median(exit_deltas), 4) if exit_deltas else None,
        # One-sided cost, which is the shape the backtest's own input takes.
        # This is the MEASUREMENT and is always reported, however few fills went
        # into it — `fills_compared` sits beside it and says how few.
        "measured_cost_per_side_pct": median_abs,
        "assumed_spread_pct": assumed_spread_pct,
        "assumed_fee_pct": assumed_fee_pct,
        # What to actually put in the backtest form — an ACTIONABLE claim, and
        # therefore held to a higher bar than the measurement above it.
        #
        # None on no data, and None on too little. Those used to be different:
        # the field went out populated whenever a single fill existed, so a
        # comparison reporting `enough_to_judge: false` on two fills still told
        # the reader to type 0.3345% into every future backtest. A median of two
        # is one unusual fill away from anything, and this number is copied into
        # a setting that then biases every backtest the app runs — the one place
        # in the report where a weak answer is more expensive than no answer.
        "suggested_spread_pct": median_abs if enough else None,
        # Why it was withheld, so the absence reads as a decision rather than as
        # the report having failed to compute something.
        "suggested_spread_withheld": (
            None
            if enough
            else (
                f"Measured over {len(both)} fill{'' if len(both) == 1 else 's'}, which is too "
                f"few to set a cost from — {MIN_FILLS_TO_JUDGE} is the bar. The measured figure "
                "is shown above as an observation; don't put it in the backtest form yet."
                if both
                else "No matched fills to measure."
            )
        ),
        "backtest_pnl_optimism_usd": pnl_gap,
        "enough_to_judge": enough,
    }


# Fields that change WHAT gets traded, and therefore make a replay of today's
# config a comparison against a different strategy. Presentation names, because
# this is read by someone deciding whether to trust a report.
_SHAPING_FIELDS: list[tuple[str, str]] = [
    ("universe", "Universe"),
    ("symbols", "Symbol list"),
    ("basket_id", "Basket"),
    ("rank_by", "Ranking"),
    ("top_n", "Top N"),
    ("rank_enabled", "Ranking on"),
    ("sizing_usd", "$ per trade"),
    ("sleeve_usd", "Sleeve budget"),
    ("max_positions", "Max positions"),
    ("swing_mode", "Swing mode"),
    ("ignore_regime", "Ignore regime filter"),
]


def _flat_params(snapshot: dict) -> dict:
    """entry/exit/atr/macd numbers flattened to "Entry min_day_gain_pct" style
    keys, so a changed stop shows up as one row rather than a nested blob."""
    out: dict[str, object] = {}
    params = snapshot.get("params") or {}
    for block in ("entry", "exit", "atr", "macd", "dca"):
        values = params.get(block) or {}
        if isinstance(values, dict):
            for key, value in values.items():
                out[f"{block.capitalize()} {key}"] = value
    return out


def config_drift(produced_by: dict | None, replayed: dict) -> list[dict]:
    """What differs between the config that PRODUCED the trades and the one being
    replayed against them.

    Every trade records the config version that produced it, precisely so a later
    question like this one can be answered. Edit a strategy's universe — or its
    sleeve, or how many positions it may hold — and a replay of today's settings
    is answering a different question than "did the backtester reproduce what
    happened", while looking identical on screen.

    Returns the changed fields only. An empty list is the good case and means the
    comparison is apples to apples."""
    if not produced_by:
        return []
    changes: list[dict] = []
    for key, label in _SHAPING_FIELDS:
        then, now = produced_by.get(key), replayed.get(key)
        if then != now:
            changes.append({"field": label, "then": then, "now": now})
    then_params, now_params = _flat_params(produced_by), _flat_params(replayed)
    for key in sorted(set(then_params) | set(now_params)):
        then, now = then_params.get(key), now_params.get(key)
        if then != now:
            changes.append({"field": key, "then": then, "now": now})
    return changes
