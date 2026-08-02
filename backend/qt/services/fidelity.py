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

from statistics import median


def _pct_delta(live: float | None, sim: float | None) -> float | None:
    """How far the simulated price sat from the live one, as a % of the live
    price. Positive = the backtest got a BETTER price than reality (it flatters
    itself); negative = it was more pessimistic than real life."""
    if live in (None, 0) or sim is None:
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
) -> dict:
    """Diff what really happened against what the replay says would have.

    `live_trades` are journal rows: symbol, entry_day, exit_day, entry_price,
    exit_price, pnl, status ("open"/"closed"/"rejected"), entry_reason,
    exit_reason. Rejected rows matter as much as filled ones — they are how a
    backtest-only trade is told apart from a rail doing its job.

    Every number is rounded where it is produced, so the API layer has nothing
    to decide and the UI cannot render 14 decimal places of false precision.
    """
    universe = (
        {s.upper() for s in replayed_symbols} if replayed_symbols else None
    )
    sim_trades = list(backtest_result.get("trade_list") or [])
    sim_open = list(backtest_result.get("open_positions") or [])

    # A position still open at the window's end is a real decision — the entry
    # happened. Excluding it would count every held winner as "the backtest
    # missed this trade", which is the opposite of the truth.
    sim_by_key: dict[tuple[str, str], dict] = {}
    for t in sim_trades + sim_open:
        sim_by_key.setdefault(_key(t.get("symbol", ""), t.get("entry_day")), t)

    live_filled = [t for t in live_trades if t.get("status") in ("open", "closed")]
    live_rejected = [t for t in live_trades if t.get("status") == "rejected"]
    live_by_key = {_key(t.get("symbol", ""), t.get("entry_day")): t for t in live_filled}
    # Which (symbol, day) pairs the engine WANTED but a rail refused. Without
    # this, every correctly-blocked trade would be filed as a backtest error.
    rejected_keys = {_key(t.get("symbol", ""), t.get("entry_day")) for t in live_rejected}

    matched: list[dict] = []
    live_only: list[dict] = []
    for key, live in live_by_key.items():
        sim = sim_by_key.get(key)
        if sim is None:
            live_only.append(
                {
                    "symbol": live.get("symbol"),
                    "day": key[1],
                    "entry_price": live.get("entry_price"),
                    "pnl": live.get("pnl"),
                    "entry_reason": live.get("entry_reason"),
                    # Whether the replay was even LOOKING at this symbol. A name
                    # outside the replayed universe was never going to be found,
                    # so calling it "the backtest missed a trade" blames the
                    # replay for being pointed somewhere else — usually because
                    # the strategy's universe was edited after these trades, or
                    # the comparison resolved a different one.
                    "in_replayed_universe": (
                        None if universe is None else live.get("symbol", "").upper() in universe
                    ),
                }
            )
            continue
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
                "live_exit_reason": live.get("exit_reason"),
                "sim_exit_reason": sim.get("exit_reason"),
                # False when a human or the broker ended this trade, not a rule.
                # The entry still counts; every exit-side comparison skips it.
                "exit_comparable": _is_strategy_exit(live.get("exit_reason")),
                # Reasons are prose with numbers in them, so compare the RULE
                # that fired (its first few words), not the whole sentence.
                "exit_reason_matches": _same_rule(
                    live.get("exit_reason"), sim.get("exit_reason")
                ),
            }
        )

    backtest_only: list[dict] = []
    rails_blocked: list[dict] = []
    for key, sim in sim_by_key.items():
        if key in live_by_key:
            continue
        row = {
            "symbol": sim.get("symbol"),
            "day": key[1],
            "sim_entry": sim.get("entry_price"),
            "sim_pnl": sim.get("pnl"),
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
    same_day = [m for m in comparable if m["exit_day_matches"]]
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
        "match_rate_pct": round(len(matched) / total * 100, 1) if total else None,
        "same_exit_rule_pct": round(len(same_rule) / len(exits) * 100, 1) if exits else None,
        "same_exit_day_pct": round(len(same_day) / len(comparable) * 100, 1) if comparable else None,
        # Trades whose EXIT was a force-close, an account reset or a
        # reconciliation. Surfaced rather than silently dropped: if most of your
        # exits were by hand, the exit half of this report is describing very
        # little, and you should know that before trusting it.
        "manual_exits": len(matched) - len(comparable),
        # Below this, differences are anecdotes. The same "count the coins"
        # discipline the optimizer applies to its own winners.
        "enough_to_judge": len(matched) >= 30,
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
    return {
        "fills_compared": len(both),
        "median_entry_delta_pct": round(median(entry_deltas), 4) if entry_deltas else None,
        "median_exit_delta_pct": round(median(exit_deltas), 4) if exit_deltas else None,
        # One-sided cost, which is the shape the backtest's own input takes.
        "measured_cost_per_side_pct": median_abs,
        "assumed_spread_pct": assumed_spread_pct,
        "assumed_fee_pct": assumed_fee_pct,
        # What to actually put in the backtest form. None when there is nothing
        # measured — a suggestion built on no data is worse than no suggestion.
        "suggested_spread_pct": median_abs,
        "backtest_pnl_optimism_usd": pnl_gap,
        "enough_to_judge": len(both) >= 30,
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
