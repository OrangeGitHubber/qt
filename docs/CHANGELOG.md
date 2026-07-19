# What we've done — plain-English changelog

Newest first. Each phase links to the technical details in
[how-it-works.md](how-it-works.md) and the reasoning in [decisions.md](decisions.md).

## App icon (2026-07-18)

QT now has an icon — a blue "QT" monogram badge — shown in the browser tab
(favicon), at the top of the README on GitHub, and as the container icon in
unraid / Docker (the unraid template already points at it). Source is
`frontend/public/favicon.svg`; a 256×256 `frontend/public/icon.png` is the
raster used by unraid and as the PNG/apple-touch fallback.

## Strategies: custom symbol universe + clearer asset-class scoping (2026-07-18)

- **"Specific symbols" universe.** A strategy can now target a **hand-picked list
  of symbols** instead of the scanner, watchlist, or a basket — pick exactly the
  tickers you want (e.g. just SPCX). The engine trades only those, your entry/exit
  rules still apply, and there's no need to create a whole basket for a one-off.
- **Asset class made explicit.** The editor now states plainly that a strategy's
  universe is scoped to its asset class — a **crypto strategy draws only from the
  crypto** scanner/watchlist/symbols and a **stock strategy only from stocks**,
  never the other. The symbol search in the custom universe is filtered to match.

## Scanner: crypto uses a rolling 24-hour window (2026-07-18)

Crypto "Today %" and "$ volume" are now measured over a **rolling 24 hours**
instead of the 00:00-UTC calendar day.

- **Why.** Crypto trades 24/7 with no real "close," so the old UTC-day bar meant
  the scanner effectively went blind to crypto for the first hours of each UTC
  day — the fresh bar hadn't accumulated enough volume to clear the floors yet,
  and the % move was measured from a near-flat open. A rolling 24h has **no
  timezone boundary at all** and matches the "24h change" every crypto exchange
  and price site quotes.
- **What you'll notice.** Crypto results are stable through the day instead of
  vanishing after midnight UTC, and the numbers line up with what you'd see on
  Coinbase/CoinGecko (still a feed *slice*, so smaller than the true market).
- Stocks are unchanged — they keep using the real trading session in Eastern
  time.

## Scanner: "+ Watch" is now a toggle (2026-07-18)

The Scanner's per-row **+ Watch** button now reflects — and changes — whether a
symbol is already on your watchlist.

- **Two states.** If a symbol isn't watched, the button reads **+ Watch**
  (filled blue) and clicking adds it. If it's already watched, the button reads
  **✓ Watched** (a calmer, muted blue) and clicking **removes** it — hovering
  hints it's removable ("Unwatch"). No more accidentally re-adding something you
  already pinned, and you can un-pin without leaving the Scanner.
- **Stays in sync.** The button state is driven by your real watchlist, so a
  symbol you pinned earlier already shows as **✓ Watched** when the Scanner
  loads. Stock and crypto tickers are tracked separately.

## Scanner: separate stock & crypto filters (2026-07-18)

Stocks and crypto now have **their own filter sets** instead of sharing one.

- **Why.** A single volume/price floor can't serve both: a $5M volume floor is
  right for stocks but starves crypto (whose volume resets at 00:00 UTC), and
  the $1 stock price floor wrongly excludes sub-$1 coins like DOGE. So the
  Scanner's Edit-filters panel now has a **Stocks** block and a **Crypto** block,
  each with its own min price, max price, min gain, and min $ volume. Rows-per-
  list and the "never trade" exclusions stay shared.
- **Sensible defaults per class.** Stocks: $1 price / $5M volume / 2% gain.
  Crypto: no price floor / $1M volume / 1% gain.
- **Nothing to redo.** Any existing saved filters are migrated automatically —
  your old single set is copied onto both classes, and you can differentiate
  them from there.

## Scanner: honest empty states + market-closed labeling (2026-07-18)

The scanner now explains itself instead of showing bare results or a blank
"nothing passes."

- **"Market closed" label.** Stock movers reflect the **last trading session**
  even on a weekend/holiday, so the Stocks panel now says so plainly — no more
  mistaking Friday's movers for live Saturday prices. (Crypto trades 24/7, so it
  has no such label.)
- **Why a panel is empty.** Instead of "Nothing passes the filters right now,"
  an empty panel reports **how many symbols were scanned and the strongest mover
  seen** — e.g. "Scanned 22 symbols — the strongest was ETH/USD at +0.42%, which
  didn't clear your filters." So you can tell the difference between *a quiet
  market* and *filters set too tight*, on your own instance, without guessing.

## Backtest & strategy UI polish (2026-07-18)

Readability and clarity fixes across the trading screens.

- **Backtest form, tidier.** The controls are grouped into *what* to test
  (strategy + a now-wider symbol search) and *how* to test it (history, bar
  size, cash, spread), so fields line up instead of scattering around the tall
  symbol picker.
- **Backtest "Trade log".** The results table is now a **time-ordered log of
  every buy and sell** — date on the left, one row per action. Each buy shows
  *why it bought* (the entry rule that fired, e.g. "up 5.2% today, above VWAP")
  and each sell shows its exit reason and the trade's P&L. Previously each
  round-trip was one row that only showed the exit reason.
- **Live sleeve-allocation readout.** Editing a strategy now shows the **sum of
  all strategy sleeves against your live Alpaca equity**. Over-allocating on
  purpose is fine and clearly explained: sleeves may overlap, whichever strategy
  trades first draws the shared cash, and the no-leverage rail still caps total
  spending at your real balance — nothing borrows.
- **Strategies grouped by state.** The Strategies list is now split into
  **Enabled** (on top) and **Disabled / drafts** sections, and an enabled
  strategy's badge **glows** with a green-edged card — so which strategies are
  armed to trade is obvious at a glance. (The engine still has to be on for them
  to act.)

## Themed baskets + top-N ranking universe (2026-07-18)

Build strategies by **theme/sector** instead of hand-picking tickers every time.

- **Baskets.** A new **Baskets** tab holds named symbol groups. QT ships a
  curated starter set — Defense, Banking, Gold & Mining, REITs/Property, Big
  Tech, Semiconductors, Energy, Healthcare, and a Sector-ETFs basket — of real,
  liquid, well-known tickers. Create your own, rename, delete, and add/remove
  symbols with the same ticker/company search used everywhere else.
- **Honest by design.** Baskets are **curated lists, not a sector database.**
  Alpaca has no sector/industry classification on this plan, so these lists are
  hand-picked and yours to edit; they drift as companies change. The UI says so.
- **Strategy universe "basket".** Point a strategy at a basket, choose how to
  rank its members — today's % move, 30-day return, or relative strength (vs the
  200-day average) — and how many to take (**top N**). The live engine ranks the
  basket each cycle and considers the top N (your entry rules still apply). This
  is how "top 10 from Defense" works.
- **Backtest from a basket.** One click loads a basket's symbols into the
  backtest (capped at 25, with a warning if trimmed) so you always see exactly
  what's tested. Stated plainly: a backtest tests the **whole basket** over
  history — it can't reconstruct the historical daily top-N, so **top-N ranking
  is a live feature only.** Dividend-yield ranking is out of scope for now.

## About page — build identity, changelog & roadmap (2026-07-18)

A new **About** tab answers "which build am I running, what changed, and where
is this going?"

- **Which build.** Shows the app version, license (GPLv3), a link to the
  GitHub repo, and — importantly — the **exact commit and build date** this
  container was made from, so a bug report can name the precise build. (Locally
  it falls back to your working commit, or "dev".)
- **What changed.** Renders this changelog itself, straight from the maintained
  `docs/CHANGELOG.md` — so it's always current, never a separate copy that can
  drift.
- **Roadmap.** A new plain-English [roadmap](roadmap.md) of every phase (0–6),
  what's shipped versus planned, sourced the same way from `docs/roadmap.md`.

## CI security scanning + release hygiene (2026-07-18)

- **Dependabot** now watches the Python, npm, and GitHub Actions dependencies
  and opens weekly update PRs.
- **Image vulnerability scanning.** Every published container image is scanned
  with Trivy in CI and the build fails on any HIGH/CRITICAL vulnerability, with
  a `.trivyignore` allowlist for accepted exceptions.
- **Don't auto-update the live bot.** The README now warns against tools like
  Watchtower auto-pulling `:latest` (a surprise restart mid-trade is dangerous)
  and recommends pinning a version tag and updating deliberately.

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

## Backtest trade detail moved below the chart (2026-07-18)

Follow-up to the readout work: the per-day trade description was still cramped
into the fixed strip above the chart, so a busy day's text ran off the right
edge (hidden behind an ellipsis) and the bottom row's descenders were clipped.
There's no fixed height that both fits variable, multi-trade text and keeps the
chart from moving — so the trade detail now lives **below** the chart, where it
wraps to as many lines as the day needs and is read in full. Its growth pushes
the legend down, never the chart. The strip above stays put with just the date
and each line's value (always two rows, so it never clips or shifts). Verified:
readout doesn't clip, trade text isn't truncated, chart top moves 0px between a
busy day and a quiet one.

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
