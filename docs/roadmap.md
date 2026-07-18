# Roadmap — where QT is going

This is the plan, in plain English, phase by phase. It's the canonical in-repo
roadmap and the source for the **Roadmap** tab on the About page, so it's kept
current as we go. Each phase is marked **Shipped**, **In progress**, or
**Planned**.

QT is built deliberately and conservatively because it eventually touches real
money. Everything below is paper-trading first; live money is late and gated.

---

## Phase 0 — Foundation & walking skeleton — Shipped

The bones of the app:

- A web app (Python/FastAPI backend, React frontend) in a single Docker
  container, made for a home server (unraid).
- A first-run setup wizard that verifies your
  [Alpaca](https://alpaca.markets) **paper-trading** keys and stores them
  encrypted.
- A status dashboard: account equity/cash, market open/closed, and a permanent
  "PAPER MODE" banner.
- GitHub Actions build the container image automatically on every push.

## Phase 1 — Market data & scanner — Shipped

The app can *see* the market:

- **Scanner** — finds today's biggest movers among US stocks and crypto, with
  filters you set visually (minimum price, dollar volume, % gain, an exclude
  list).
- **Watchlist** — pin symbols you always want considered, with live prices and
  mini charts. Later gained per-symbol stats (30-day move, typical daily range,
  trend vs the 200-day average) and clickable full price history.

## Phase 2 — Paper-trading engine — Shipped (core), ongoing polish

The bot that actually decides and trades on paper:

- **Google Sign-In in front of everything**, with an allowlist so you can add
  another person (e.g. a family member running their own copy).
- **Strategies you configure from presets** — no config files, just forms and
  sliders. Every trade records the exact settings that produced it.
- **Regime filter** — keeps the bot out of falling markets (only open new long
  stock positions when the S&P 500 is above its long-term average).
- **Benchmark scoreboard** — honestly compares the bot against "just buy and
  hold", which is the real test of whether any of this is worth it.
- **Shadow mode** — the engine runs the full decision loop and journals every
  trade it *would* make, placing no orders. A zero-risk way to watch it think.
- **Paper order execution** — simulated trades with strict safety rails
  (no leverage ever, max positions/exposure, daily-loss kill switch, a
  self-imposed max-trades-per-day brake, per-strategy capital sleeves).

## Phase 2.5 — Minimal backtester — Shipped

A **Backtest** tab that replays any saved strategy over up to two years of
historical prices, using the *same* decision code the live engine runs, so the
test can't lie about what the bot would do. It reports net profit after trading
costs, win rate, max drawdown, an equity curve against buy-and-hold, and every
simulated trade with its reason. Honest limits are stated plainly in the UI.

**Also shipped alongside it:**

- **Symbol search by name or ticker** — a local mirror of Alpaca's ~11,000
  tradable symbols, so typing "nvidia" finds NVDA instantly with no API calls.
- **Honest backtest metrics** — a buy-and-hold benchmark of the symbols you
  actually tested (not just the broad market), plus how much of your account
  was really invested, so a great trade record on a tiny slice of capital can't
  masquerade as a great result.
- **Readable backtest charts** — hover for values, trade markers showing where
  the strategy bought and sold, clearer labels.

## Phase 3 — Reliability hardening — Shipped

Making the bot survive the real world without duplicate or orphaned orders:

- **Crash recovery** — on restart (and every 15 minutes) QT reconciles its
  journal against Alpaca: missed exits are closed, unrecognised positions are
  flagged (never silently adopted), unconfirmed entries are resolved.
- **Won't die mid-order** — a graceful shutdown waits for an in-flight order to
  finish before the container stops.
- **Heartbeat & watchdog** — a stalled engine during market hours triggers a
  single Slack alert instead of going unnoticed.
- **Market-calendar correctness** — half-days and holidays are respected.
- **Nightly database backups** — the precious database (config, keys, journal)
  is snapshotted nightly and after each start; the disposable bar cache is not.
- **Data-loss guard** — QT detects when its `/data` folder isn't a real
  persistent volume (the exact silent failure that once wiped a container) and
  shows a loud red banner plus a Slack alert.
- **Supply-chain security in CI** — Dependabot updates and a Trivy image scan
  that fails the build on fixable HIGH/CRITICAL vulnerabilities.

## Phase 3.5 — Themed baskets & top-N ranking — In progress

Pulled forward because it makes strategy-building and backtest review easier.

- **Baskets** — named groups of symbols (Defense, Banking, Big Tech,
  Semiconductors, sector ETFs, and so on). QT ships a modest curated starter
  set of real, liquid, well-known tickers; you can create, rename, edit and
  delete your own. Important honesty: baskets are **curated lists that drift
  over time, not an authoritative sector database** — Alpaca has no sector
  classification, so these are hand-picked.
- **Rank a basket, take the top N** — a strategy can point at a basket, rank it
  (by today's move, 30-day return, or relative strength) and trade the top few.
  This delivers "the top 10 from Defense".
- **Backtest from a basket** — one click loads a basket's symbols into the
  backtest. Honest limit: a backtest tests the basket's *symbol set* over
  history; it can't reconstruct the historical daily top-N ranking.

## Phase 4 — Full backtesting & evaluation — Planned

The serious research tools:

- **Local bar cache** — stop re-downloading historical prices on every
  backtest. Bars are cached in a separate, disposable `bars.db` so iterating on
  settings is fast and stays well under Alpaca's rate limits. (A hard
  prerequisite for the optimizer below.)
- **Full backtesting UI** — richer results (equity curve, win rate, max
  drawdown, per-trade list) and side-by-side comparison of strategy configs.
- **Portfolio (multi-strategy) backtest** — run several strategies over the
  same period sharing **one** account, enforcing the same global rails as live,
  so the test matches how the bot really runs. Idle cash is shown as the sizing
  choice it is, not a hidden flaw, with a per-strategy contribution breakdown.
- **Strategy optimizer** — a *parameter search* (deliberately **not** called
  "AI") that uses the backtester to measure what actually worked instead of
  guessing numbers. Built from the ground up to fight
  [overfitting](https://www.investopedia.com/terms/o/overfitting.asp): it always
  holds out unseen data and reports only that out-of-sample result, prefers
  broad stable regions over lucky peaks, always shows how many combinations it
  tried, and validates across a whole basket rather than one ticker. Its output
  is a draft strategy to review, never a shortcut to live trading.
- **New strategy types** — a simple always-on DCA/rebalance sleeve (dumb,
  steady ETF buys) and a sector-ETF relative-strength rotation strategy, each
  validated by backtest before it's allowed to paper-trade.

## Phase 5 — Graduated live trading — Planned

Real money, carefully:

- Separate live API keys (kept apart from paper, with extra confirmation).
- **Approval mode** — proposed trades are pushed to Slack with a link; you
  approve or reject in the UI, and unapproved proposals expire.
- Live dashboards for realized profit/loss, exposure vs limits, and a
  wash-sale report for taxes.
- A **full-auto toggle** that only unlocks after the bot has earned a minimum
  paper track record, shown to you at the switch.

## Phase 6 — Sharing & multi-user — Planned

- A friendly setup guide for a non-technical second user, versioned releases,
  and simple UI login for a shared LAN install.
- An optional broker-adapter interface so another broker could be added later
  without touching the trading engine.

---

## Things we've deliberately decided **not** to build

Options trading, shorting, margin/leverage, machine-learning "signal" models,
and news/social-media sentiment as trade entry signals. These were considered
and rejected; sentiment might return one day only as a *defensive* filter (avoid
trading around risky events), never to decide what to buy.
