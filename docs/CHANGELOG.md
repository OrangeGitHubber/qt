# What we've done — plain-English changelog

Newest first. Each phase links to the technical details in
[how-it-works.md](how-it-works.md) and the reasoning in [decisions.md](decisions.md).

## Two strategies can hold the same symbol (2026-08-01)

Until now the whole account held at most one position per symbol: whichever
strategy bought first owned that name and every other strategy was locked out of
it. That's the safe default and it stays the default — but two strategies with
genuinely different reasons to own a stock is a real book, and the rail made it
impossible.

Each strategy now has an opt-in: **"Let this strategy hold a symbol another
strategy already holds."** Off everywhere until you turn it on. It still won't
let a strategy stack a second position on one it already holds — that's
scaling-in, a different feature — and when it does block you, the reason now says
which of the two rules stopped it.

**The wash-sale guard and the cooldown after a loss stay account-wide**, whatever
this is set to. Those protect the whole portfolio (the tax rules count the
account, not your strategies), so a per-strategy exemption would defeat the
point. Tests pin that specifically.

### The part that had to be fixed first

The broker reports **one net position per symbol**, so once two strategies can
hold the same name, matching a position to a trade by symbol alone stops working.
Two failures were waiting:

- An entry we recorded but never confirmed adopted the broker's **whole**
  position. If one strategy held 5 and another's entry was unconfirmed while the
  broker showed 8, that second trade would have claimed all 8 — and the journal
  would then total 13 against a real 8.
- "In sync" meant only "a position exists", so any drift between what QT thinks
  it holds and what the broker actually holds went unnoticed.

Reconciliation is now quantity-aware: it adopts the broker's figure only when a
single trade owns the symbol, and it checks that the open trades for a symbol
**add up** to the broker's position, reporting a mismatch to the audit log and
Slack. It never auto-corrects one — it can tell that the totals disagree, but not
which strategy is wrong, and guessing would either invent shares or write off
real ones.

## Force exit a position by hand (2026-08-01)

Every open position on the dashboard now has a **Force exit** button: sell it
right now, at market, ignoring whatever the strategy's exit rules would have
waited for.

It sells at **market**, deliberately. The engine's normal exit is an escalating
marketable limit, which protects the price but can sit unfilled — and a button
labelled "force exit" that might not fill would be lying to you.

It closes **one position**, not every holding of that symbol. If two strategies
hold the same name, the other one is untouched; it didn't ask to be out.

Because it's immediate and irreversible, it asks first — naming the symbol, the
size, which strategy holds it and where the P&L currently stands, since "are you
sure?" on its own is a question you can't actually answer. If the broker rejects
the order you're told, and the position stays open rather than being marked
closed on a sale that never happened.

## The Save button says what it does (2026-08-01)

It read **"Save (creates new config version)"** on every edit — including one
where you'd only typed a note, which creates no version at all. It was also the
reason the row jumped: when the label became "Saving…" the button shrank by a
third and every button beside it slid left, right at the moment you're watching
to see whether the click registered.

The button now says **Save** (or **Create strategy**), and the consequence moved
to a line above it that updates as you type:

- *Saving creates a new config version. Trades already made keep pointing at v7,
  so past results stay honest.*
- *Only your notes changed — no new config version.*
- *No changes yet.*

Which is more useful than the old label ever was: you can see whether you've
actually changed a setting before you commit to it. The button also holds its
width, so nothing moves while it saves.

## Notes on a strategy (2026-08-01)

Every strategy now has a freeform notes box: what you were testing, what a
backtest suggested, what to try next. Yours alone — the engine never reads it.
Saved notes appear on the strategy's expanded row, so you can read them without
opening the editor, and line breaks are kept as you typed them.

One thing worth knowing: **writing a note does not create a new config version.**
Config versions exist so every trade in the journal points at the exact settings
that produced it. A note changes no behaviour, so minting a version for one would
put a "v124" in your history for a change that altered nothing. Change a real
setting and a version is still created, exactly as before; change both at once
and you get one.

## A rate limit no longer kills a long run (2026-08-01)

A 500-day comparison backtest died partway through with *"Bar download failed
(429): too many requests"*. Alpaca's free data plan allows 200 requests a
minute, and a comparison — two strategies, hundreds of days, fetched in pages —
can brush against that. A 429 means "wait a moment", but there was no retry at
all, so a job that had already run for minutes threw the work away.

Reads now wait and try again: up to four attempts, honouring Alpaca's own
`Retry-After` when it sends one and otherwise backing off 1s → 3s → 8s. The same
applies to transient gateway errors. If it still can't get through, the message
now says what to do about it instead of just naming the limit — shorten the
period, use fewer symbols, or run the two strategies one at a time, noting that
bars already downloaded are cached so a re-run asks for far less.

**Orders are deliberately not retried.** Retrying a read costs a moment;
retrying an order submission after an ambiguous response is how one intent
becomes two positions. The retry lives only on the read path, and there's a test
that fails if anyone ever tidies the two together.

## The optimizer searches the strategy's own universe (2026-08-01)

The backtest was locked to a strategy's universe a while back — you test the
strategy, so you test what it trades. The optimizer had kept its own symbol
picker and a scanner-replay toggle, which is the same problem in a worse place:
tune a basket rotator against three hand-picked names and the settings are
fitted to those three, then handed to a strategy that trades a different pool.
That's the exact overfitting the out-of-sample split exists to catch, arriving
through the front door.

The picker and the toggle are gone. The optimizer now shows the strategy's
universe read-only, the same chips the backtest shows, and a scanner strategy is
searched against its real day-varying universe automatically because that *is*
its universe.

The server decides this, not the page — the endpoint is reachable directly, so a
posted symbol list is ignored rather than merely hidden. Tests cover both.

One thing this makes visible rather than hides: if a strategy trades a single
symbol, the optimizer now says so plainly, because a search fitted to one name's
history rarely survives contact with another.

## Crypto backtests now measure a day the same way everything else does (2026-08-01)

Reviewing whether the optimizer handles crypto properly turned up the opposite
of the expected answer: **the optimizer was right, and the plain backtest was
wrong.**

Crypto has no trading session. It runs 24/7, its daily bars are stamped
midnight UTC, and "up 3% today" means over the last 24 **hours** — not since
some calendar midnight. The live engine, the scanner, the optimizer and the
portfolio backtester all worked that way. The single-strategy backtest only did
when a run happened to be mixed-resolution; otherwise it fell back to the stock
convention.

That mattered more than it sounds. On the same crypto bars, the day-gain that
`Min gain today` filters against read **1.54%** under the old rule and **3.69%**
under the correct one — so the optimizer could tune a strategy against one
definition of a day, and the backtest would then grade it against another.
Two more consequences of the same flag: the intraday VWAP reset at midnight New
York instead of midnight UTC, and every crypto *daily* bar was filed a day early
(00:00 UTC is 8pm the previous evening in New York).

Crypto backtest results will move. They were being measured against the wrong
yardstick; they are now measured against the one live trading uses.

Nothing changes for stocks.

## One day boundary everywhere: midnight New York (2026-08-01)

QT had two different ideas of when a day starts. The trading engine's daily
counters — the trade-rate limit and the daily-loss switch — reset at **midnight
ET**. But everything that *reported* on the trading (the scoreboard's rows, the
trades listed on them, and the daily-contribution chart) bucketed by the **UTC**
date.

During US market hours the two agree, which is why it went unnoticed. Crypto is
where it bites: a position closed at 21:00 ET is already "tomorrow" in UTC, so it
appeared on the next day's bar while the engine's own counters still called it
today.

Everything QT reports about its own trading now rolls over at midnight New York,
using a proper timezone rather than a fixed offset, so it stays right across
daylight saving.

Deliberately left alone: **market data keeps the vendor's convention.** Alpaca
stamps crypto daily bars at 00:00 UTC and the movers cache is keyed to match —
re-bucketing those to ET would put our records a day out from the data they were
built from. Vendor stamps keep vendor time; our own bookkeeping is ET.

One visible consequence: an equity snapshot taken in the evening used to open
the next day's row hours early. It now lands on the day it belongs to.

## Trade tables: the same colour rule as the charts (2026-07-31)

The charts stopped using green and red for buy-versus-sell, but the trade tables
hadn't caught up — every **Bought** was green and every **Sold** was red, so an
exit that made $12.32 was printed in the same colour as one that lost $1.83, with
the correct figure sitting right beside it in the P&L column.

Now, in both the backtest trade log and the live journal:

- **Bought** is neutral. A buy has no result yet.
- **Sold** takes the colour of what it made — so the Action and the P&L always
  agree.
- **Rejected** keeps its warning colour. That one *is* a status, not an outcome.

## Server errors now identify themselves (2026-07-31)

"HTTP 500 — Internal Server Error" is what FastAPI says when something breaks
unexpectedly. It's true and completely useless: the actual error is in the
container log, mixed in with everything else, with nothing to search for.

Every unexpected 500 now carries a short reference:

> Something went wrong on the server (ref a3f9c2). The full error is in the
> container log — search it for 'a3f9c2'.

The same reference is written next to the full traceback in the log, so one
occurrence can be tracked down with a single search instead of guesswork. The
internal message itself is deliberately *not* shown in the browser — the
reference is for correlating, not for shipping stack traces to the UI.

One specific case is now separated out. SQLite allows only one writer at a time,
so a save that lands while the trading engine's minute tick or a sweep is writing
can be refused. That's **transient** — it works on retry — but it arrived as the
same anonymous 500 as a real bug. It now says so plainly, including that nothing
was saved, so you're not left wondering whether the edit half-applied.

## The basket sweep now reports progress like a backtest (2026-07-31)

The sweep drew a progress bar but sat behind a button that just said
"Sweeping…", while a backtest showed a spinner, the phase it was in and a
running clock. Three long jobs, three different answers to "is this still
working?".

All three now use the same status line — spinner, the phase the server reports,
a percentage where one is knowable, and elapsed time. The sweep says which
basket it's on out of how many; the parameter search says how many combinations
it has tried.

The clock reads from the **server's** start time rather than the browser's, so
reloading the page in the middle of a ten-minute sweep no longer restarts it at
zero.

## Chart markers stop pretending to be profit and loss (2026-07-31)

The buy and sell triangles on every chart were green and red — the same two
colours that mean *gain* and *loss* everywhere else in the app. A green triangle
looked like a good trade and a red one like a bad one, when all they ever meant
was "bought here" and "sold here". A buy has no result to report yet, and a day
holding several buys and sells has no single outcome to colour.

Buy versus sell is now carried by **shape and position**: a filled triangle
pointing up, above the line, is a buy; a hollow triangle pointing down, below the
line, is a sell. Colour now says **which line the trade belongs to** — which is
the thing you genuinely couldn't tell before when comparing two strategies on one
chart.

Applied to both the performance chart and the symbol price chart, so the two
don't teach different vocabularies.

## Every trade reason now shows what it was measured against (2026-07-31)

The exit reasons had drifted into different styles. Some told you everything —
*"ATR stop-loss: -5.42% ≤ -5.16% (1.5× ATR 3.44%)"* — and some told you half:
*"trailing stop: 8.54% off high 818.6700"* named the drop but not the setting it
breached, leaving you to remember your own configuration and do the arithmetic.

They now share one shape — **what happened, the bar it crossed, and where that
bar came from**:

| before | after |
| --- | --- |
| `trailing stop: 8.54% off high 818.6700` | `trailing stop: 8.54% ≥ 6% off high $818.67 (stop $769.55)` |
| `price 123.4567 fell below VWAP 124.0000` | `price $123.46 fell below VWAP $124.00` |
| `MACD turned bearish` | `MACD turned bearish — daily line crossed below its signal` |
| `flatten before market close` | `flatten before market close — no overnight exposure` |
| `up 5.95% today, MACD bullish` | `up 5.95% today (min 3%), MACD bullish` |

Prices are formatted as money throughout, with enough decimals to stay honest on
a sub-dollar mover — `$0.4382`, not `$0.44` and not `0.4382`. Thresholds print
consistently too, so you no longer see `-5.0%` in one reason and `6%` in the
next for the same kind of setting.

This lands everywhere at once: the live journal, Slack alerts, the backtest trade
log and the optimizer all read from the same two functions that decide the
trades, so there is no second copy to drift.

## The stop-loss box now tells you when it isn't the stop (2026-07-31)

If the **ATR stop** is switched on, it *replaces* the fixed stop-loss — the hard
stop becomes the multiplier times each symbol's own daily ATR, recalculated
every tick. That has always been how the engine works, but the editor showed a
stop-loss box with a number in it that wasn't the rule being enforced, and the
ATR knob that overrode it sits inside a collapsed **Advanced** section. So a
strategy could read "stop 2%" while actually stopping out at 6%.

With the ATR stop on, the stop-loss field is now greyed out and relabelled
*"replaced by the ATR stop"*, with a note under it spelling out the real level,
worked through with examples (at 1.5× ATR, a symbol moving 1% a day stops out
near 1.5%; one moving 4% a day, near 6%). It also says the two things people ask
next: the fixed percentage stays as the **fallback** for when ATR can't be
computed, so a position is never left without a stop; and the **trailing stop is
unaffected** — the two are independent and whichever fires first sells.

The strategy list had the same problem in miniature, summarising a strategy as
"trail 6% · stop 2%" when ATR was governing. It now reads "stop 1.5×ATR".

## The chart hover panel now says what each thing is (2026-07-31)

A busy day ran everything together into one unlabelled stream — twenty buys and
sells behind bare arrows, followed by "3 strategies:" and some percentages, with
nothing saying which was which or what the percentages measured.

It now reads as sections:

```
2026-07-30
▲ Bought 13   ACN @ $165.15   ADA/USD @ $0.16   C @ $130.06   …
▼ Sold 7      ACN @ $164.52 −$0.95   AON @ $362.03 −$18.70   …
─────────────────────────────────────────────
Day −0.28%    realized, by strategy:  Banking Sector −0.19%   …
```

Buys and sells get their own labelled row with a count. The day's move leads its
own line, separated by a rule, so trades and contributors can't be mistaken for
each other — and the label finally says what those numbers are: **realized**
P&L per strategy, which is why they don't always add up to the day's move (open
positions move the line too, and aren't realized until they're sold).

One thing that was quietly backwards: a sell was always printed red, because it
was a sell. A profitable exit therefore looked like a loss. Sells are now
coloured by what they made.

## Every buy now records where it ranked (2026-07-31)

A journal entry read *"up 2.17% today, MACD bullish; all rails passed"* whether
the bot had bought the strongest name in the basket or the twenty-fourth one —
the last thing standing after everything above it was already held or failed the
rules. Those are very different trades and they looked identical.

Buys, blocked entries and skipped candidates now say **where the symbol placed**:
*"up 2.17% today, MACD bullish, ranked #24 of 25 by momentum today; all rails
passed"*. The strategy's last-run table gains a **Rank** column showing the same
thing for every candidate it looked at.

This is worth reading when a strategy that allows only a few positions keeps
buying from the tail of its own list. The engine already takes candidates
strictly best-first, so a low rank isn't the bot ignoring the leaders — it means
every stronger name was already held or didn't pass the entry rules. Now you can
see which.

## Strategy contributions: unrealized P&L column (2026-07-31)

The table showed each strategy's **realized** profit and a bare count of open
positions. It now also shows **unrealized P&L** — what those open positions are
up or down right now, marked to live prices.

The two are shown side by side and never added together, because they aren't the
same kind of money: realized is locked in, unrealized moves with every tick and
isn't yours until you sell.

One distinction the column is careful about: **a missing price is not a
break-even price.** If a quote can't be fetched, that strategy shows "—" rather
than "$0.00" — the two look identical on screen but mean opposite things. A
strategy holding nothing genuinely shows $0.00. If only some positions could be
priced, the figure is marked with an asterisk and a note says the total is a
floor, not the whole picture.

## The scoreboard said "no trades this day" every single day (2026-07-31)

Hovering any day on the dashboard scoreboard reported **no trades that day** —
including days the bot plainly traded. It wasn't reading the trades and getting
them wrong. It had never been given any trades at all: the chart component
treats "no trades supplied" and "no trades happened" as the same thing, so a
caller that passed nothing got a confident denial on every date.

Two fixes, because either alone leaves a trap:

- The scoreboard now sends the day's actual **buys and sells**, so hovering
  shows what was bought or sold and for how much. They're matched to days on the
  server, using the same UTC day the equity points use — done in the browser, an
  evening trade would slide onto the neighbouring day and be blamed for the wrong
  move. They're scoped to the same broker account as the line, and shadow-mode
  trades are excluded, since those never touched this equity.
- The chart no longer claims anything about trades it wasn't given. "No trades
  this day" now means exactly that, and appears only when the day's trades were
  actually checked.

## Charts now name the date they're measured from (2026-07-31)

Every line on a backtest chart is rebased to zero on the **first day of the
window you tested**, so the same calendar date reads +0% in a 150-day run and
+22% in a 500-day one. Both are right; they have different starting lines. The
hover now says **"since <start date>"** next to the date, so two runs of
different lengths can't be silently read against each other.

Worth knowing when comparing runs: percentages measured from a common base don't
subtract. If SPY shows +22.36% on one date and +30.07% later, the return between
them is not 7.71 points — it's (1.3007 ÷ 1.2236 − 1) = **6.30%**.

## The last two bar downloads that skipped the cache (2026-07-30)

The local bar cache works the way it should — run a backtest once and the second
one reads history from disk, downloading only the days that have happened since.
Two paths were still going straight to Alpaca every time, though:

- **The portfolio backtest.** It fetches every symbol of every selected
  strategy — the heaviest download in the app — and re-downloaded all of it on
  every run.
- **The benchmark line.** A year of SPY, re-fetched for every single backtest,
  even though it's the same daily history the strategies already cache.

Both now read through the cache like everything else. The benchmark needed the
most care: cached daily bars are re-stamped when they're read back, so the risk
wasn't a slow run, it was the benchmark line landing a day off the equity curve.
There's now a test that the cached benchmark is identical to a freshly
downloaded one, and tests that a repeat backtest — single or portfolio —
downloads *nothing*.

Unchanged, deliberately: the live engine, the scanner and the watchlist always
fetch fresh. The cache never stores a bar whose period is still open, because a
half-finished price saved once would be served as fact forever.

## A running backtest now says what it's doing (2026-07-30)

Now that a long run happens in the background, the only sign of life was a
greyed-out button — and a frozen button looks exactly like a broken one. Beside
it you now get a spinner and the actual phase the server is in: **"Downloading
30 symbols of history…"**, then **"Replaying history… 47%"**, then **"Fetching
the SPY benchmark…"**, with the elapsed time alongside. The percentage is real,
counted off the bars actually replayed, and only appears for the replay — the
one phase whose length is knowable in advance. The clock is kept locally, so the
line keeps ticking between updates instead of freezing whenever one phase runs
long.

## Long backtests no longer die at 100 seconds (2026-07-30)

A 350-day backtest over 30 symbols ran for a few minutes and then failed with
**HTTP 524**. That code is Cloudflare's own: it waited 100 seconds for QT, gave
up, and closed the connection. The replay was fine — the *answer* had nowhere to
go. The 100-second limit is fixed and cannot be raised by any setting, so the
fix isn't a bigger timeout; it's not holding a connection open for minutes.

Backtests (single and portfolio) now run **in the background**: the browser
starts one, gets a ticket, and asks "done yet?" every second and a half. Every
request finishes in milliseconds, so there's nothing left for a proxy to time
out, however long the run takes. The Run button counts the elapsed time while it
waits — a slow run now visibly *is* slow rather than looking frozen.

One thing that had to change underneath: the replay is pure arithmetic over
every bar, and Python would otherwise do it *instead of* answering anything
else. Left as-is it would have frozen the trading engine's minute tick, every
other page, and the very "done yet?" checks — timing out on those instead. The
replay now runs alongside them.

Two smaller honesty fixes: a backtest cut short by a restart used to look like
one that finished with no result, and now says it was cancelled; and asking
about a run the server no longer remembers explains that it expired rather than
just saying "not found".

## Buttons that "did nothing" were failing with an invisible message (2026-07-30)

"Save & backtest" sometimes appeared to do nothing and then worked on the next
click; the **Run backtest** button sometimes did nothing at all. Neither was
random. Every page shows API errors with a check that treats an **empty message
as no error**, and the code that turned a failed request into an error could
produce exactly that — most reliably when the app is reached through a **reverse
proxy**, because HTTP/2 removed the text that follows a status code, leaving it
blank. A 500, a proxy's 502, or a gateway timeout on a long backtest all arrived
as an error with nothing to say, so the click looked ignored. Retrying "fixed"
it only because the next request happened to succeed.

Failed requests now always explain themselves: the app's own wording when it has
some, otherwise the status code — and specific advice for the two cases you're
most likely to hit. A **502/503/504** now says the server didn't answer in time
and that a long backtest or sweep can outlast a proxy's default 60-second
timeout. A **401** now says your sign-in expired and to reload. Form validation
errors, which used to stringify to `[object Object]`, now name the field.

Two related dead ends closed. If the strategy list failed to load, the failure
was swallowed *and* left the Run button permanently greyed out — indistinguishable
from a broken button; it now says what went wrong. And Run/Search no longer grey
themselves out when nothing is selected — they let the click through and tell you
to pick a strategy, because a disabled button explains nothing.

## Backtest trade log: one line per trade (2026-07-30)

The log's rows were folding onto two and three lines — `▲ Bought` broke after the
arrow, and `$558.7882 ×0.447397` broke between the price and the quantity — so 63
trades rendered about three screens taller than they needed to be. The data
columns no longer wrap and the reason column absorbs the leftover width. Prices
now adapt (`$558.79` for a normal stock, `$0.4382` for a sub-dollar mover) instead
of always showing four decimals, and fractional share counts lose the digits
nobody reads. Rows went from 89–107px to a flat 37px.

## Scoreboard: the −80% cliff was an account switch, not a loss (2026-07-30)

The dashboard's honesty meter showed the bot down **−80%** overnight. It never
lost that money: benchmark snapshots stored one equity row per day with **no
record of which broker account it came from**, and the chart measured every point
against the first row ever recorded. Swapping to a new paper account with a
smaller balance made the *step between two unrelated accounts* read as a
catastrophic trading loss (20k ÷ 100k − 1 = −80%). Because the row is keyed by
day, the new account also overwrote the old one's row for the switch day —
hence one sharp cliff instead of a gap.

Snapshots now record their account (trades already did), and the scoreboard
**scopes to the account you're actually trading** and measures from *its* first
day — bot and both benchmarks alike, so the chart reads "since this account
started". Old rows are kept as legacy history, and the cross-account view is
still available on request if you ever want to see it. Switching accounts from
here on starts a clean line instead of faking a crash.

Two more things while in there: the chart's last point used to be up to an hour
stale (the snapshot job runs hourly) — it now refreshes on load. And **hovering a
day tells you which strategies moved it**, in the same percentage points the
line is drawn in, like the backtest chart's per-symbol breakdown. That
attribution is *realized* P&L, so it won't always add up to the whole day's step
— open positions move the line too, and the card says so.

## Backtests and searches stop re-downloading the same history (2026-07-30)

Every backtest and every parameter search used to pull its bars from Alpaca from
scratch — the same year of the same symbols, again and again. On a mixed-
resolution search that's a lot of 15-minute bars for data that hasn't changed
since the last time you asked.

The bar cache you already have (the one the scanner sweep fills — SQLite by
default, Postgres if you've pointed `QT_BAR_CACHE_URL` at one) now sits in front
of those fetches. It reads what's already stored, downloads only the missing
recent edge, and saves what comes back. Re-running a search after tweaking a
setting is now mostly free.

Three rules keep it honest, and they matter more than the speed:

- **A bar that hasn't finished is never stored.** Today's daily bar and the
  15-minute slot currently ticking still have moving closing prices. The cache
  never updates a row once written, so saving one would mean every future run
  reading a wrong price for that bar — forever. Only definitively closed periods
  are saved. You still *see* the live bar in your results; it just isn't kept.
- **The cache can never break a backtest.** Not configured, database down,
  corrupt, anything at all — it logs and quietly falls back to a normal download.
- **A partial series is never passed off as complete.** If a symbol's cached
  history has a hole in it, or starts later than the window you asked for, that
  symbol's whole window is re-downloaded rather than handed to the backtester
  with a month silently missing. Slower in that one case, never wrong.

Stocks and crypto go to their own tables (they disagree about what "a day"
means), as do daily and 15-minute bars. Scanner replay and the basket sweep were
already cached and are untouched.

## The optimizer stops tuning your stops against a replay that can't see them (2026-07-30)

Yesterday the backtest learned to run mixed resolution — 15-minute bars for the
stops, daily closes for the MACD/RSI signals. The **optimizer** hadn't caught up,
and that mattered more than it sounds.

The search tunes four knobs: minimum gain today, trailing stop, stop-loss and
take-profit. **Three of those four only ever trigger intraday.** But a MACD or RSI
strategy was flatly refused on intraday bars and forced onto daily ones — where a
stop is only checked at the closing price. On daily bars a 2% stop almost never
fires, so tightness looked practically free, and the search happily drifted toward
stops that would have whipsawed you out repeatedly in real trading. It wasn't just
inconsistent with the backtest; it was quietly recommending bad numbers.

**Now the search runs mixed resolution too.** A strategy with daily signals *and*
a price-triggered exit gets the same deal the backtest gives it: entries and exits
replayed on 15-minute bars, MACD and RSI still read off completed daily closes.
Every stop the search tries is now scored on whether it would genuinely have been
hit. The old "use 1 Day" rejection still applies to a MACD/RSI strategy with no
stop at all — there, an intraday replay would buy you nothing and only make the
signals twitchy. The bar-size dropdown says which of the two you're getting, and
warns that a mixed search takes a good deal longer (~26× the bars per day).

One thing you can't see but should know about: the daily signal history is handed
**whole** to both halves of the in-sample / out-of-sample split. Only the
15-minute replay timeline is cut in two. Split the daily series as well and the
out-of-sample half — the only number the optimizer treats as real — would lose its
MACD/RSI history and silently report "no trades". Two tests now fail loudly if
anyone ever does that.

The **basket sweep** still runs on daily bars, and now says so on the page: 12
baskets × 25 symbols of 15-minute bars is an enormous download. Its *ranking* is
still fair (every basket is scored the same way), but treat a row's stop values as
indicative and re-run that basket through the single-strategy search before
trusting them.

## Backtests can now be honest about signals AND stops at the same time (2026-07-30)

Until today a MACD or RSI strategy had to pick which half of its own rules the
backtest would lie about.

The signals had to come from **completed daily closes**, because that is exactly
what the live engine reads — so those strategies were forced onto daily bars. But
a daily replay checks your exit rules **once a day, at the close**. A position
that dipped straight through your stop-loss at lunchtime and clawed its way back
by 4pm was scored as a *winner*. Live, it would have been sold. For crypto it was
worse still: a daily bar covers a full 24 hours of continuous trading, so it hid
even more. The alternative — replaying on 15-minute bars — fixed the stops but
computed MACD/RSI off 15-minute closes, which whipsaw and look nothing like live.
Correct signals with fake stops, or correct stops with the wrong signals.

**Now it does both at once.** A strategy with daily signals *and* a
price-triggered exit (stop-loss, trailing stop or take-profit — and a hard stop
is mandatory, so this is most of them) is replayed on **15-minute bars**, with
MACD and RSI taken from a **separate daily series**. Your stops are checked every
15 minutes, and the signals are still the ones live would have seen.

The part that matters most is the part you can't see: on any 15-minute bar during
a given day, the indicator is derived **only from daily closes that finished
before that day started**. Mid-day, live, today's close hasn't happened yet — so
the backtest is never allowed to know it either. That rule is enforced in one
place and pinned by a test that builds a day whose own close *would* flip MACD
from bearish to bullish, then proves every bar of that day still reads bearish.
Without it, every backtest of every MACD strategy would quietly flatter itself.

Expect these runs to look **worse** than the old daily ones. That's the point:
the losses were always there, the daily replay just couldn't see them. The bar
size shown under the form now says what actually happens ("signals from daily
closes, stops checked every 15 minutes"), and the "your stops weren't simulated"
warning correctly stops appearing on these runs. Portfolio (multi-strategy)
backtests still pick one resolution for now.

## Fixed: "max holding time" could be silently ignored; crypto loses a fake toggle (2026-07-30)

Two related fixes, one of them a genuine bug, both found by putting the question
*"what does 'intraday' even mean for crypto?"* through the advisory council.

**The bug: a max-holding-time limit could silently not fire.** It was evaluated
*after* swing mode's "be patient on the entry day" rule, so setting "max hold 2
hours" on a swing strategy quietly meant "some time tomorrow" instead. A time
limit you set is a hard ceiling — it now sits with the stop-loss and trailing
stop, above the patience rule, and always fires. Affects stocks and crypto.

**Crypto no longer shows a "swing vs intraday" choice**, because it never
actually did anything for crypto. That toggle's whole job is to defer the softer
exits until the day *after* you buy — measured against New York's midnight,
which is meaningless for an asset that trades 24/7. So a crypto strategy set to
"intraday" got no timed sell at all; the label promised something the engine
never delivered. Now crypto strategies show, in that exact spot, the control you
actually wanted: **Max holding time**, with the plain-English note that crypto
has no market close, exit rules are live from the moment you're filled, and
stop-loss/trailing stop always apply. Crypto intraday momentum still works —
it's what the app is best at — it's just honest about how you bound a trade now.

Knock-on: the backtester used to infer crypto's bar size from that toggle, so it
now follows the hold limit instead (a cap of ≤48h means short-horizon trades that
need 15-minute bars to simulate stops honestly; no cap means daily bars are right
and much cheaper). The "Trading style" and "Max holding time" ? bubbles now spell
all of this out, including that a hold limit can only fire while QT is running.

## Dashboard: all open positions, per strategy — plus quicker jumps (2026-07-30)

- **New "Open positions — all strategies" card on the Dashboard.** Every open
  position across every strategy in one table: owner strategy, symbol, mode,
  quantity, entry vs current price, unrealized P&L, and held-since. This is the
  answer to a real confusion: the "position already open for this symbol" rail
  is **account-wide**, so the strategy holding the position is often not the
  one that got blocked — now you can see exactly who holds what. Refreshes
  every 30s; degrades gracefully to entry data if prices are unreachable.
- **Engine heartbeat moved home** — it now lives in the Engine card (where the
  mode switch is), not the Market card.
- **The strategy editor gains "Backtest →" and "Optimize →" buttons** next to
  Save/Cancel, jumping straight to those tabs with the strategy preselected.
  They run the last *saved* version — the tooltips say so in case you have
  unsaved edits open.

## Crypto "day gain" now means the same thing everywhere: rolling 24h (2026-07-30)

A real inconsistency, caught by a user comparing screens: the crypto
**scanner** measured gains over a **rolling 24 hours** (deliberately — crypto
has no daily close, and that's how crypto sites quote change), but the
**strategy engine** for a hand-picked crypto list measured **since 00:00 UTC**
(Alpaca's calendar bar). So SOL/USD could show +0.95% on the scanner while the
strategy saw +0.23% and skipped the buy — the threshold you calibrated against
one number was judged against another. The watchlist and the backtester's
intraday crypto mode had the same midnight-UTC baseline.

Now the scanner's rolling-24h definition is THE crypto day-gain everywhere:
engine candidates (custom lists and ranked universes), the watchlist's "today"
column, and the backtester's intraday crypto replay (which measures each bar
against the bar ~24h back). Daily-bar crypto backtests are unchanged — daily
closes already sit exactly 24h apart. Stocks are untouched; they genuinely have
a session day. The "Min gain today" ? bubble now spells out both definitions.

## Backtest form polish: edit shortcut + right-sized inputs (2026-07-30)

- Each strategy column on the Backtest page gains an **"Edit this strategy"**
  shortcut that jumps straight into the editor on the Strategies tab (which
  opens and scrolls to it automatically).
- **History** and **Spread cost** are now compact fields with their unit
  ("days", "%") shown inside the box — no more card-wide inputs for a
  three-digit number. (The strategy dropdown itself already gets the new
  select styling from the entry below — it's global.)

## Polish: modern dropdowns, right-sized symbol search (2026-07-30)

Every dropdown in the app sheds the dated stock-browser look: flat panel
surface, a chevron matching the app's icon set, hover/focus states like every
other control, and a dark option list on Windows (it used to flash to a light
native palette). Still a native `<select>` underneath — keyboard and screen
reader behaviour are untouched. The symbol-search box is also capped at a sane
width — a six-character ticker doesn't need the whole card — while the
suggestion list below stays wide enough for full company names.

## Onboarding: the app now explains itself from the first screen (2026-07-30)

- **README rewritten to match reality.** The status section had been frozen at
  "Phase 2 in progress" — it now lists what actually exists (backtester with
  three modes + scanner replay, optimizer, basket sweep, baskets, bar cache…),
  drops the long-gone PDT guard from the safety-rails blurb (the SEC retired
  the rule in June 2026; QT's no-leverage cap and trade-rate brake replaced
  it), and gains a **step-by-step first-run guide**: Google OAuth client →
  Alpaca paper keys → universe → preset strategy → backtest → optimize →
  shadow → paper → scoreboard.
- **The same guide lives in the app**, so nobody needs GitHub: the setup wizard
  now previews the road ahead, and the Dashboard shows a **Getting started**
  checklist — with jump buttons to each tab — until your first strategy exists,
  then retires itself.

## UX batch: compact strategies, sticky nav, folded settings, honest backtest universe (2026-07-30)

- **Strategies pack tight.** Each strategy is now a compact folded row (name,
  state, one-line summary); expand the one you care about for holdings, ranking,
  last run and actions. Clicking **Edit** now scrolls you to the editor (it was
  easy to miss at the top), switching to edit a different strategy warns that
  unsaved changes will be lost (previously it silently did nothing — a real
  bug), and every strategy gains an **Optimize** action that jumps straight to
  the Optimizer with it preselected.
- **The top tab ribbon stays visible** while you scroll.
- **Settings cards fold** to their titles with a +/− toggle — everything starts
  collapsed, including (especially) **Danger zone — liquidate holdings**, which
  is now double-folded inside Broker connection.
- **The backtest can no longer deviate from a strategy's universe.** For a
  scanner strategy, scanner replay is simply ON with the strategy's own top-N —
  the checkbox and the "risers per day" knob are gone (change them on the
  strategy, not the test). The Optimizer also lost its 1-hour bar option,
  matching the backtest.

## Zoom into any chart to see the detail (2026-07-29)

Testing a long backtest — say 500 days — meant squinting at a wall of points
where a single interesting week was only a pixel or two wide. Now you can zoom.

- **Drag across a chart to zoom in.** Press and drag a horizontal range on the
  plot; a translucent band shows what you're selecting, and on release the chart
  zooms to just those days, stretched to fill the width so you can read them.
- **Everything zooms together.** On the backtest chart the equity lines, trade
  markers, hover crosshair and date labels all follow the window. On the
  watchlist price chart the price line, moving-average/Bollinger overlays,
  buy/sell markers *and* every sub-panel (volume, MACD, RSI, relative strength)
  share the same zoom, so they never drift out of line with each other.
- **Get back out easily.** A small "Reset zoom" button appears while you're
  zoomed in; click it (or just double-click the chart) to return to the full
  range. A tiny accidental drag is treated as a normal click, so hovering for
  values still works exactly as before.

## Optimizer fix: same MACD/RSI warm-up, and it mattered more here (2026-07-29)

The optimizer splits history into an in-sample slice (it searches on) and an
out-of-sample slice (it validates on — the number you're meant to trust). Both
slices had the same dead zone as the plain backtest, and the out-of-sample one
was hit hardest: it starts partway through the window with **no** earlier bars,
so for a MACD/RSI strategy the signal was undefined for roughly its first 35
bars — which for a typical run is nearly the whole slice. The "honest verdict"
was effectively being measured with the indicator switched off.

Now the optimizer fetches the same ~150 days of warm-up before the window (daily
MACD/RSI/ATR strategies only) and gives **each** slice its own warm-up history:
the in-sample slice trades from the window start, the out-of-sample slice trades
from the split boundary with everything before it — including the whole
in-sample window — feeding the indicators. So both slices judge the strategy
with live signals from their very first traded bar. If you optimized a MACD or
RSI strategy before, re-run it: the out-of-sample numbers were understated.

## Audit: consistency fixes after the no-forced-sale change (2026-07-30)

A consistency pass over the day's features caught three seams left by the
"positions stay open at the end" change:

- **The optimizer had silently gotten harsher.** Its minimum-trades gate counted
  only *closed* trades, so a config that entered positions and held its winners
  to the end scored as if it never traded — and could never win the search. The
  gate (and the out-of-sample "untested" rules in the optimizer and basket
  sweep) now count **entries** (closed trades + positions still open), restoring
  the original intent. Result tables show "OOS entries" accordingly.
- **Buys that never exited had vanished from view.** Chart buy markers, the
  trade log, the compare chart, "Trades in view", and the portfolio log were all
  built from closed trades only — so a backtest's last few entries (still open
  at the end) appeared nowhere except the "Still open" panel. They're back
  everywhere, labelled "still open at test end".
- **Stale bits:** the optimizer still offered 1-hour bars (removed, same
  rationale as the backtest — 15-min is strictly more faithful), and the compare
  head-to-head still claimed "same universe & cash" when each strategy runs its
  own universe and sleeve (reworded).

Plus seven new borderline-edge tests: entry on the window's final bar, spread
cost as the entry-day attribution, same-day round-trip attribution, warm-up days
excluded from attribution, held-to-end counting as tested (optimizer + sweep),
and a SPY-less sweep still ranking rows as untested.

## Backtest chart: hover a day → see what was held and what it cost you (2026-07-30)

When the equity line moves on a day with **no trades**, the cause was always
invisible — a position held overnight did it, but nothing said which one. Now
the backtest records, for every simulated day, **which positions were open and
each one's contribution** to that day's move. Hovering a day on the chart shows
a second line under the trade detail: e.g. *"2 open: NVDA −0.56% · GOLD +0.06%
→ day −0.50%"* — in **percentage points of the account**, the same unit as the
chart's y-axis, so the holdings visibly sum to the line's day-over-day move. A
position closed that day appears too (labelled "sold"), entries count from
their fill price, and the per-day sum always equals the equity curve's move —
an exact decomposition, not an estimate. Biggest mover listed first.

## Basket sweep: "which theme would have beaten SPY?" — answered honestly (2026-07-29)

A new one-click experiment at the bottom of the Optimizer page: **Sweep all
baskets**. It runs the *same* parameter search across **every basket** (one
identical momentum template — $1k/trade, $5k sleeve, max 5 positions, daily
bars — so the only variable is the basket itself), then ranks the winners by
their **out-of-sample margin over SPY**: each basket's best config, measured
only on the slice of history its search never saw, against what SPY did over
that exact same window.

The honesty rules are inherited from the optimizer wholesale: in-sample numbers
are context, not proof; every row shows its combination count; a winner that
made **no out-of-sample trades ranks last as "untested"** no matter how good its
numbers look; and the leader's warnings ride along. One click saves any row as a
**disabled draft strategy** on that basket — to review, shadow, and paper-trade,
never to enable automatically. The point: the leaderboard's numbers come from
the backtester on real data, not from anyone's (or any AI's) opinion.

## Backtest: positions open at the end stay open (no forced sale) (2026-07-29)

A backtest used to **force-sell** every position still held on the last bar and
count that synthetic sale as a completed trade. That polluted the stats — a
position the strategy never chose to exit could show up as a "win" or "loss" it
never actually took, skewing win rate and profit factor. Now those positions
**stay open**: they're marked to market (so their unrealized gain/loss still
counts in net P&L and the equity curve, unchanged), but they're no longer
counted as trades. Win rate, profit factor, avg win/loss and the trade count now
reflect only **real strategy exits**.

A new **"Still open at test end"** panel lists what was held — symbol, entry,
current mark, unrealized P&L, and how long it was held — in the single, compare,
and portfolio views (tagged by strategy where more than one is involved). The
portfolio contribution table gains an **Unrealized** column, and realized +
unrealized per sleeve reconciles to the portfolio's net P&L.

## Backtest results: one metrics table, not stat boxes (2026-07-29)

The row of big stat boxes (Net P&L, Trades, Win rate, …) at the top of a backtest
result is now a compact **Metric | Value** table. In Compare mode those boxes
were pure duplication — the head-to-head table right below already shows the same
numbers for both strategies — so they're gone there, leaving just the
head-to-head. Single and Portfolio results get the same table, so all three views
read consistently. (The capital-deployment section and per-strategy contribution
breakdown are unchanged.)

## Watchlist: add a symbol without picking stock vs crypto first (2026-07-29)

Adding to the watchlist no longer needs the Stock/Crypto dropdown up front. Just
type — the search now spans **both** asset classes at once, and each result shows
a small icon (a candlestick for stocks, a coin for crypto) so you can tell them
apart as you pick. The picked symbol carries its own asset class, so it's added
correctly either way. (The Stocks/Crypto filter over the existing list is
unchanged — this was only about the add box.)

## Zoom a chart → see the trades in that window (2026-07-29)

Building on the drag-to-zoom charts: when you zoom the backtest chart into a
stretch, a **"Trades in view"** panel now appears under it, listing every buy and
sell inside the visible dates. In **Compare** mode it's tagged by strategy — so
when two near-identical strategies diverge (one line dives, the other doesn't),
you can zoom the divergence and read, trade by trade, exactly what each one did
differently. The chart reports its visible window up to the page; the panel
clears when you reset the zoom or run a new backtest.

## Backtest: "Compare" is its own mode, and bar size / cash are automatic (2026-07-29)

Tidied the backtest form so it stops asking for things it can figure out itself:

- **Three modes now: Single strategy · Compare · Portfolio.** "Compare against"
  used to sit on the single-strategy form always; now it only appears when you
  pick the **Compare** tab. Single mode is just one strategy, clean.
- **Bar size is no longer a dropdown — it's derived from the strategy.** MACD/RSI
  → 1 Day; VWAP → 15 Min; a plain strategy follows its trading style (swing → 1
  Day, intraday → 15 Min). **1-hour is gone**: 15-min is a strictly more faithful
  intraday simulation, and the live engine ticks every ~60s, so an hourly bar
  would miss intraday stops and VWAP crosses. In Compare mode both strategies
  share the finer of the two bar sizes (they must, to sit on one chart); if one
  is daily-only and the other intraday-only, that's flagged as untestable
  together.
- **Starting cash is no longer typed — it's the strategy's sleeve.** That's the
  capital it actually gets live; an arbitrary number just made idle cash look
  like a strategy flaw. In Compare mode each strategy runs on its own sleeve.

This mirrors the Portfolio backtest, which already derived its account and bar
size. History (days) and spread cost stay editable.

## Comparison backtest: less duplication, a real two-line read (2026-07-29)

Running a backtest with a "Compare against" strategy used to repeat most of the
single-strategy layout. Tightened it up so the comparison reads as a comparison:

- **Both strategies' trades now show on the chart.** The equity graph already
  drew two lines; now each strategy's buys/sells sit on *its own* line, and
  hovering any day lists who traded and why (name-prefixed) — so you get the
  trade detail without two separate logs.
- **The trade log is hidden in compare mode.** Two full logs side by side was
  noise; the chart's hover covers "who traded when".
- **Capital deployment is per strategy.** "Most ever invested" and "Time in
  market" are now rows in the head-to-head table (alongside return on money
  used), so you can see how each strategy used its cash in one place instead of
  a duplicated section.

## Sizing guardrails: catch the "all-in" trap before it bricks a strategy (2026-07-29)

A subtle, costly config trap: if your **$ per trade** is as large as your whole
sleeve/account, the strategy can hold only one position — and because the
no-leverage rail caps spending at your real equity, a *single* losing trade
drops you below one full position and silently blocks every trade after it. A
backtest of exactly this looked like "the strategy does nothing for months" when
really it went all-in once, took a small loss, and could never afford another
position. Three additions make this impossible to miss:

- **Strategy builder warning** — when $ per trade ≥ the sleeve, a red flag now
  explains the all-in trap in plain English and suggests a fraction (e.g. a fifth
  of the sleeve for ~5 positions).
- **"How many positions fit" helper** — under the sizing fields, a live line
  spells out the real cap: capital (sleeve ÷ per-trade) vs your **Max positions**
  vs how many symbols your universe actually has, and names which one binds. It
  also flags when Max positions is set higher than the real limit (a no-op) or
  lower (parking cash on purpose).
- **Backtest trade log** — a no-entry stretch caused by this now reads **"not
  enough funds for a full position (no-leverage cap)"** instead of a vague
  "blocked by a risk rail". The other rails (sleeve full, cooldown, wash-sale,
  max positions, trade-rate, daily-loss) are likewise named specifically.

## Backtest fix: MACD/RSI now work from day one of the window (2026-07-29)

Fixed a real bug that cost a lot of debugging time: a MACD or RSI strategy
couldn't trade for roughly the **first 35 days** of a backtest window, because
those indicators need a run-up of prior closes to be defined and the backtest
only ever fetched the window itself — so the signal sat "dead" until enough bars
had accumulated *inside* the window. That's why an obviously-bullish day early in
the window would show "MACD not bullish" and skip the trade.

Now, when a strategy uses a daily indicator (MACD, RSI, or ATR) on **1 Day**
bars, the backtest quietly fetches ~150 extra calendar days of history *before*
your window. Those warm-up bars feed the indicators only — they never trade,
never touch the equity curve, and never appear in the trade log — so MACD/RSI/ATR
are live from the very first day of the window you actually asked about. This
mirrors the live engine, which already looks back 120 days for its MACD. Warm-up
is skipped on intraday bars (where windows already hold plenty of bars, and
MACD/RSI backtests are locked to daily anyway).

## UI: consistent Lucide SVG icons (2026-07-29)

Swapped the text-glyph action icons (Enable/Pause/Edit/Delete, the modal close
button, the market-closed note, and the ⚠ warning markers) for a consistent
Lucide SVG icon set, driven from one place so size, stroke and colour stay in
step across the app. Sort arrows, disclosure carets and prose marks stay as
text. Purely a look-and-feel change — nothing about behaviour moved.

## Optimizer now tunes MACD speed + fuller RSI (2026-07-29)

The parameter search can now tune the **MACD speed** — it searches the slow-EMA
period (lower = a faster, less-laggy MACD) and scales the fast line along your
strategy's own ratio, so the whole MACD gets faster/slower while keeping its
shape. This is the knob for the "MACD is too laggy" problem. RSI tuning is also
completed: alongside Max RSI and the overbought exit, it now tunes **Min RSI**
too. As before, each knob is searched **only when your strategy already uses that
signal**, and the results table / plateau grid show the extra columns only then.

## Rotation-strategy tooling: regime exit, MACD ranking column, RSI optimizer (2026-07-29)

A batch that makes the rank-and-rotate workflow more complete:

- **"Sell to cash when the market turns down"** — a new stock exit that flattens
  the strategy's positions when the S&P 500 drops below its 200-day average (the
  exit-side companion to the regime *entry* filter). This is the real "go to cash
  in a downturn" switch: pure rank-and-rotate otherwise just holds the least-bad
  names. Off by default, fail-safe on missing data. Like the entry regime filter,
  it's a **live overlay not modelled in backtests** — for downturn behaviour in a
  backtest, lean on the MACD-bearish / stop exits (which are modelled).
- **MACD column in "Current ranking"** — see each candidate's daily momentum
  direction (Bullish/Bearish) next to its rank metric.
- **Optimizer tunes RSI thresholds** (Max RSI / Sell-if-RSI-above) — but only for
  strategies that already use them, so it refines your factors rather than adding
  new ones.
- **Backtest** hides the scanner-replay option for non-scanner strategies (it
  honours whatever universe the strategy is set to), and the **strategy builder**
  warns when the intraday-only VWAP rule is left on a swing/rotation strategy.

## RSI as a strategy factor — rank, entry band, and overbought exit (2026-07-29)

RSI (the 14-day momentum oscillator, already shown on the watchlist) is now a
**strategy signal**, wired the same way MACD is — so you can build a rank-and-
rotate strategy on RSI + MACD together:

- **Rank a basket by RSI** — a new `rank_by` option; ranked highest-first
  (strongest recent momentum). Because that also means the most *overbought*
  names rank top, it pairs naturally with the overbought exit below.
- **RSI entry band** — "Min RSI / Max RSI" (0 = off) under Advanced entry options.
  Setting Max RSI to ~70 skips names that are already overbought, so you enter
  strength that still has room instead of buying the top.
- **Overbought exit** — "Sell if RSI above" (0 = off) under Advanced exit options:
  book the gain when a holding gets stretched.

All three are computed from **completed daily closes** (a swing-timeframe signal,
like MACD — it doesn't wiggle intraday), and the **backtester** evaluates them
identically, so a strategy that uses RSI backtests faithfully. RSI stays off by
default; existing strategies are unchanged.

## Watchlist: RSI column + a column configurator (2026-07-29)

The watchlist now computes **RSI (14)** per symbol — the 0–100 momentum
oscillator (>70 overbought, <30 oversold) — shown as its own column, with
overbought/oversold values subtly cued and the reading explained in its ? bubble.

The old all-or-nothing "Show extra columns" toggle is replaced by a **Columns**
menu: tick exactly which optional columns you want (30 day, Daily move, vs 200d
avg, RSI, Trend), and your choice is remembered in the browser. Adding more
columns later is now a one-line change.

## Optional market orders + fractional shares, per strategy (2026-07-29)

QT's default is still **marketable limit orders and whole shares** — the
price-protected path. But that meant a small "$ per trade" could never buy an
expensive name: a $200 budget buys 0 whole shares of a $700 stock, so the buy was
skipped and the bot moved on to a lower-priced (often lower-quality) symbol
instead. Each strategy now has a **"Buy & sell at market price (allow fractional
shares)"** toggle (Sizing & risk section, off by default). Turn it on and that
strategy sends plain **market orders sized by dollar amount** — so $200 buys a
fractional slice (~0.28 shares of a $700 name) and fills immediately. The
trade-off, spelled out in the ? bubble: a market order takes whatever price is
available, with no limit to protect you on a fast or thin move. Backtests of a
market+fractional strategy now size fractionally too, so an expensive-name
strategy no longer shows a misleading 0 trades.

As a side benefit this also makes **crypto** fills immediate, which removes a
class of "orphan" positions: a slow-filling limit order that QT gave up on (and
canceled) could still fill at Alpaca a moment later, leaving a real position with
no open trade in QT's journal — invisible in the per-strategy holdings view. QT
now re-checks a canceled order and **adopts a late fill** instead of orphaning it,
on the default limit path too.

## Starter baskets replaced with 12 sector/theme lists (2026-07-28)

The shipped starter baskets have been swapped out for a broader, more useful set:
**11 GICS-style sector baskets** — Information Technology, Health Care, Financials,
Consumer Discretionary, Consumer Staples, Industrials, Communication Services,
Energy, Utilities, Real Estate, Materials — each with roughly 30 large-cap US
stocks, plus a **High-Yield & Dividend** theme (30 income-oriented names). The old
starter set (Defense, Banking, Gold & Mining, REITs, Big Tech, Semiconductors,
Energy, Healthcare, Sector-ETFs) is gone from what ships to new installs.

Seeding now keeps these baskets in sync: on start-up the shipped baskets are
refreshed to match the canonical lists, so an update reaches you automatically.
Baskets *you* created are never touched. Note: on an already-running instance the
old starter baskets aren't deleted for you — you can remove any you don't want from
the Baskets screen. A handful of energy/materials names in the sector lists have
since been acquired or merged away (Pioneer/PXD, Marathon Oil/MRO, WestRock/WRK) —
they're included as given but may not trade on Alpaca, so prune them if you like.

## Optimizer explains a 0-trade result instead of looking like a failure (2026-07-28)

If an optimizer run comes back with 0 trades on every combination (0% in- and
out-of-sample), that's almost never "the strategy is unworkable" — it's usually a
setup mismatch (e.g. the **"price above VWAP"** rule or an **entry time window**
on **daily** bars, which can't be evaluated intraday, so every entry is rejected).
The backtest already computes a plain-English reason for this; the optimizer was
throwing it away. Now:

- The optimizer **surfaces that reason** (from the most permissive combo) at the
  top of the results — "No configuration traded — this isn't a verdict on the
  strategy, it's a setup issue: …" — so a 0-trade run is self-explanatory.
- The Optimizer now **auto-picks 15-minute bars** when the selected strategy uses
  VWAP or an entry-time window (intraday rules that daily bars can't evaluate), so
  the common case just works — with a note explaining why, and the daily default
  everywhere else. If you override back to 1 day with those rules still on, the
  backend **fails fast** with clear guidance rather than running an empty search.

## Optimizer & backtest symbol cap raised 25 → 50 (2026-07-28)

A 30-symbol sector basket wouldn't optimize ("Max 25 symbols per search"). The
optimizer downloads bars **once, batched**, then reuses them across every
iteration, so the symbol count barely touches the rate limit — the 25 was just a
conservative copy from the backtest. Raised both to **50**, which comfortably
fits a full sector basket. (Subsetting to a top-25 by rank was considered and
rejected: validating a parameter set across the *whole* basket is the point —
params that survive 30 names are far likelier real than ones tuned to a picked
25.)

## Journal + P&L are now per broker account (2026-07-28)

After switching Alpaca paper accounts, the journal and per-strategy P&L still
showed the OLD account's trades. Now every trade is **stamped with the account it
was made on** (the Alpaca account number, captured on key-save and on each engine
cycle), and the **Journal** and Dashboard **"Strategy contributions"** views
default to the **current account** — with an **Account** dropdown to view a past
account, the legacy pre-tagging trades ("Earlier / untagged"), or "All accounts".
So the moment you point QT at a different account, the views go clean on their
own; nothing is deleted.

New: `GET /api/engine/accounts` (the accounts present in history) and an optional
`account` filter on `/journal`, `/strategy-pnl`, and `/strategy-pnl-daily`. The
account picker only appears when there's more than one account to choose from.

Note: trades made *before* this change are untagged; a one-time SQL backfill can
assign them your old account id if you want them attributed rather than grouped
as "Earlier".

## Strategies: "Current ranking" — see who's eligible in a ranked strategy (2026-07-28)

For a ranked strategy (a basket, or a watchlist/custom list with "Rank & take top
N" on), the card now has a **"Current ranking — who's eligible right now"**
expander. It ranks the *whole* pool live by the strategy's metric and shows every
symbol with its rank and value, marking the top-N (✓) that the strategy will
actually consider and greying out the rest. So if a name you expected (e.g. AAPL)
isn't being bought, you can see at a glance that it's ranked, say, #12 — well
outside the top 3 — and never reaches the entry rules. Backed by a new
`GET /api/strategies/{id}/ranking`; non-ranked strategies get a short note
explaining why there's nothing to rank. The live ranking uses the exact same code
path the engine uses to pick candidates, so what you see is what it does.

## Strategies: "Last run" — see exactly why a strategy did (or didn't) buy (2026-07-28)

Every strategy card gains an expandable **"Last run — why it did / didn't buy"**
section: the engine now records a decision trace each entry cycle, and this shows
the most recent one. It tells you:

- **When** it last ran and **where it looks** for candidates — including the
  crucial "top N of the basket, ranked by X — only these are evaluated" line, so
  it's obvious when a name you expected (e.g. AAPL) wasn't even considered because
  it's not in the top-N.
- A one-line **outcome** (e.g. "Regime filter blocked stock entries", "Market
  closed", "Evaluated 3 candidate(s); bought 0").
- A per-symbol table: each candidate it evaluated, its day move, the **decision**
  (bought / skipped / blocked) and the **why** in plain English — "MACD not
  bullish", "price not above VWAP", "day gain 0.30% < required 0.50%", "wanted to
  buy but max positions reached", etc.

Backed by a new `GET /api/strategies/{id}/last-run`. The trace is in-memory (it
resets when the app restarts) and building it never affects trading — the entry
loop is wrapped so a trace hiccup can't change a decision.

## Strategies: see the holdings each strategy currently owns (2026-07-28)

Every strategy card now has an expandable **Holdings (N)** section (shown when it
has open positions) listing exactly what that strategy is holding right now:
symbol, quantity, entry price, current price, and **unrealized P&L** (with a live
total). Prices are best-effort from Alpaca and degrade gracefully to entry data
if the broker is momentarily unreachable. Backed by a new
`GET /api/strategies/{id}/holdings`.

## Liquidate: closing orphans is now opt-in (2026-07-28)

The "Liquidate holdings" action now defaults to closing **only the positions QT
tracks** — matched to the broker by the same normalization reconciliation uses,
and by QT's *own* quantity, so a co-existing bot's shares in the same symbol are
never touched. A separate checkbox, **"Also close positions QT doesn't track
(orphans)"** (off by default, with a clear warning), does the whole-account
flatten. Orphans should never exist, and if another bot trades the same Alpaca
account, QT must not close its positions.

## Settings → Broker connection: switch accounts & liquidate everything (2026-07-28)

You can now manage the Alpaca connection AFTER first-run, from **Settings →
Broker connection** — previously the keys could only be entered once in the setup
wizard. Two things live here:

- **Change / replace API keys** — paste a different Alpaca paper (or live) key
  pair to point QT at another account. QT verifies them against Alpaca before
  saving (same validation as first-run). Use it to move to a fresh paper account.
- **Liquidate all holdings** (danger zone) — closes **every** position at the
  broker at market, *including any QT doesn't track* (the "orphan" holdings the
  reconciliation warnings flag), cancels resting orders, and marks QT's own open
  trades closed. It's the clean-slate button for starting over. Gated behind
  typing **LIQUIDATE** to confirm; it fires a Slack alert and an audit entry, and
  reports how many positions closed and how many were orphans.

The intended "start fresh" flow: pause the engine → liquidate → replace the API
keys with the new account. (Cashing out at Alpaca itself is done in Alpaca, not
here.)

## Symbol chart overlays: clearer colors, ? explainers, and "good zone" shading (2026-07-28)

Polish on the symbol-detail review chart, from testing feedback:

- **MA50 vs MA200 are now easy to tell apart** — the 200-day line is cyan against
  the 50-day's gold, instead of two near-identical oranges.
- **Every overlay checkbox has a ? explainer** — what it measures and, crucially,
  how to read it: moving averages (trend + golden/death cross), EMA 9/21 (faster
  momentum), Bollinger Bands (volatility envelope), the ATR-stop line, volume,
  MACD, RSI, relative-strength, and the buy/sell journal markers.
- **The "good" zone is shaded green** so it reads at a glance:
  - **RSI** shades the **50–70 band** — healthy uptrend momentum that isn't yet
    overbought (below 30 is oversold, above 70 is overbought/caution).
  - **Relative strength vs SPY** shades **above 1.0** — the region where the
    symbol is out-performing the S&P 500 since the start of the window. Above 1.0
    and rising = a market leader.

## Top-N ranking now works for watchlists and custom lists, not just baskets (2026-07-28)

Ranking a pool of symbols and trading only the strongest few — plus rotating out
of names as they weaken — used to be a basket-only feature. It now works for a
**watchlist** or a **custom list** too, which is the natural home for the
"long-term rotation across a specified list" strategy.

- On a watchlist or specific-symbols strategy, a new **"Rank & trade only the top
  N of this list"** toggle appears (off by default, so existing strategies are
  unchanged). Turn it on and you get the same **Rank by** (momentum / 30-day
  return / relative strength / RS-vs-SPY) + **Take top N** controls baskets have.
- **Rotate out when it leaves the top N** is no longer basket-only — any ranked
  strategy (basket, or a ranked watchlist/custom list) can rotate as strengths
  shift: hold the strongest few, sell one when it drops out, let a new leader in.
- Nothing changes for existing strategies: baskets stay always-ranked; scanner
  and "scanner + watchlist" are already ranked by the scanner, so the toggle
  doesn't apply there. It remains a **live** feature (a backtest can't
  reconstruct the historical daily ranking, so it tests the whole pool).

## Strategy builder shows the symbols in play — editable inline (2026-07-28)

Pick a universe and the builder now shows **exactly which symbols the strategy
will consider**, right there, editable on the spot:

- **Basket** → the basket's members as chips you can remove, plus a search box to
  add more. A clear ⚠ note says editing changes that **shared basket everywhere**
  it's used (and that the strategy still trades only the top-ranked few).
- **Specific symbols** → your own list (as before), now labelled "…in play (N)".
- **Watchlist / Scanner + watchlist** → your watchlist for this asset class, add
  and remove inline (it updates the Watchlist everywhere).
- **Scanner** → explained plainly: it's dynamic (each day's top risers), so there's
  no fixed list to edit — add names to your Watchlist and use "Scanner + watchlist"
  to always include them.

Also clarified the **entry-window** help: the window restricts *buying* only, and
only within regular US market hours — selling (exits, trailing stops, stop-losses)
is never blocked by it, and for stocks nothing trades pre-/after-hours anyway.

## Strategy builder redesigned — grouped sections, compact fields (2026-07-28)

The strategy builder had grown into one long scroll where every new option was
bolted on at the bottom, each explanation sat as a wall of text, and a single
"3%" value got a text box as wide as the screen. It's been rebuilt around how you
actually think about a strategy:

- **Five clear sections**, each an outlined card with a one-line subtitle:
  **Start here** (preset, name, asset class, trading style) → **Universe** →
  **Entry criteria** → **Exit criteria** → **Sizing & risk**.
- **Common controls first, rarer knobs tucked away.** Each of Entry, Exit and
  Sizing has an **Advanced** drop-down: advanced entry (max gain, price band,
  entry window, entry slippage, MACD periods), advanced exit (max holding, exit
  slippage, VWAP/MACD/rotation exits), and advanced sizing (the ATR volatility
  stops & sizing plus the regime override).
- **Value-sized inputs.** A percentage now gets a small box, and related numbers
  sit side by side as a set instead of a stack of full-width slabs.
- **Asset class and trading style are sliders**, not dropdowns.
- The explanatory text is all still there — now in the **?** bubbles next to each
  field, so the form stays scannable and a newcomer isn't hit with a wall of
  prose before they've started.

## Symbol chart: toggleable review overlays (MACD, RS, markers, and more) (2026-07-28)

Click any symbol (watchlist, scanner, backtest) and the detail chart now has a
row of **overlay checkboxes** — each adds or removes its data on the chart, so
you can review what a symbol (and your trades on it) were doing:

- **Buy / sell markers** (on by default) — every entry and exit from the trade
  journal for that symbol, placed on its day, with the price, reason, and P&L in
  the tooltip.
- **50 & 200-day moving averages** and **EMA 9 & 21** drawn on the price line.
- **Bollinger Bands** (20-day, 2σ) as a shaded volatility envelope.
- **ATR-stop level** — an illustrative "close − 2×ATR" line showing how far a
  volatility-based stop sits below price and how it breathes (not a live stop).
- **Volume**, **MACD** (12/26/9 with signal + histogram), and **RSI** (14) as
  their own sub-panels beneath the price, sharing the crosshair.
- **Relative strength vs SPY** (stocks) — the symbol's performance divided by
  SPY's, rebased to 1.0 at the window start; above 1.0 and rising = leading the
  market.

All of it is **display-only** — indicators are computed in the browser from the
daily bars the chart already loads (high/low/volume now ride along in the history
response) and never touch a trading decision. Everything is off by default except
the trade markers, so the chart stays clean until you ask for more.

## Optimizer can now search against the scanner's historical risers (2026-07-28)

The parameter search used to fall back to your watchlist whenever the strategy's
universe was the **scanner** — you couldn't tune a "today's risers" strategy
against the thing it actually trades. Now it can **replay the scanner**, exactly
like the backtest already does: for each past day, only that day's **top-N
risers** (read offline from the bar cache) are eligible to enter, and the search
optimizes the entry/exit knobs against that real, day-varying universe.

- Turn it on with a **"Scanner replay"** checkbox on the Optimizer (it's on by
  default when the selected strategy's universe is the scanner). A **risers per
  day (top N)** control sets how many of each day's movers are eligible.
- The symbol picker, the 25-symbol cap, and the bar-size selector don't apply in
  this mode — the universe is however many names made a top-N list, and the bar
  size comes from the cache (15-minute if you've run an intraday sweep, else
  daily). Needs a completed sweep first (Settings → Historical bar cache).
- The out-of-sample discipline is unchanged: the same top-N eligibility map is
  applied to both the in-sample (first ~70%) and out-of-sample (last ~30%)
  slices, so a symbol can still only be entered on the days it actually rose, and
  only the out-of-sample number is treated as real.

Under the hood the cache-reading logic is now shared by the backtest and the
optimizer (one `load_scanner_replay_dataset` helper), so both build the universe
identically. Fixed-list and basket searches are unchanged.

## Watchlist "Trend" sparkline now shows the 30-day daily trend (2026-07-28)

The little trend line next to each watchlist symbol used to be built from
15-minute **intraday** bars. On the free IEX data feed those bars are thin —
empty outside market hours and sparse for many stocks — so most rows just said
"no data", and the few that had a line often looked flat. Crypto (which trades
24/7) was fine; stocks were the problem.

It now draws the **last ~30 daily closes** instead — daily bars exist for every
tradable symbol and don't depend on the intraday feed, so every row gets a real
trend line regardless of the time of day. The column is relabelled **"Trend
(30d)"**. Green means the period ended higher than it started, red lower.

## Fix: MACD and rotation settings were silently dropped on save (2026-07-27)

The MACD entry/exit toggles (and their periods) and the sector-rotation
"rotate out of the top-N" exit weren't actually being saved — the strategy's
validation model didn't know about those fields, so it discarded them every time
you saved a strategy. In practice that meant turning MACD on, or building a
rotation strategy, did nothing: the flags never reached the engine. Now those
fields are declared (with a guard that MACD's fast period stays below its slow
period), so they persist and take effect. Two regression tests lock it in.

## ATR-based stops & position sizing — optional, off by default (2026-07-27)

A strategy can now size its **stop** and its **position** to each symbol's real
volatility, measured by **ATR** (Average True Range — the symbol's typical daily
move, gaps included). Two independent switches, both **off by default**, that
sit in a new "Volatility-based stops & sizing" area under Sizing & safety:

- **ATR stop** (`atr.stop_mult`, 0 = off). Instead of a fixed stop-loss %, the
  hard stop is placed at **stop_mult × ATR%** below entry. A volatile stock gets
  a **wider** stop and a calm one a **tighter** stop, so ordinary daily wiggle
  doesn't shake you out of a good trade. It stays the **hard** stop (top
  priority), and it's **recomputed from the current bar's ATR every tick**, so
  the stop *breathes* with the symbol's volatility — widening as things get wild,
  tightening as they calm. If the ATR can't be computed (a data blip or too
  little history) it **falls back to the fixed stop-loss** — a position is never
  left without a stop.
- **ATR sizing** (`atr.risk_usd`, 0 = off; needs the ATR stop on). Each position
  is sized so that a stop-out loses **about `risk_usd`**, whatever the symbol's
  volatility: `size = risk_usd / (stop_mult × ATR% / 100)`. A wild name gets a
  **smaller** position, a calm one a **larger** position, for the **same dollar
  risk**. The computed size is **capped at the strategy's sleeve budget** (so a
  very calm name can't compute a size that blows the sleeve) and falls back to
  the fixed **$ per trade** when ATR sizing is off or the ATR is unavailable.

Both features reuse the same look-ahead-safe **daily-bar** ATR (completed bars
only — never today's in-progress bar), fetched together with the MACD signal in
one call so there's no duplicate work. The backtester mirrors both exactly, and
non-ATR runs stay byte-for-byte identical. The editor spells all of this out in
plain English next to each field, with an advanced **ATR period** (default 14).

## Basket ranking: "Relative strength vs S&P 500" + a MACD when-to-use hint (2026-07-27)

Two small additions to how basket strategies rank their members and how MACD is
explained.

- **New basket ranking — "Relative strength vs S&P 500" (`rs_vs_spy`).** For a
  basket universe you can now rank members by how far each one has **out-performed
  the market** over a ~90-day window: the member's return *minus* SPY's return
  over the same span. This is the classic sector-rotation "relative strength", and
  it's different from the existing **"Relative strength (vs 200-day average)"** —
  a stock can sit above its *own* long-term trend yet still be **lagging the
  market**. Positive means it's beating SPY; negative means it's trailing. Like
  every top-N ranking it's a **live** feature only: a backtest can't reconstruct
  the historical daily basket ordering. It's **stock-only** (SPY is a stock
  benchmark), so a crypto basket can't select it — the option is hidden for crypto
  strategies and rejected by the server if forced.
- **MACD "when to turn it on" hint.** Next to the MACD entry/exit switches there's
  now a one-line pointer: best for swing / daily strategies (it avoids buying into
  fading momentum), and better left off for fast intraday trades.

## Optimizer: it now tells you exactly which symbols it tests (2026-07-27)

The parameter search wasn't clear about *which* symbols it validates on, so it
was easy to run one and not know what universe was used. It now spells it out.
Under the symbol picker, a live line says "This search will test on: …" for the
strategy you picked — your hand-picked symbols, the strategy's own list, a named
basket's members, or (highlighted) your watchlist. That last case is the
gotcha: a **scanner** strategy can't be replayed on the historical daily risers,
so the search falls back to your asset-class **watchlist** — now stated plainly
instead of buried. The results header also lists the exact symbols it tested.

## MACD momentum signal — optional entry filter & exit (2026-07-27)

A strategy can now use **MACD** (Moving Average Convergence Divergence, the
classic 12/26/9 momentum gauge) as **two independent, opt-in switches** — both
**off by default**:

- **"Require bullish MACD to enter"** — only open a position while the MACD line
  is above its signal line. It's **fail-closed**: if MACD is bearish *or* there
  isn't enough history to decide, the entry is blocked. An unproven signal is
  never a green light.
- **"Exit when MACD turns bearish"** — sell when the line crosses back below its
  signal. It sits with the *soft* exits: a confirmed bearish cross triggers it,
  an unknown reading never forces a sale, and (in swing mode) it waits until the
  day after entry — while your **hard stop-loss always keeps priority**.
- **Configurable periods** under an advanced disclosure (fast / slow / signal,
  default 12 / 26 / 9), shared by both switches.

It's a **deliberate, explainable toggle you set — not something the optimizer
auto-tunes.** MACD is deliberately kept out of the parameter search.

**A nuance worth stating plainly:** the **live engine always computes MACD from
DAILY bars** (excluding today's still-forming bar, so there's no look-ahead),
whereas a **backtest computes it from whatever timeframe the backtest itself is
replaying** (up to and including the prior completed bar). For the intended
daily/swing use (1Day, or 1Hour tracking a daily signal closely enough) these
line up; on much finer bars they would drift — which is why MACD is documented
as a daily/swing signal.

## Strategy optimizer — a parameter search (2026-07-27)

Instead of guessing a strategy's numbers (min gain, trailing stop, stop-loss,
take-profit), the new **Optimizer** tab **searches** for settings that actually
held up — running the *same* backtester across many combinations. It's a
**parameter search, not "AI"**, and it's built from the ground up to fight
**overfitting** (the trap where settings look brilliant on the history you tested
and fall apart on anything new):

- **Out-of-sample, always.** The search only ever sees the **first ~70%** of the
  history. Every winner is then re-run on the **final ~30% it never looked at** —
  and the app treats *only* that out-of-sample number as real. The in-sample
  number is shown beside it, clearly labelled "not proof".
- **It counts the coins.** It always tells you **how many combinations were
  tested** — a winner out of 12 tries means far less than a winner out of 2,000.
- **Plateaus, not peaks.** Around the winner it sweeps each knob one step either
  way and charts those neighbouring scores, so a dependable setting (its
  neighbours score similarly) is easy to tell from a lone lucky spike.
- **vs buy-and-hold.** It puts the winner's out-of-sample return next to simply
  holding the same symbols — if trading can't beat that, it destroyed value.
- **Validate across several symbols**, never one ticker.

The result is a **hypothesis**, not a verdict: a one-click **"Save as draft
strategy"** creates a new strategy from the winning settings — born **disabled**,
mirroring the one you tuned — that still has to earn its way up shadow → paper.
Nothing is ever enabled for you. The search runs as a background job with a live
progress bar, and reuses the existing backtester unchanged.

## Portfolio (multi-strategy) backtest (2026-07-27)

The backtester could only replay **one** strategy at a time — but live, the engine
runs **all** your enabled strategies at once, competing for the **same account**.
The new **Portfolio** mode on the Backtest page closes that gap: pick two or more
strategies and replay them together over one shared cash balance and the exact
**global risk rails** the live engine enforces — the cap on total open positions,
exposure never exceeding your equity (no borrowing), the account-wide trade-rate
limit, and the daily-loss kill switch. Each strategy still keeps its own sleeve
budget, position sizing, and universe; they simply take turns drawing from one
wallet, just like they would with real money.

You get one **portfolio equity curve** and the usual honest metrics (net P&L, win
rate, max drawdown, trades, profit factor) plus the capital-deployment tiles for
the whole book — and, crucially, a **per-strategy contribution breakdown**: how
much realized profit or loss each strategy added, its trade count, and its share
of the result. Those contributions add up exactly to the portfolio total, so you
can see which strategy actually carried the book and which just diluted it. Same
honesty framing as the single backtest — modeled fills, a partial data feed, and
**past results predict nothing**; a scanner strategy falls back to its watchlist
because a shared timeline can't reconstruct the historical daily risers. The
existing single-strategy backtest and the head-to-head "Compare against" feature
are untouched.

## DCA baseline sleeve strategy (2026-07-27)

A new **"DCA baseline sleeve (weekly)"** preset: an always-on
dollar-cost-averaging sleeve that buys a **fixed set of ETFs on a fixed cadence**
(every 7 days by default) no matter what the market is doing. It's the dumb,
steady baseline the momentum strategies have to beat — the same dollars, rain or
shine, with no timing decisions. Pick the preset (it seeds SPY + QQQ; edit to
your own always-buy list), set "Buy every N days," and it accumulates.

Under the hood each scheduled buy is its **own independent lot** — a clean,
single position — not an averaged-together basis. That keeps the engine's
one-position-per-symbol model intact while letting several lots of the same
symbol coexist: the DCA path bypasses **only** the "already open for this symbol"
check, so a fresh weekly buy is allowed even while earlier lots are still held.
**Every other safety rail still applies** — the sleeve budget, the exposure cap
(never more than your cash), the account-wide trade-rate limit, and the
daily-loss kill switch. Lots are buy-and-hold with no momentum exits unless you
add a stop yourself.

## Sector-ETF relative-strength rotation strategy (2026-07-27)

A new **"Sector rotation"** preset (and the engine support behind it): hold only
the strongest few names in a basket and rotate. It buys the top-N ranked by
relative strength (price vs its 200-day average) and — the new part — **sells a
holding the instant it drops out of the top-N**, rotating into whatever's leading
now. That rank-drop-out exit is a new per-strategy option ("Rotate out when it
leaves the top N"), shown for basket strategies, with a wide stop-loss as the
safety net. Low turnover, and it leans on daily bars so the thin free intraday
feed doesn't matter. Like all top-N ranking, it's a live-engine feature (a
backtest can't reconstruct the historical daily ranking). Pick the preset, then
choose your Sector-ETFs basket in the editor.

## Backtest: compare two strategies side by side (2026-07-27)

The backtest page can now run **two** strategies over the exact same universe,
period, cash and spread and put them head to head — a "Compare against
(optional)" picker next to the strategy selector. When set, both strategies run
and you get a **head-to-head table** (net P&L, win rate, max drawdown, trades,
profit factor, return on money used — the winner of each highlighted) plus **both
equity curves overlaid** on one chart. Because every setting except the strategy
rules is identical, the difference you see is purely the strategy. Part of Phase
4's "side-by-side compare of strategy configs."

## Fix: crypto scanner's volume floor was set ~40× too high (2026-07-27)

Crypto scanner replay was only finding ~16 "riser" days across a whole year of
cached data, and the live crypto scanner was surfacing far fewer movers than it
should. The cause: the crypto scanner's minimum dollar-volume filter defaulted to
**$1,000,000/day**, but Alpaca's crypto feed is thin — even the busiest pairs
trade only ~$70k–$400k a day. So the floor rejected almost every day/pair
regardless of how much they moved. Lowered the crypto default to **$25,000**,
which matches the feed while still excluding essentially-untraded pairs. This
fixes both scanner *replay* (backtests now see a full year of crypto movers
instead of 16 days) and the *live* crypto scanner. Stocks are unchanged. If
you'd set a custom crypto volume filter in Settings, check it's not still up at
the old level.

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
