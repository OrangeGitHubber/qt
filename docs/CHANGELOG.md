# What we've done — plain-English changelog

Newest first. Each phase links to the technical details in
[how-it-works.md](how-it-works.md) and the reasoning in [decisions.md](decisions.md).

## Market-calendar correctness + nightly DB backups (2026-07-18)

- **Half-days and holidays respected.** The daily summary used to fire on a
  fixed 4:10pm-ish schedule and would post a meaningless "0 trades" on market
  holidays. It now checks Alpaca's trading calendar and stays quiet on days the
  market didn't open. (Flatten-before-close was already correct — it reads the
  real closing time from Alpaca, so it handles early-close days on its own.)
- **Automatic database backups.** QT snapshots its database (config, encrypted
  keys, trade journal) nightly and shortly after each start, keeping the last 7
  in `/data/backups/`. It uses SQLite's online backup, which is safe to run
  while the app is live. The disposable bar cache is not backed up. Restore is a
  simple file swap — steps are in the
  [data-persistence guide](data-persistence.md).

## Graceful shutdown + engine heartbeat/watchdog (2026-07-18)

- **Won't die mid-order.** When the container is asked to stop, QT sets a
  shutdown flag (no new positions open from that moment) and waits — up to 20
  seconds — for any in-flight engine tick to finish, so an order that's already
  been submitted is never abandoned between "placed" and "confirmed".
- **Heartbeat.** Every healthy engine cycle stamps a "last tick" time, shown on
  the dashboard (green when fresh, amber when stale) and in the status API.
- **Watchdog.** If the market is open and the engine hasn't ticked in over 5
  minutes, QT sends a single Slack alert (no spam) so a silently-stalled engine
  doesn't go unnoticed. It alerts again only after recovering and stalling anew.

## Crash recovery: reconcile with Alpaca on startup (2026-07-18)

If QT is stopped at the wrong moment — power cut, container restart, a crash
between placing an order and hearing back — the journal and the broker can drift
apart. QT now reconciles them on boot and every 15 minutes:

- **Exit we missed?** If the journal thinks a position is open but Alpaca no
  longer holds it, the exit filled while QT was down. QT closes it in the
  journal (marked "reconciled") at the last price it knew, so stats stay honest.
- **A position QT doesn't recognise?** It alerts (log + Slack) and leaves it
  alone — it never silently adopts a position, since it can't know which
  strategy it belonged to.
- **An entry it never confirmed?** It checks the order: filled → finalise it;
  still working → wait; dead → mark it rejected.

This only runs in paper mode (shadow places no real orders).

## Data-loss guard: warns when `/data` isn't persistent (2026-07-18)

QT can now tell when its data folder isn't a real, persistent location — the
exact silent failure that once wiped a container's config, API keys and trade
history after an update.

- **Startup detector.** On boot QT checks whether `/data` is a genuine mounted
  volume or a throwaway spot inside the container. If it's throwaway, it logs a
  loud error, sends a Slack alert (if configured), and shows a **red banner** in
  the UI: your data will be lost on the next update, with a link to the fix.
- **No more masking.** The container image no longer auto-creates a hidden
  "anonymous" volume that made a wrong volume mapping look like it was working.
- **"Keys can't be decrypted" is now explained,** not a crash: if the database
  has saved API keys but the encryption key file is missing, QT says so plainly
  and tells you how to recover.
- **Clearer setup docs.** The README, the unraid template, and a new
  [data-persistence guide](data-persistence.md) spell out that the volume is
  `your-server-folder : /data` — and warn against auto-updating the live
  container (e.g. Watchtower) mid-trade.
- The detector is careful: it only warns when it's sure, so it never nags on a
  normal developer machine.

## Steadier chart hover readout (2026-07-18)

The strip above the charts that shows the date and each line's value used to
churn as you moved the cursor: text reflowed and numbers jumped sideways, so a
figure you were trying to read kept sliding out from under your eye. Sometimes
a scrollbar appeared on the right — but it was unreachable, because the readout
blanked the instant the mouse left the chart to go grab it.

- **Every value now has its own fixed slot.** Date, each series (with its
  colour swatch) and its value all live in a grid that never reflows. Numbers
  are right-aligned with fixed-width digits, so only the digits change as you
  sweep — the layout stays put and a specific number holds its position.
- **No more scrollbar.** The readout always fits its content; nothing scrolls.
- **The long trade description got its own reserved line** below the numbers
  (▲ bought / ▼ sold, size, price, P&L, exit reason). It's the item that used
  to shove everything around; now it's on a single line that truncates with
  "…" if unusually long, with the full text on hover. The numbers above it no
  longer move when a trade happens.
- **The readout is now "sticky."** After you move off the chart it keeps
  showing the last day you hovered instead of going blank, so your eye can rest
  on a value. It updates again the moment you move back over the chart.
- Same treatment on the watchlist price chart (price / date / change).

## Readable backtest charts (2026-07-17)

- **Fixed: the same asset was drawn twice.** For a crypto strategy the
  "broad market" benchmark was hardcoded to BTC/USD — so a BTC/USD backtest
  charted BTC/USD as both "the symbol you tested" and "the market", with two
  legend entries reading *Hold BTC/USD*, disagreeing slightly because they
  were sampled differently. The market line is now skipped when it's the same
  asset being traded (which also saves an API call). A basket like BTC+ETH
  still gets a BTC market line, because "hold the basket" and "hold BTC" are
  genuinely different facts.
- **Hover the chart** for the date and every line's value at that point,
  colour-matched to the legend — no more decoding lines by eye.
- **Trade markers**: ▲ where the strategy bought, ▼ where it sold, drawn on
  its equity line. Hovering a marker shows the size, price, P&L and the exit
  reason, so you can see *where* in the window the trades happened.
- Clearer labels: "This strategy" / "Buy & hold X" / "Broad market (X)".

## Watchlist stats & price history (2026-07-17)

The watchlist now answers "is this symbol worth trading, and can my settings
even survive it?" at a glance:

- **30 day** — medium-term momentum, closer to a swing strategy's horizon
  than today's noise.
- **Daily move ([ATR](https://www.investopedia.com/terms/a/atr.asp))** — how
  much this symbol typically moves in a day, gaps included. The most
  decision-relevant number on the page: a trailing stop tighter than ATR will
  shake you out of good trades for no reason.
- **vs 200-day average** — the same trend test the regime filter applies to
  the S&P 500, per symbol.

Columns are toggleable, each explained by a tooltip. They're computed from
daily bars fetched **once per day** and cached, and if that history fetch
fails the prices still show — only the extra columns go quiet.

**Click any ticker** for its full price history (as far back as the data plan
allows — roughly 2016 for stocks) with 1M/6M/1Y/5Y/Max ranges. **Hover the
line** and the price, date, and change-from-start track your cursor.

## Symbol search, honest backtest metrics (2026-07-16)

**Type a company name, not a ticker.** Every place you used to type raw
symbols — watchlist, backtest, the scanner's exclude list — now autocompletes
on **ticker or company name** ("nvidia" finds NVDA). It's backed by a local
copy of Alpaca's ~11,000 tradable symbols, refreshed daily, so search is
instant, costs no API calls, and works even if Alpaca is unreachable. Adding
a known symbol no longer needs a live quote check either. Sync status and a
manual "Sync now" button live in Settings.

**The backtest stops flattering itself.** Two additions after a real result
was easy to misread:

- **Buy-and-hold benchmark of the symbols you actually tested**, not just
  SPY. If you backtest NVDA, the honest question is "would I have done better
  just holding NVDA?" — now the chart answers it, with the broad market shown
  as a secondary line.
- **Capital deployment**: how much of your account was ever really invested,
  how long it held anything, and the return on the money actually used. A
  strategy risking $200 of a $5,000 account can post a great trade record and
  a ~1% account return — those are different facts, and the UI now says so
  instead of letting them blur.

## Phase 2.5 — Minimal backtester (2026-07-13)

A new **Backtest** tab replays any saved strategy over up to two years of
historical prices — using the *same* decision code the live engine runs, so
the test can't lie about what the bot would do. You get net P&L after
[spread](https://www.investopedia.com/terms/s/spread.asp) costs, win rate,
[profit factor](https://www.investopedia.com/terms/p/profit_factor.asp),
[max drawdown](https://www.investopedia.com/terms/m/maximum-drawdown-mdd.asp),
an equity curve charted against buy-and-hold SPY/BTC, and every simulated
trade with its reason. Honest limits are stated in the UI: it replays a fixed
symbol list (not the scanner's historical daily picks), and past performance
predicts nothing — a backtest exists to kill bad ideas cheaply.

## Phase 2 (in progress — July 2026)

The trading engine. Google Sign-In in front of everything, database
migrations, strategies you configure from presets, a
[regime filter](https://www.investopedia.com/terms/m/movingaverage.asp) that
keeps the bot out of falling markets, a benchmark scoreboard that honestly
compares the bot against "just buy and hold", a zero-risk **shadow mode**
that journals every trade the bot *would* make without placing orders, and
finally simulated ([paper](https://www.investopedia.com/terms/p/papertrade.asp))
order execution with strict risk rails.

## Phase 1 — Market scanner & watchlist (2026-07-13)

The app can now *see* the market:

- **Scanner**: finds today's biggest risers among US stocks (via Alpaca's
  movers screener) and crypto (computed from snapshots of every tradable
  USD pair). You control the filters visually: minimum price, minimum
  [dollar volume](https://www.investopedia.com/terms/v/volume.asp) (so the
  bot avoids illiquid symbols that are hard to sell), minimum % gain, and
  an exclude list.
- **Watchlist**: pin symbols you always want considered, with live prices
  and mini trend charts.
- Results are cached briefly so the UI can never exceed Alpaca's
  [API rate limits](https://en.wikipedia.org/wiki/Rate_limiting).

## Phase 0 — Walking skeleton (2026-07-13)

The foundation:

- Web app (Python/[FastAPI](https://fastapi.tiangolo.com/) backend,
  [React](https://react.dev/) frontend) in a single Docker container for unraid.
- Setup wizard that verifies your [Alpaca](https://alpaca.markets)
  [paper-trading](https://www.investopedia.com/terms/p/papertrade.asp) keys
  and stores them [encrypted at rest](https://en.wikipedia.org/wiki/Data_at_rest#Encryption).
- Status dashboard: account equity/cash, market open/closed, and a permanent
  "PAPER MODE" banner.
- GitHub Actions build the Docker image automatically on every push.
