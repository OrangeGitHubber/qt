# Decision log

Why QT is the way it is. Newest first.

## 2026-07-18 — About page renders the living docs, served by the backend
The About page must never drift out of date, so it is sourced from the
maintained markdown files, not a hardcoded copy: `docs/CHANGELOG.md` (the
plain-English "what changed", already updated on every change) and the new
`docs/roadmap.md` (now the canonical in-repo roadmap — keep it current when
plans move). **These two files are the living sources for the About page and
must be kept up to date.**

Rendering approach chosen: **the backend serves the docs** (`/api/about/...`
reads the files at request time; the image `COPY`s `docs/` to `/app/docs`).
Preferred over baking the markdown into the frontend bundle because it's more
robust for a self-hoster — the page reflects the docs in the running image with
no frontend rebuild, and the same `/api/about` endpoint carries the build
identity. The build id (git short-SHA + build date) is threaded
Dockerfile → CI (`GIT_SHA`/`BUILD_DATE` build args on both image builds) →
backend env (`QT_GIT_SHA`/`QT_BUILD_DATE`) → `/api/about` → UI, with a
`git rev-parse` fallback for local/dev, so "what changed per build" sits next to
the exact commit the container was made from.

## 2026-07-18 — Dropped `VOLUME /data` from the Dockerfile
`VOLUME /data` auto-creates a Docker anonymous volume whenever no bind mount
is supplied. That silently masked an inverted unraid volume mapping: the app
ran fine on a throwaway volume until an image refresh recreated the container
and orphaned it, destroying config, keys and trade history. Removing the
`VOLUME` line means a missing/misdirected mount instead lands on the
container's ephemeral layer, where a startup detector (compares the device
behind `/data` vs `/`, and reads `/proc/self/mountinfo`) can catch it and warn
loudly. **Deploy implication:** a real bind mount (`-v host:/data`) was already
required and is now *doubly* required — without any `-v`, data is ephemeral by
design and the app will say so, rather than pretending to persist.
The detector is deliberately conservative (only warns when confident) so local
dev never false-alarms.

## 2026-07-16 — Symbol directory is local and per-instance
Autocomplete searches a local mirror of Alpaca's asset list rather than
calling the API per keystroke (rate limits, latency, and it must work when
Alpaca is down). The mirror is reference data — rebuildable, non-sensitive —
so it's safe to wipe, and each container keeps its own copy rather than
sharing one across instances: SQLite over unraid's `/mnt/user` FUSE layer is
a known corruption risk, sharing couples otherwise-isolated instances, and a
second instance may run on another machine entirely. The savings would have
been ~1 MB.

## 2026-07-16 — Backtests report deployment, and benchmark the traded symbol
A real backtest returned "+0.98%" for a strategy with a 2.04 profit factor —
because only 4% of the account was ever invested, and the benchmark shown was
SPY rather than the NVDA that was actually traded. Both were misleading by
omission, so the result now separates account return from return on capital
used, and charts buy-and-hold of the tested symbols alongside the market.
Rule of thumb this encodes: a strategy that can't beat holding the same
symbol is subtracting value.

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
