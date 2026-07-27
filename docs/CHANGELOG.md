# What we've done — plain-English changelog

Newest first. Each phase links to the technical details in
[how-it-works.md](how-it-works.md) and the reasoning in [decisions.md](decisions.md).

## App logs now actually appear in the container logs (2026-07-27)

The app never configured logging, so the container only showed uvicorn's access
lines (`GET/POST … 200`) — every one of QT's own log messages (sweep progress,
reconciliation, persistence warnings, and crucially *errors*) was silently
dropped. QT now sends its `qt.*` logs to stdout at INFO by default (override with
the `QT_LOG_LEVEL` env var), so you can actually see what it's doing and diagnose
issues from the logs. Also added a diagnostic line to the crypto daily sweep that
reports how many bars Alpaca returned and over what date range — to track down
why crypto history was only caching ~16 days.

## Persistence is now enforced: no trading on an ephemeral journal (2026-07-27)

The app already *detected* a non-persistent `/data` and warned loudly (red
banner + Slack + log), but it kept trading anyway — and that's dangerous. When
the trade journal doesn't survive a restart, the engine forgets what it holds,
the "don't buy the same symbol twice" rail goes blind (it only checks the local
journal), and the bot **re-buys positions the broker already holds** — duplicate
orders and untracked "orphan" positions. That is exactly how a pile of 2am
crypto buys appeared on one paper account.

Now, when the engine is *confident* `/data` is ephemeral, it **freezes new
entries** — it opens no new positions until the volume mapping is fixed. Exits
and broker reconciliation still run (closing a position is always safe), so
nothing gets stranded. If persistence can't be determined (e.g. local
development, no container), trading proceeds normally — the freeze only triggers
on a confident "this is throwaway storage" verdict, so there are no false
alarms. The fix on your side is still to correct the `/data` volume mapping
(host path → container `/data`, never inverted — see docs/data-persistence.md).

## Fixed the sideways-scrolling page (2026-07-27)

The whole app had a faint horizontal scrollbar: the page was a bit wider than
the window, so you could nudge it sideways. The cause was the row of page
buttons at the top (Dashboard, Scanner, … Settings) refusing to wrap — on a
narrower window they ran off the right edge and dragged the whole page with
them. They now wrap onto a second line instead. As a bonus, wide data tables
(scanner results, trade logs, etc.) now scroll inside their own box on small
screens rather than pushing the page sideways. Nothing looks different on a
normal-width window.

## Choose which Slack messages QT sends (2026-07-27)

Slack was all-or-nothing: set a webhook and you got everything. The Slack
settings card now has a **"What to send"** list where you opt each message type
in or out, saved instantly. The categories:

- **Trade confirmations** — every buy/sell as it happens (on by default)
- **Daily summary** — end-of-day trades + realized P&L (on)
- **Weekly summary** — a Sunday recap of the week's trades, P&L and win rate (off — new)
- **Per-strategy P&L breakdown** — adds a per-strategy split to the daily/weekly summaries (off — new)
- **Reconciliation alerts** — broker-sync mismatches, e.g. an untracked position (on)
- **Engine health warnings** — watchdog alert if the engine stalls (on)
- **Risk & leverage changes** — when leverage is toggled (on)
- **Critical system alerts** — data-persistence / secret-decryption problems (on)

Every existing message is now gated by its category, and two brand-new opt-in
reports were added: a **weekly summary** (Sunday 17:00 ET) and a **per-strategy
breakdown** that enriches the daily and weekly recaps so you can see which
strategy drove the result. The manual "Send test" button always sends,
regardless of these toggles.

## Slack trade alerts: ticker now unmistakable (2026-07-27)

A buy alert like "bought 27 DAMD @ $1.85 — up 17.19% today, above VWAP…" could
be misread — with nothing separating the symbol from the reason, one reader
thought the bot had "bought VWAP". The buy and sell messages now put a `×`
between the quantity and the symbol, bold the symbol itself, and fence the
sections apart with `·` separators and plain labels, e.g. "bought 27 × *DAMD*
@ $1.8500 · reason: up 17.19% today, above VWAP; all rails passed · strategy:
Small cap daily pumps". Same information, just impossible to confuse the ticker
for part of the reason.

## Settings: bar-cache panel reorganised as stocks | crypto columns (2026-07-27)

The historical-bar-cache controls had grown into a hard-to-scan pile — stock
and crypto stats interleaved, buttons and the info toggle scattered, and three
long explanatory paragraphs on top. Reworked into two side-by-side columns,
**Stocks** and **Crypto**, each showing the same four figures (symbols/pairs
cached, days of movers, intraday bars, data through) aligned row-for-row with
its own action buttons beneath. A live status pill (idle / sweeping) sits in the
header, an in-progress sweep shows one shared progress strip, and the step-by-
step explanation now lives in a collapsible "How the sweep works" section so it
stays available without dominating the panel. Same actions and behaviour — just
grouped so the two caches read at a glance.

## Fix: regime filter was silently blocking ALL stock entries (2026-07-27)

The regime filter (which only lets stock strategies open positions while SPY is
above its 200-day moving average) needs ~200 days of SPY daily bars to do its
sum. It was asking Alpaca for those bars without a start date — and Alpaca's
bars endpoint, given no start, returns only the current day's single bar. With
one bar it can't compute the average, so the rail failed closed and blocked
every stock strategy, showing "CAUTION — regime unknown" on the dashboard no
matter what SPY was actually doing. (Crypto strategies were unaffected — regime
gating is stocks-only.) The fetch now asks for a 400-day window, so the average
computes and stock trading is gated on the real bull/bear signal again. Exits
were never affected.

## Crypto cache upkeep now runs every calendar day (2026-07-27)

The nightly job that keeps the scanner-replay cache current used to run only on
US trading days (after the 16:00 ET close) and maintained both the stock and
crypto caches together. That's right for stocks, but crypto trades 24/7 — so on
weekends and US holidays the crypto cache's newest movers lagged by a few days.

Crypto now has its own upkeep job that runs **every calendar day** at 00:20 UTC,
independent of the US market calendar, so weekend and holiday movers stay fresh.
Stocks keep their trading-day-gated 18:00 ET job unchanged.

Along the way, a subtle correctness fix: because a crypto daily bar for "today"
is always still forming (crypto never closes) and the cache only ever *adds*
bars it hasn't seen, a daily run could have cached that partial near-the-open bar
and frozen it — quietly flattening the day's percent change. The daily sweep now
skips the in-progress UTC day and only caches completed days. Both the automatic
upkeep and the manual "Run crypto sweep" button benefit. Still a no-op unless
you've actually built a crypto cache.

## Dashboard: per-strategy contribution breakdown (2026-07-27)

The dashboard scoreboard shows ONE bot line, which can't tell you *which*
strategy made or lost the money. A new "Strategy contributions" card breaks that
single number apart.

It has two parts. First, a table: each strategy's realized (locked-in) profit or
loss, its trade count, win rate, and how many positions it still has open — and
the rows sum exactly to the account's realized total, so nothing is hidden or
double-counted. Each strategy gets a stable colour used everywhere on the card.

Second, a stacked-bar chart of the last 30 days that had trades: one bar per day,
each bar split by strategy, gains rising above the zero line and losses dropping
below. It answers "who contributed what, and when" at a glance — you can see a
single strategy quietly bleeding on days the total still looked fine.

Both are computed straight from the closed-trade journal (grouped by strategy,
bucketed by exit date), so there's no new data to store and the numbers always
match the journal. Only the current engine mode's trades count (paper trades
don't mix into a live breakdown).

The daily chart has a **7D / 30D / 90D / All** window selector above it (added
2026-07-27) so you can zoom the lookback instead of it being fixed at 30 days.
The totals table stays all-time regardless, so it won't always sum to the
windowed chart.

## Crypto scanner-replay backtesting (2026-07-27)

A crypto "today's risers" strategy can now be backtested with scanner replay,
just like stocks — previously the historical bar cache and scanner replay were
stocks-only, so a crypto risers strategy worked live but couldn't be replayed.

The crypto cache lives in its OWN separate tables next to the stock cache, so
adding it never touches (or risks re-downloading) the large stock cache. The
crypto universe is tiny — the ~20–40 tradable USD pairs — so Settings gets one
"Run crypto sweep" button that pulls every pair's daily bars AND ranks each
day's risers in a single step, plus "Sweep crypto intraday" for 15-minute bars.
The crypto cache stats show up on the same panel once you've swept something.

The important subtlety: crypto trades 24/7 and its bars are aligned to the UTC
calendar day, not the US market session. So the crypto replay buckets every day
— movers, eligibility, and the equity curve — by the UTC day, while stocks stay
on the ET session day exactly as before. The nightly upkeep job also keeps a
crypto cache current if you've built one (it never bootstraps one on its own,
the same rule as stocks).

On the Backtest page, a crypto scanner strategy now defaults to scanner replay
(no more "crypto can't replay yet, using your watchlist" fallback), and the
daily-vs-intraday warning and paper-trading caveats apply to crypto too.

## Backtest defaults starting cash to the strategy's sleeve (2026-07-27)

The backtest's "Starting cash" now defaults to the selected strategy's sleeve
(the most that one strategy is ever allowed to deploy) instead of a fixed
$5,000. A single-strategy backtest can never put more than its sleeve to work,
so a fixed $5,000 against a $1,000 sleeve left most of the account idle and made
the account-% return look worse than the strategy actually was. It's still fully
editable — just a smarter starting point.

## Hide the regime-filter toggle on crypto strategies (2026-07-27)

The "Ignore regime filter" checkbox only affects stock strategies — the regime
filter is a stocks-only gate (S&P 500 vs its 200-day average) and the engine
never applies it to crypto. It's now hidden on crypto strategies, where it did
nothing, the same way "Flatten before close" is stock-only.

## App-wide button hierarchy (2026-07-27)

Swept the whole app so button rows read by role instead of a wall of identical
blue. Each panel/form keeps ONE filled primary (its main Save/Create/Run); every
secondary or utility action — Cancel, Refresh, Edit filters, Rename, Send test,
Sync now, Close, "show extra columns" — is now a quiet neutral outline;
destructive actions (Delete/Remove) stay red; and the existing toggle families
(mode switch, segmented filters, sort headers, range picker) were already
consistent. Nothing changed behaviourally.

## Settings: distinct bar-cache action buttons (2026-07-27)

The Run sweep / Sweep intraday / Re-rank / ⓘ buttons on the Historical bar cache
panel were all identical blue. They now read by role, matching the strategy-card
restyle: Run sweep is the filled primary, Sweep intraday an accent outline (a
second download, below the primary), Re-rank a quiet neutral outline (the light
recompute), and ⓘ a muted info toggle pushed to the end — with a divider
separating the row from the status above. Small ⭳/↻ glyphs reinforce each.

## Strategy cards: distinct action buttons (2026-07-27)

The Pause / Enable / Edit / Delete buttons on each strategy card now look
distinct instead of a row of identical blue buttons: Enable is filled green
(the "live" colour, matching the ENABLED pill), Pause is amber, Edit is a quiet
neutral outline, and Delete is red — each with a small glyph. The action row is
also separated from the strategy details by a divider so the buttons aren't
cramped against the text.

## Advanced per-strategy order-fill settings (2026-07-27)

New "Advanced — order fills" section on the strategy editor exposes how
aggressively QT prices its marketable limit orders — previously fixed constants:

- **Entry slippage %** (default 0.5) — how far *through* the market the buy limit
  is priced.
- **Exit slippage %** (default 1.0) — how far *below* the market the sell limit
  is priced.
- **Max exit slippage %** (default 1.0 = off) — set above the base to enable an
  **escalating chase**: each time an exit misses the fill, the sell price widens
  one step further down (up to the max), so a fast drop still gets out. It stays
  a limit order — QT never sends a naked market order.

Defaults reproduce the previous behaviour exactly. These affect live/paper orders
only; the backtest uses its own spread-cost input and assumes fills. (The retry
interval itself is the global ~1-minute engine tick, not per-strategy.)

## Re-rank progress bar + a freshest-riser cache check (2026-07-27)

- **Re-rank now shows real progress**, not an opaque "re-ranking…". It reports two
  phases — loading the cached bars (streamed, which also eases memory) then
  ranking the days — with a live count and a progress bar. So a long re-rank on a
  big cache looks like it's working instead of frozen.
- **New ⓘ button on the bar-cache panel** shows the freshest reconstructed riser
  (the #1 mover on the most recent cached day) with a ✓/✗ for whether its
  15-minute bars are cached — a quick "is my intraday sweep caught up to the
  latest movers?" check.

## Fix: Re-rank (and the sweep's ranking step) no longer freeze the app (2026-07-26)

Re-rank ran the heavy reconstruct — loading every cached daily bar and ranking
each day in pure Python — inline in the async task, which **blocked the whole
event loop** until it finished. On a large (Postgres) cache that looked like a
freeze: no status update, and the sweep buttons stopped responding too (their
requests couldn't be served). The reconstruct now runs in a worker thread, so
the server stays responsive — status shows "re-ranking…" and updates normally,
and other actions still work. The daily sweep's final ranking step was offloaded
the same way.

## Trading style is one choice; backtest warns when replay falls back to daily (2026-07-26)

- **Swing vs Intraday is now a single "Trading style" choice**, not two independent
  checkboxes. They're opposites — Swing holds overnight, Intraday flattens before
  the close — and enabling both was contradictory (the engine even suppressed
  flatten under swing). Picking Intraday sets flatten-before-close for stocks
  (crypto has no close); picking Swing turns it off and holds overnight.
- **The backtest now warns, prominently, when a scanner replay runs on daily
  bars for an intraday strategy.** Previously the only hint was the word "daily
  bars" in the header, so it looked like flatten-before-close was broken when it
  simply couldn't be simulated. The banner points you to run an intraday sweep
  and re-run.

## Bar-cache panel shows persisted totals, not just this run's progress (2026-07-26)

After a container redeploy the Historical bar cache panel showed all zeros and
"Last run: never" — alarming, because it looked like the swept data was gone. It
wasn't: the counters were tracked in memory (they reset with the process), while
the actual cache lives in the (durable) database. The panel now reads the real
persisted totals from the cache — **symbols cached**, **days of movers**,
**intraday bars**, and **data through** (the latest cached day) — so a redeploy
reflects what's actually there. Live per-run progress still shows while a sweep
is running.

## Strategy editor warns about two silent config traps (2026-07-26)

Two misconfigurations that quietly wreck a backtest now surface a warning in the
editor (they warn, never block):

- **Sleeve ≈ $ per trade** → only one position can ever open (a second exceeds
  the sleeve, so "Max positions" can't take effect), and a backtest stops
  trading once a losing streak leaves less cash than one full trade. The editor
  now also shows the rough number of concurrent positions the sleeve allows.
- **A tight stop-loss (<3%) with swing mode on** → holding overnight but bailing
  on a sub-3% wiggle means normal daily noise stops you out almost immediately,
  usually at a loss. Suggests widening the stop (5–8%, above ATR) or switching to
  intraday (swing off + flatten-before-close).

## Sweeps are now resilient and resumable (2026-07-26)

A long intraday sweep could stop partway (e.g. at "day 183/249") and go idle: a
request timeout or dropped connection isn't an Alpaca API error, so it escaped
the per-day handler and aborted the whole run. (A plain rate-limit was already
caught and the day skipped — so a hard stop meant something else.) Now:

- **Retries with backoff** on any fetch failure (rate-limit, timeout, network),
  so a transient hiccup no longer loses a day — or the whole sweep.
- **A day that still fails after retries is skipped, not fatal** — the sweep
  finishes the remaining days.
- **Resumable:** a re-run skips days already cached and continues where it
  stopped, instead of re-downloading everything and re-hitting the wall.

## Clearer bar-cache button descriptions (2026-07-26)

The "Historical bar cache" panel on Settings now spells out its three-step
pipeline so a novice can tell the buttons apart. A tester thought **Run sweep**
already produced the top-N risers and couldn't see why **Re-rank** existed. The
copy now numbers the steps: Run sweep downloads one daily bar for the *entire*
tradable US-stock universe (a raw dump, no risers yet); Re-rank recomputes each
day's top risers from bars already cached (no download, seconds — only needed
after changing the ranking criteria); Sweep intraday pulls 15-minute bars for
those movers so an intraday strategy can be replayed for real. Copy only — no
behaviour changed.

## Fix: intraday sweep progress now counts live (2026-07-26)

The "Intraday bars" counter sat at 0 for the whole sweep and only jumped to the
final number at the end — the live progress callback never carried the running
bar count. It now updates as each day is fetched. The status panel is also
sweep-aware: an intraday sweep shows "intraday sweep… · day 111/249 · N
symbol-days" instead of reusing the daily sweep's "of 12,971 symbols" total.

## Fix: flatten-before-close strategy gave zero trades on daily bars (2026-07-26)

The new "don't open a position on the bar we'd flatten it" guard skipped any bar
that's the last of its day — but a *daily* bar is the only bar of its day, so on
the daily-bar replay path it skipped **every** bar, producing "0 bars evaluated,
0 trades" for any strategy with flatten-before-close on. The guard now only
applies to a genuine intraday last bar (a bar that isn't also the first of its
day), so daily replay evaluates and trades normally again.

## Fix: scanner replay on an existing cache (missing intraday table) (2026-07-26)

A cache built before the intraday feature (e.g. a durable Postgres cache with
movers and daily bars already in it) didn't have the new `intraday_bars` table,
so a scanner-replay backtest errored when it went to read intraday bars — the run
appeared to do nothing. Replay now ensures the cache schema exists first
(idempotently creating only what's missing), so an older cache is healed in place
and falls back to daily bars until you run an intraday sweep.

## Backtest defaults to the strategy's own universe (2026-07-26)

The Backtest screen used to ignore the universe you set on the strategy — you had
to re-declare it by hand (tick "Scanner replay" or pick symbols), and if you
forgot, a "today's risers" strategy was silently tested against your *watchlist*
instead. Now picking a strategy preselects the right universe automatically:

- **Scanner (today's risers)** → scanner replay, with the riser count seeded from
  the strategy's own top-N (crypto risers fall back to the watchlist, since
  replay is stocks-only for now).
- **Basket** → loads that basket's symbols. **Custom** → the strategy's own list.
  **Watchlist** → the watchlist.

A banner spells out what's being tested ("universe: today's risers"), and the
manual controls remain as an explicit override for "what-if" runs.

## Scanner replay, stage 2: intraday bars — actually test an intraday strategy (2026-07-26)

Daily-bar replay couldn't test an intraday strategy: with one price per day there's
no "before the close" to flatten at, so a scalper got simulated as a multi-day
holder (positions rode overnight, and "flatten before close" silently did nothing).
Two fixes land together:

- **Rank risers by the intraday *peak*, not the close.** A stock that spiked +40%
  at 10:30am and closed flat is exactly what an intraday scanner flags — the daily
  bar's *high* captures that, so reconstruction now ranks on it. Ranking on the
  close silently dropped the pump-and-fade names these strategies live on.
- **New intraday sweep** (Settings → *Sweep intraday*) pulls 15-minute bars for the
  reconstructed movers — only those names, only their mover-days (plus a prior
  session so the day-gain baseline is real). Scanner replay then runs on the
  15-minute bars automatically, so VWAP, the entry window, trailing stops, and
  **flatten-before-close** all behave for real. Without an intraday sweep, replay
  still falls back to daily bars (and now says which it used).
- **The backtester now simulates flatten-before-close** on the last bar of each
  day (previously live-only — it never fired in any backtest), and won't open a
  position on that final bar (a scalp with no time to work).
- After your first intraday sweep, the nightly upkeep job keeps it current too.

Note: this doesn't prove your scalper is good or bad — it means it can finally be
tested on its real, intraday behavior instead of a daily-bar stand-in.

## Scanner replay: pick the riser count instantly, and keep the cache current (2026-07-26)

Built on the "store wide, narrow at read" idea, so the expensive part (downloading
bars) is decoupled from the cheap knob (how many risers per day):

- **Riser count is now a backtest knob, not a sweep setting.** The cache stores a
  generous top-50 per day; the Backtest screen has a **Risers per day (top N)**
  field (1–50). Dial it from top-3 to top-20 and the backtest re-runs
  instantly — no re-sweep, no re-download. Fewer names = only the very strongest
  movers; more = closer to a broad scanner.
- **Widen the history any time.** Re-running the sweep with more days adds the
  older days via idempotent upserts — it never re-downloads what's already
  cached — then re-ranks across the whole window.
- **Re-rank button** (Settings → Historical bar cache): re-computes the risers
  from bars already cached, in seconds, with no download. Use it after changing
  the scanner's filters, or to widen an older cache to the new top-50 set.
- **Automatic daily upkeep.** After your first sweep, QT pulls the day's universe
  bars and re-ranks the recent days every trading evening (18:00 ET). It only
  *maintains* a cache you already built, so if you don't use scanner replay it
  costs nothing.

## Fix: daily risk counters now reset on the US trading day, not midnight UTC (2026-07-26)

The **trade-rate limiter** ("max trades per day") and the **daily-loss kill
switch** measured "today" from midnight **UTC** — which is 7-8pm ET the evening
before. For 24/7 crypto that meant the bot's trade budget and its loss headroom
quietly reset in the middle of the evening's trading, right when a bad run might
be underway. Both counters now reset at **00:00 US Eastern** — the same trading
-day boundary the rest of the engine already uses — so "today" means one real
market day. Harmless for stocks (the market is shut by then either way);
important for crypto. Covered by boundary tests across daylight-saving and the
exact ET-evening rollover where the UTC date has already ticked over.

## Backtest: "Scanner replay" mode — test against each day's real risers (2026-07-26)

The backtest can now replay against **the market's actual top-10 risers on each
past day** — the names the live "today's risers" scanner would have surfaced —
instead of a fixed symbol list you type in. Tick **Scanner replay** on the
Backtest screen (the symbol picker greys out; it's stocks-only for now), and each
day only that day's cached top-10 are eligible to enter — your strategy's own
entry rules then decide. It's the closest a backtest gets to what the live engine
really does.

- Runs **fully offline** on the cached daily bars, so **run a sweep first**
  (Settings → Historical bar cache). If the cache is empty it says so rather than
  silently returning nothing.
- The results header summarises the run ("scanner replay — N days, M unique
  movers") since there's no short symbol list to show; the broad-market **SPY**
  line is still drawn for comparison.
- Fixed a day-alignment bug found in testing: cached daily bars were timestamped
  at midnight UTC, which the engine reads as the *previous* trading day — that
  misalignment would have quietly let every symbol through the daily filter.
  Bars are now stamped inside the trading day so the "day's movers" gate is
  applied to the right day.

## Bar cache: "Run sweep" button in Settings (2026-07-26)

Settings now has a **Historical bar cache** panel: a **Run sweep** button that
kicks off the universe daily-bar sweep + movers reconstruction (no more browser
console), with live progress — symbols saved, batches, days reconstructed, last
run — and the cache backend in use (local SQLite, or your Postgres host). Bad
DB connections or Alpaca auth errors surface right there.

## Backtest groundwork: historical universe sweep + movers reconstruction (2026-07-26)

Laying the data foundation for the upcoming "scanner replay" backtest. Alpaca
has no historical *movers* endpoint, so to ask "what would the scanner have
surfaced last March?" QT now rebuilds that answer from raw price history.

- A new **sweep** downloads about a year of daily bars for the whole tradable
  US-stock universe (real exchanges only — OTC/pink-sheet junk excluded, same
  as the live scanner). It works in batches, saving as it goes and skipping any
  batch the broker rejects, so a hiccup never aborts the whole run.
- A **reconstruction** step then replays each past day and recomputes that day's
  **top risers** — the biggest % gainers that clear the scanner's usual price,
  change, and dollar-volume floors — and stores them.
- Two new endpoints drive it: **POST `/api/barcache/sweep`** starts the run in
  the background (only one at a time) and returns straight away, and **GET
  `/api/barcache/status`** reports progress plus which cache database is in use
  (SQLite or your Postgres — host only, never the password). Starting a sweep
  also creates the cache tables, so a successful call doubles as a check that
  your cache-DB connection works.

This is backend + data only — no UI yet, and the sweep itself must be run on
your own instance (against real Alpaca and your database).

## Strategies: per-strategy share-price band (2026-07-26)

New entry rules **Min share price** and **Max share price ($)**. A strategy will
only buy symbols whose price sits in that band — e.g. set Max to 10 to trade
only movers **under $10**, or a Min to avoid sub-$1 names. It narrows this
strategy's universe on top of the scanner's own price floor; 0 on either side
means no limit that way. (Applies to the live engine's entry decision.)

## Strategies: entry window is a proper time picker with an on/off toggle (2026-07-26)

The entry-window fields were free-text, so you could type an ambiguous value like
"0930" that wouldn't match the HH:MM format the engine expects. They're now native
**time pickers** (clock selection, always valid HH:MM), gated by a **"Limit entries
to a time window (ET)"** checkbox — untick it and the window is off (entries any
time the market is open). That's the easy way to turn it off for crypto, which
trades 24/7.

## Journal: separate buy and sell rows (2026-07-26)

The journal used to collapse a whole position onto one line (entry price + exit
price + P&L together). Now each position shows as **separate rows**: a **▲ Bought**
row and, once it exits, a **▼ Sold** row, sorted by time. Each row shows the
position's **status** (open / closed), and expanding a Sold row **links back to
the buy** it closes (quantity, entry price, entry time, realized P&L). Rejected
decisions stay a single **⊘ Rejected** row. (One buy per position today — QT
doesn't scale into a position, so a sell maps to exactly one buy.)

## Fix: crypto trades were wrongly auto-closed by reconciliation (2026-07-26)

A real bug: crypto positions were being closed within minutes of opening, with
"reconciled: position no longer held at broker" and $0 P&L. Cause — QT stores
crypto as `AVAX/USD` but Alpaca's positions endpoint returns it slash-less
(`AVAXUSD`), so the reconciler couldn't match them and assumed the position had
vanished. Symbol matching is now slash-insensitive, so a held crypto position
stays open. (Stocks were never affected.)

## Strategies: "max gain today" entry ceiling (2026-07-26)

A new optional entry rule: **Max gain today (%)**. Momentum buys strength, but a
stock already up 20%+ is often a blow-off top about to reverse — chasing it (as
the engine did with CONL at +22%) means buying near the peak. Set a ceiling
(e.g. 10) and the bot **skips anything already up more than that today**; 0
leaves it off. The "Momentum — stocks, swing (recommended)" preset now defaults
this to 10. Existing strategies keep their rules until you edit and save them.

## Strategies: default entry window starts at the market open (2026-07-26)

Stock strategy presets (and new custom strategies) now default the entry
window to **09:30–15:30 ET** instead of 10:00–15:30 — so the bot can enter
risers **from the start of the trading day** rather than sitting out the first
half hour and buying in late, after a move has already run. (Trade-off: the
first ~30 minutes are the most volatile with the widest spreads on the free
feed.) You can still set any window per strategy, and crypto stays 24/7.
Existing strategies keep their saved window until you edit and save them.

## Journal: timestamp column + filter out rejected noise (2026-07-26)

- **Time column, leftmost, in your local time.** Every journal entry now shows
  when it was logged — including rejected ones — as the first column, formatted
  in your system's timezone (not UTC).
- **Filter by outcome.** An All / Trades / Rejected toggle. "Trades" shows only
  the actual buys and sells (open + closed); "Rejected" shows only the blocked
  decisions. The filter runs server-side, so hiding the (often numerous)
  rejected rows can't crowd real trades out of the row limit.
- **Filter by asset class.** An All / Stocks / Crypto toggle too. It composes
  with the outcome and mode filters (e.g. crypto + trades only).

## Watchlist: sort by any column + filter by asset class (2026-07-26)

- **Click any column to sort** — Symbol, Type, Price, Today, 30-day, Daily move,
  or vs-200d-avg. Click again to flip the direction (an ▲/▼ shows which column
  and way). Numeric columns default to high→low; empty ("—") values always sink
  to the bottom.
- **Filter by asset class** — an All / Stocks / Crypto toggle to see just one
  market at a time. The sort you picked carries across the filter.

## Scanner: stock volume floor uses a full session, not a partial day (2026-07-18)

The stock "$ volume" (and the min-volume floor it feeds) now uses the **last
completed trading session** — a stable full day. Previously, while the market
was open it used *today's* bar, which is only partial and grows through the
session, so a stock could fail the floor at 10am and pass at 3pm purely from
accumulation. Now the floor is a consistent full-day liquidity gate (it falls
back to today's bar only if the prior session's volume is unavailable). This
mirrors crypto, which uses a rolling 24-hour total. Stocks stay in Eastern
time; crypto has no timezone boundary.

## Fix: crypto scanner was only reading a couple of coins (2026-07-18)

The rolling-24h crypto change had a bug: it fetched hourly bars with a
`limit`, but Alpaca caps that limit across **all** symbols combined, not per
symbol — so the first coin or two consumed the whole budget and every other
pair came back with no/partial data. That showed up as "scanned 2 symbols"
and volumes reading ~$0. Fixed by fetching bars over a **time window** (the
last ~25 hours) with pagination, so every pair gets its full 24h of data and
the `$ volume` numbers are real again.

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
