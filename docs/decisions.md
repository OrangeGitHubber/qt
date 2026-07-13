# Decision log

Why QT is the way it is. Newest first.

## 2026-07-13 — Leverage is double-locked at the server level
Margin/leverage stays off by default. The UI option to enable it is invisible
unless the Docker container sets `QT_ALLOW_LEVERAGE=true`; even then, enabling
requires passing an explicit risk warning with typed confirmation. Rationale:
a compromised or fat-fingered web UI must never be able to switch on borrowed
money; changing an unraid container variable is a deliberate physical act by
the server owner. This is the single deliberate exception to the "everything
configurable in the UI" rule — by design.

## 2026-07-13 — PDT rule repealed; replaced our design accordingly
The [FINRA pattern day trader rule](https://www.finra.org/rules-guidance/notices/26-10)
($25k minimum, 3-day-trades-per-5-days) was eliminated effective 2026-06-04.
Alpaca replaced it with an intraday margin framework and removed PDT fields
from its API. Consequences for QT: the planned day-trade counter was dropped
before being built; a self-imposed trade-rate limiter (overtrading brake) and
a hard no-leverage rail (the new rules allow 4x intraday buying power from
$2k — dangerous for a novice) took its place.

## 2026-07-13 — Council review shaped the strategy roadmap
A five-perspective adversarial review of the strategy plan concluded:
- Build a **regime filter** and **benchmark scoreboard** before anything clever.
- Add a **shadow mode** rung to the autonomy ladder (zero-risk decision-loop testing).
- Strategy order: regime-gated momentum → DCA baseline → (after backtesting
  exists) sector-ETF relative-strength rotation → mean reversion presets.
- **Rejected**: pairs trading (complexity, shorting), RSS/news/social
  sentiment as entry signals (noisy, gameable, undebuggable for a novice).
  Sentiment may return someday as a *defensive* filter only.
- Numeric gate before live money: ≥30 paper trades, net positive after
  spread costs, <10% max drawdown, beat buy-and-hold.

## 2026-07-13 — Google Sign-In in front of everything
All UI and API sit behind Google OIDC login with an owner-managed email
allowlist. A LAN is not a trust boundary (IoT devices, guests); a trading
app with broker keys deserves real authentication before the engine exists.

## 2026-07-11 — Alpaca over Robinhood/Fidelity; custom engine over Freqtrade
- Robinhood has no official API for stocks (crypto only; unofficial
  libraries risk account termination). Fidelity has no public API.
  [Alpaca](https://alpaca.markets) has first-class APIs for stocks + crypto,
  commission-free stock trades, and a free unlimited paper sandbox.
- Freqtrade (mature open-source bot) can't trade stocks and can't use
  Alpaca; its strategies are Python files, conflicting with the visual-config
  requirement. We build our own engine and borrow its proven concepts
  (dry-run, protections, journal), under the same GPLv3 license.

## 2026-07-11 — Paper-first, graduated autonomy
The bot must prove itself in a simulator before touching money, then earn
each rung: shadow → paper → live-with-approval → live-auto, with kill
switches always active. Chosen because the operator is a trading novice and
reliability is a hard requirement.
