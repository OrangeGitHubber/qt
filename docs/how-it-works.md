# How QT works

A guided tour of the product and the machinery. Every market term links to a
reliable explainer — QT is built for someone learning trading, not someone who
already knows it.

## The big picture

QT is a self-hosted trading assistant that:

1. **Scans** the market for stocks and crypto that are rising today,
2. **Decides** whether each candidate passes your strategy's entry rules and
   every safety rail,
3. **Acts** according to its mode — journals the decision (shadow), places a
   simulated order ([paper trading](https://www.investopedia.com/terms/p/papertrade.asp)),
   or (much later, behind approvals) places a real one,
4. **Watches** open positions and exits when your configured downturn rules
   trigger,
5. **Explains** every single decision in a journal you can audit.

It runs as one Docker container on your unraid server. Everything is
configured in the web UI; the only exception is one deliberately
server-level safety lock (see *Leverage* below).

## The autonomy ladder

The bot earns trust in rungs. Each rung is a deliberate switch you flip:

| Rung | What it does | Risk |
|---|---|---|
| **Shadow** | Full decision loop, journals would-be trades, places no orders | Zero |
| **Paper** | Trades a simulated account with real market data | Zero (fake money) |
| **Live + approval** | Proposes real trades; you approve each one | Real, human-gated |
| **Live auto** | Trades real money within the risk rails | Real |

## Where trades come from

- The **scanner** ranks today's biggest gainers (stocks via Alpaca's movers
  screener, crypto computed from 24h snapshots) and filters them by your
  rules: price range, minimum
  [dollar volume](https://www.investopedia.com/terms/v/volume.asp), minimum
  gain, exclusions. Crypto is measured over a **rolling 24 hours** (no midnight
  boundary — the same "24h change" every crypto site quotes), while stocks use
  today's trading session. **The "$ volume" you see is feed volume, not the
  whole market.** Free stock data comes from the
  [IEX exchange](https://en.wikipedia.org/wiki/IEX) (~2–3% of US volume), and
  crypto volume is a rolling-24h total from Alpaca's aggregated feed — so BTC,
  which really trades billions a day, can show only a few thousand dollars here.
  Treat the number as a *relative* liquidity signal for comparing symbols on the
  same feed, and set the min-volume floors to the magnitudes you actually see in
  the table, not real-world market figures.
- The **watchlist** is your manual list of symbols to always consider. It also
  shows each symbol's 30-day change, its
  [ATR](https://www.investopedia.com/terms/a/atr.asp) ("typical daily move"),
  and how far it sits from its 200-day average — and clicking a ticker opens
  its full price history. **Use ATR to sanity-check your stops**: a stop
  tighter than the symbol's ordinary daily move will trigger on noise alone,
  which is exactly how a strategy ends up with many small losses and no
  winners.
- A **basket** is a curated, named list of symbols grouped by theme (Defense,
  Banking, Big Tech, a Sector-ETFs basket, …). QT ships a modest starter set of
  real, liquid large-caps and you can create/rename/delete baskets and edit
  their members freely. **Baskets are curated lists, not an authoritative
  sector database** — Alpaca provides no sector or industry classification on
  this data plan, so the lists are hand-picked and drift over time as companies
  change. Used as a strategy universe, the engine snapshots the basket's
  members, ranks them by a metric you choose (today's % move, 30-day return, or
  relative strength = how far price sits above/below its 200-day average) and
  takes the **top N** as candidates — this is how "top 10 from Defense" works.
  Top-N ranking is a **live entry-selection feature only**: a backtest tests the
  basket's whole symbol set over history, because the historical daily ranking
  can't be reconstructed (the same limitation the scanner has). Dividend-yield
  ranking is deliberately out of scope for now.
- Each **strategy** chooses its universe: scanner, watchlist, both, or a basket.
- Anywhere you choose symbols, search by **ticker or company name**. QT keeps
  a local, daily-refreshed copy of Alpaca's tradable asset list, so
  autocomplete is instant and costs no API calls. It's reference data —
  rebuildable at any time from Settings → Symbol directory → Sync now.

## Strategies

A strategy is a saved set of rules (never code):

- **Entry rules** — e.g. up at least X% today, trading above its
  [VWAP](https://www.investopedia.com/terms/v/vwap.asp), inside an allowed
  time window.
- **Exit rules** — a
  [trailing stop](https://www.investopedia.com/terms/t/trailingstop.asp)
  that follows the price up and sells on a configurable pullback, a hard
  [stop-loss](https://www.investopedia.com/terms/s/stop-lossorder.asp), a
  [take-profit](https://www.investopedia.com/terms/t/take-profitorder.asp)
  target, a maximum holding time, and optional
  flatten-before-close.
- **Mode** — stocks default to
  [swing trading](https://www.investopedia.com/terms/s/swingtrading.asp)
  (hold overnight, judged over days); crypto may trade intraday (24/7
  markets, cleaner data).
- **Sizing** — dollars per trade and a per-strategy budget ("sleeve"), so
  multiple strategies can't collide over the same cash.

Every edit creates a new **config version**, and every trade records which
version made it — so performance stats always know which rules produced them.

## The safety rails (always on)

- **Regime filter** — only open new long stock positions when the S&P 500
  (SPY) is above its
  [200-day moving average](https://www.investopedia.com/terms/m/movingaverage.asp),
  a widely used definition of a healthy market. Configurable, on by default.
- **No leverage** — the bot never borrows.
  [Margin](https://www.investopedia.com/terms/m/margin.asp) can amplify losses
  4x and is double-locked: the option is invisible unless a server-level
  environment variable is set, and enabling it still requires typed
  confirmation past an explicit risk warning.
- **Daily loss kill switch** — if today's losses reach your limit, the bot
  halts new entries and alerts you.
- **Exposure caps** — max open positions, max per-position size, max total
  invested, counted across *all* strategies.
- **Trade-rate limiter** — a self-imposed cap on trades per day (the
  regulatory [pattern day trader rule](https://www.investopedia.com/terms/p/patterndaytrader.asp)
  was retired in June 2026; this brake protects you from overtrading, which
  is still a real way to lose).
- **Wash-sale guard** — warns or blocks re-buying a stock within 31 days of
  selling it at a loss, which would disallow the tax deduction
  ([wash sale rule](https://www.investopedia.com/terms/w/washsalerule.asp)).
  It can only see this app's trades — trades in other accounts (e.g. a
  personal Robinhood) count too in the IRS's eyes.
- **Audit log** — every decision, config change, and mode switch is recorded.

## Reading a backtest honestly

Two numbers are deliberately kept apart, because conflating them is the
easiest way to fool yourself:

- **Account return** — what your whole balance did. If the bot only ever
  invests $200 of $5,000, this is dominated by the 96% sitting in cash.
- **Return on money used** — what the trades themselves achieved. This judges
  the *strategy*; the account return judges your *sizing*.

The chart also plots **buy-and-hold of the symbols you tested**, not just the
broad market. If a trading strategy can't beat simply holding the same stock,
the trading is subtracting value — that's the bar to clear, and it's a high
one for anything in a strong uptrend.

## The scoreboard

From the first day of paper trading the dashboard compares the bot's equity
curve against simply having bought and held the S&P 500 and Bitcoin on day
one. If the bot can't beat "do nothing", you'll see it — that comparison is
the project's honesty meter and the gate for ever trading real money.

## Security

- The entire UI sits behind **Google Sign-In**; only allowlisted Google
  accounts get in.
- Broker API keys are stored
  [encrypted at rest](https://en.wikipedia.org/wiki/Data_at_rest#Encryption)
  with a key generated on your server; nothing sensitive leaves your machine.
- The `/data` volume holds everything (database, keys) — back it up.

## Data & storage

- Market data and trading: [Alpaca's official APIs](https://docs.alpaca.markets/).
- Storage: a single [SQLite](https://en.wikipedia.org/wiki/SQLite) database
  in `/data`, evolved safely between versions with
  [Alembic migrations](https://alembic.sqlalchemy.org/).
- Notifications: [Slack incoming webhooks](https://api.slack.com/messaging/webhooks)
  for trade alerts, errors, and daily summaries.
