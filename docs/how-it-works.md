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
  gain, exclusions. Note: free stock data comes from the
  [IEX exchange](https://en.wikipedia.org/wiki/IEX), which sees only a small
  slice of total US volume — the UI labels volumes accordingly.
- The **watchlist** is your manual list of symbols to always consider.
- Each **strategy** chooses its universe: scanner, watchlist, or both.

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
