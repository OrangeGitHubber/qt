// Every configurable market term gets a plain-English explanation and a link
// to a reliable source — the UI must teach as it configures.

export interface GlossaryEntry {
  term: string;
  explain: string;
  url: string;
}

export const GLOSSARY: Record<string, GlossaryEntry> = {
  trailing_stop: {
    term: "Trailing stop",
    explain:
      "A sell trigger that follows the price up: if the price falls X% from its highest point since you bought, sell. Locks in gains while giving the trade room to run.",
    url: "https://www.investopedia.com/terms/t/trailingstop.asp",
  },
  stop_loss: {
    term: "Stop-loss",
    explain:
      "A hard floor: if the price drops X% below what you paid, sell immediately. Your maximum planned loss per trade. QT requires one on every strategy.",
    url: "https://www.investopedia.com/terms/s/stop-lossorder.asp",
  },
  take_profit: {
    term: "Take-profit",
    explain: "A target: once the trade is up X%, sell and bank the gain. 0 disables it.",
    url: "https://www.investopedia.com/terms/t/take-profitorder.asp",
  },
  vwap: {
    term: "VWAP",
    explain:
      "Volume-Weighted Average Price — today's average price weighted by how much traded at each level. Price above VWAP suggests buyers are in control today.",
    url: "https://www.investopedia.com/terms/v/vwap.asp",
  },
  scanner_replay: {
    term: "Scanner replay",
    explain:
      "Backtests against the market's actual top risers on each past day — the names the live scanner would have surfaced — instead of a fixed symbol list. Risers are reconstructed from a cached year of daily bars, ranked by each day's intraday PEAK gain (so pump-and-fade names aren't missed). Run a daily sweep in Settings first; add an intraday sweep to replay on 15-minute bars, which lets intraday exits (flatten-before-close, VWAP, entry window) behave for real. Each day only that day's top-N are eligible; your entry rules then decide. The closest a backtest gets to the live 'today's risers' engine.",
    url: "https://www.investopedia.com/terms/b/backtesting.asp",
  },
  replay_top_n: {
    term: "Risers per day (top N)",
    explain:
      "In scanner replay, how many of each past day's biggest movers are eligible to enter that day — top 3, top 10, top 20. The cache stores a wide set (100) of risers per day, so this is a read-time filter: dial it up or down and the backtest re-runs instantly, no re-sweep. Fewer names = only the very strongest movers (more concentrated, more survivorship-flattering); more names = closer to what a broad scanner would surface.",
    url: "https://www.investopedia.com/terms/b/backtesting.asp",
  },
  share_price_band: {
    term: "Share-price band",
    explain:
      "Limit this strategy to symbols within a per-share price range — e.g. set Max to 10 to trade only movers under $10, or a Min to avoid sub-$1 names. It narrows the strategy's universe on top of the scanner's own price floor. 0 on either side means no limit that way.",
    url: "https://www.investopedia.com/terms/s/stock.asp",
  },
  max_day_gain: {
    term: "Max gain today",
    explain:
      "A ceiling on how far a stock can already be up before the bot will buy it. Momentum buys strength, but a stock already up 20%+ is often near exhaustion — buying it means chasing a blow-off top that's prone to reverse. Set e.g. 10 to skip anything already up more than 10% today. 0 turns the ceiling off (no limit).",
    url: "https://www.investopedia.com/terms/b/blowofftop.asp",
  },
  regime_filter: {
    term: "Regime filter",
    explain:
      "Only buy stocks while the S&P 500 is above its 200-day moving average — a common definition of a rising market. When it's below, QT stops opening stock positions (exits still work).",
    url: "https://www.investopedia.com/terms/m/movingaverage.asp",
  },
  dollar_volume: {
    term: "Dollar volume (on Alpaca's feed)",
    explain:
      "Quantity traded × price. IMPORTANT: this is only what Alpaca's FREE feed saw, not the whole market. Stocks use the last completed trading session on IEX (~2–3% of US volume) — a stable full day, since today's bar is only partial while the market is open. Crypto is a rolling 24-hour total from Alpaca's aggregated feed. So a real BTC day is billions, but this column may show only a few thousand — treat it as a liquidity signal to compare LIKE-for-like across symbols, not the true market total.",
    url: "https://www.investopedia.com/terms/v/volume.asp",
  },
  wash_sale: {
    term: "Wash sale",
    explain:
      "Selling a stock at a loss and re-buying it within 30 days disallows the tax deduction for that loss. QT can block or warn. The IRS counts your OTHER accounts too — QT can only see its own trades.",
    url: "https://www.investopedia.com/terms/w/washsalerule.asp",
  },
  leverage: {
    term: "Leverage / margin",
    explain:
      "Trading with borrowed money. Since June 2026, accounts over $2k can trade with up to 4x intraday buying power — meaning losses are also 4x faster, and you can lose more than a position's worth in hours. QT keeps this off unless you unlock it at the server level AND confirm the risk.",
    url: "https://www.investopedia.com/terms/m/margin.asp",
  },
  entry_slippage: {
    term: "Entry slippage (marketable buffer)",
    explain:
      "QT never sends a naked market order — it sends a 'marketable' limit priced a little THROUGH the market so it crosses the spread and fills. This is how far through, for buys: 0.5% = default. Higher fills more reliably on fast/thin names but at a worse price; 0% is a passive limit at the quote that may not fill. Live/paper only — the backtest models fills with its own spread-cost input.",
    url: "https://www.investopedia.com/terms/s/slippage.asp",
  },
  exit_slippage: {
    term: "Exit slippage + escalating chase",
    explain:
      "How far BELOW the market QT prices the marketable SELL limit when exiting (1% = default). If a sell misses the fill (price dropping faster than the order, or a thin book), QT cancels and retries next cycle. Set 'Max exit slippage' above the base to ESCALATE: each miss widens the sell price one step further down, up to the max, so a fast drop still gets out — still a limit, never a market order. Equal base and max = no escalation. Wider = more certain exits, worse price. Live/paper only.",
    url: "https://www.investopedia.com/terms/s/slippage.asp",
  },
  swing_mode: {
    term: "Trading style: Swing vs Intraday",
    explain:
      "Two opposite styles, so it's one choice. SWING holds positions overnight and judges exits over days (soft exits like take-profit wait until the day after entry; stops still act same-day). INTRADAY flattens before the close and never holds overnight (for stocks; crypto has no close). Stocks default to Swing — spreads and free-data limits punish rapid stock trading, though the trade-off is overnight-gap risk. Crypto is the intraday lab (24/7, cleaner data).",
    url: "https://www.investopedia.com/terms/s/swingtrading.asp",
  },
  sleeve: {
    term: "Sleeve budget",
    explain:
      "The maximum dollars this one strategy may hold at once. Keeps multiple strategies from fighting over the same cash and caps the damage any single strategy can do.",
    url: "https://www.investopedia.com/terms/a/assetallocation.asp",
  },
  paper_trading: {
    term: "Paper trading",
    explain:
      "Simulated trading with fake money but real market prices, on Alpaca's paper environment. Identical mechanics to live trading with zero financial risk.",
    url: "https://www.investopedia.com/terms/p/papertrade.asp",
  },
  shadow_mode: {
    term: "Shadow mode",
    explain:
      "The engine runs its full decision loop and writes every would-be trade to the journal, but places NO orders anywhere — not even simulated ones. The zero-risk first rung of the autonomy ladder.",
    url: "https://en.wikipedia.org/wiki/Shadow_system",
  },
  daily_loss_limit: {
    term: "Daily loss kill switch",
    explain:
      "If today's realized losses reach this limit (in dollars or % of account, whichever is lower), the bot stops opening new positions until tomorrow and alerts you.",
    url: "https://www.investopedia.com/terms/d/dailytradinglimit.asp",
  },
  bar: {
    term: "Bar (candle)",
    explain:
      "One slice of price history: the open, high, low, close and volume for a period. A '1 hour' bar summarises an hour of trading. Smaller bars = more detail and slower backtests; bigger bars = faster but coarser. Two years of hourly bars for one stock is ~3,500 bars.",
    url: "https://www.investopedia.com/terms/c/candlestick.asp",
  },
  atr: {
    term: "ATR — typical daily move",
    explain:
      "Average True Range: how much this symbol usually moves in a day, as a % of price (gaps included). It's the noise floor for your stops — a 2% trailing stop on a stock that routinely swings 4% will shake you out of good trades for no reason. Set stops wider than ATR, not tighter.",
    url: "https://www.investopedia.com/terms/a/atr.asp",
  },
  change_30d: {
    term: "30-day change",
    explain:
      "Price change over the last 30 calendar days. Medium-term momentum — slower and less noisy than today's move, and closer to the horizon a swing strategy actually trades on.",
    url: "https://www.investopedia.com/terms/m/momentum.asp",
  },
  sma200: {
    term: "vs 200-day average",
    explain:
      "How far the price sits above (+) or below (−) its 200-day moving average — the same trend test the regime filter applies to the S&P 500, but for this symbol. Above it is generally considered an uptrend; below it, a downtrend.",
    url: "https://www.investopedia.com/terms/m/movingaverage.asp",
  },
  capital_deployed: {
    term: "Capital deployed",
    explain:
      "How much of your account was actually invested, versus sitting in cash. A bot with $200 per trade on a $5,000 account only ever risks 4% — so even a great return on those trades barely moves the account. Judge the strategy by the return on money used; judge your settings by how much you deployed.",
    url: "https://www.investopedia.com/terms/c/capital-allocation.asp",
  },
  hold_benchmark: {
    term: "Buy-and-hold benchmark",
    explain:
      "What you'd have made by simply buying the same symbols on day one and doing nothing. If a trading strategy can't beat this, the trading is destroying value — you'd be better off just holding.",
    url: "https://www.investopedia.com/terms/b/buyandhold.asp",
  },
  basket: {
    term: "Basket (curated symbol list)",
    explain:
      "A named group of symbols you pick — a theme like Defense or Banking. It is NOT an authoritative sector database: Alpaca provides no sector or industry data on this plan, so the starter baskets are hand-picked and yours to edit, and they drift as companies change. A basket is just a convenient, editable list.",
    url: "https://www.investopedia.com/terms/s/sector.asp",
  },
  dca: {
    term: "Dollar-cost averaging",
    explain:
      "Buying a fixed set of symbols on a fixed schedule (e.g. every 7 days) regardless of price or momentum — the same dollars, rain or shine. It removes timing decisions and is the steady baseline a smarter strategy has to beat. QT implements it as independent lots: each scheduled buy is its own separate position (no averaging together, no momentum exits), so they simply accumulate unless you add a stop.",
    url: "https://www.investopedia.com/terms/d/dollarcostaveraging.asp",
  },
  rank_by: {
    term: "Top-N ranking",
    explain:
      "For a basket universe the live engine snapshots every member, ranks them by the metric you choose, and considers only the top N as buy candidates (your entry rules still apply). Metrics: today's % move, 30-day return, or relative strength (how far price sits above/below its 200-day average). A backtest can't do this — it tests the whole basket over history because the historical daily ranking can't be reconstructed. Top-N is a LIVE feature only.",
    url: "https://www.investopedia.com/terms/r/relativestrength.asp",
  },
  rotate_on_rank_dropout: {
    term: "Rotate on rank drop-out",
    explain:
      "For a basket strategy, sell a holding the moment it falls out of the current top-N ranking — the essence of a rotation strategy: always hold the strongest few, rotating into new leaders as the ranking shifts. It's a LIVE feature (the engine re-ranks each cycle); a backtest can't reconstruct the historical daily ranking. Pair it with a wide stop-loss as a safety net and the rotation does the rest.",
    url: "https://www.investopedia.com/terms/s/sectorrotation.asp",
  },
  trade_rate: {
    term: "Trade-rate limit",
    explain:
      "A self-imposed cap on how many new positions the bot may open per day, across all strategies. Overtrading — trading too often, paying the spread each time — is one of the most reliable ways retail traders lose.",
    url: "https://www.investopedia.com/terms/o/overtrading.asp",
  },
  min_day_gain: {
    term: "Min gain today (%)",
    explain:
      "How far a stock must be up versus yesterday's close to qualify — on the scanner, to appear on the list; on a strategy, to be eligible to enter. It's the core momentum trigger. Higher = only strong movers (fewer, more extended); lower = more candidates, including weak ones.",
    url: "https://www.investopedia.com/terms/m/momentum.asp",
  },
  entry_window: {
    term: "Entry window (ET)",
    explain:
      "Only OPEN new positions between these US-Eastern times; outside the window entries are skipped (exits and stops always still work). Use it to avoid the chaotic first minutes after the 09:30 open, or to stop opening trades late in the day. Turn it off to allow entries any time the market is open.",
    url: "https://www.investopedia.com/terms/t/tradinghours.asp",
  },
  max_holding: {
    term: "Max holding time (hours)",
    explain:
      "Force an exit once a position has been held this many hours, whatever the price — a time stop. Caps how long capital sits in one trade and bounds overnight/weekend exposure. 0 = off (hold until another exit rule fires).",
    url: "https://www.investopedia.com/terms/h/holdingperiod.asp",
  },
  sizing: {
    term: "$ per trade",
    explain:
      "Dollars committed to each new position. For stocks it's rounded down to whole shares, so the real amount is a little less. Bigger = fewer positions fit the sleeve and each trade moves the account more; smaller = more diversification but small wins barely register. Must be ≤ the sleeve budget.",
    url: "https://www.investopedia.com/terms/p/positionsizing.asp",
  },
  max_positions: {
    term: "Max positions",
    explain:
      "The most positions open at once. On a strategy this caps that strategy; the account-wide rail in Settings caps everything across strategies — the binding limit is whichever is smaller, and sleeve ÷ $-per-trade can cap it lower still. More = more diversification but more simultaneous risk.",
    url: "https://www.investopedia.com/terms/d/diversification.asp",
  },
  max_exposure: {
    term: "Max total exposure ($)",
    explain:
      "A hard ceiling on the total dollars invested across all open positions at once. QT also never lets total exposure exceed your account's cash (the no-leverage rail), so this only tightens things further. Lower = more cash held in reserve, smaller drawdowns.",
    url: "https://www.investopedia.com/terms/m/marketexposure.asp",
  },
  cooldown: {
    term: "Cooldown after a loss (hours)",
    explain:
      "After a losing exit in a symbol, don't re-buy that same symbol for this many hours. Stops the bot from immediately piling back into a name that just stopped it out and churning spread on a falling knife. 0 = no cooldown.",
    url: "https://www.investopedia.com/terms/t/trading-psychology.asp",
  },
  universe: {
    term: "Universe",
    explain:
      "Where a strategy's buy candidates come from: Scanner (today's risers), a Basket (your curated list, ranked top-N), Watchlist (your pinned symbols), or a Custom fixed list. The strategy's entry rules and the safety rails then decide what actually trades from that pool.",
    url: "https://www.investopedia.com/terms/s/stockscreener.asp",
  },
  starting_cash: {
    term: "Starting cash ($)",
    explain:
      "The simulated account the backtest begins with. It defaults to the selected strategy's SLEEVE (the most that one strategy is ever allowed to deploy) — a single-strategy backtest can never put more than its sleeve to work, so a bigger account would just sit idle and dilute the account-% return. Give it room: several times your $-per-trade, or the account can't hold multiple positions and one early loss can lock it out of further trades. When unsure, read 'return on money used', not account %.",
    url: "https://www.investopedia.com/terms/b/backtesting.asp",
  },
  spread_cost: {
    term: "Spread cost per side (%)",
    explain:
      "Models the bid-ask spread you pay on entry AND exit — subtracted from every fill, each side. Thin small-caps have wide spreads, so set this honestly (0.2–0.5%+); too low flatters the result. It does NOT model missed fills, gaps, or slippage beyond the spread — real fills can be worse.",
    url: "https://www.investopedia.com/terms/b/bid-askspread.asp",
  },
  history_days: {
    term: "History (days)",
    explain:
      "How far back the backtest replays. Longer covers more trades and market conditions (more reliable) but needs more cached data. A backtest is path-dependent, so the same strategy can show different trades over 50 vs 100 days — judge by per-trade stats across windows, not a single window's total return.",
    url: "https://www.investopedia.com/terms/b/backtesting.asp",
  },
  scanner_price: {
    term: "Price floor / cap",
    explain:
      "Only scan symbols in this per-share price range. The $ floor keeps illiquid penny/OTC pumps (hard to exit) off the list; a cap (0 = none) lets you focus on cheaper movers. Applied before ranking, so it shapes the whole shortlist.",
    url: "https://www.investopedia.com/terms/p/pennystock.asp",
  },
  scanner_rows: {
    term: "Rows per list",
    explain:
      "How many top movers to show per market (stocks and crypto each). It's also the count the live engine considers as candidates when a strategy's universe is the scanner. More = a longer shortlist that reaches weaker movers.",
    url: "https://www.investopedia.com/terms/r/relativestrength.asp",
  },
  scanner_exclude: {
    term: "Never trade these",
    explain:
      "Symbols the scanner always drops and the engine never buys, in both markets. Use it to permanently avoid names you don't want the bot touching — e.g. a stock you already hold elsewhere, or one that keeps whipsawing you.",
    url: "https://www.investopedia.com/terms/e/exclusion.asp",
  },
  overfitting: {
    term: "Overfitting",
    explain:
      "Tuning a strategy's settings so tightly to past prices that it fits the noise, not the signal — it looks brilliant on the history you tested and falls apart on anything new. The classic trap of any parameter search. The defences: judge a config on data it never saw (out-of-sample), prefer settings whose neighbours also do well (a plateau, not a lone spike), and count how many combinations you tried — one winner out of thousands is often just luck.",
    url: "https://www.investopedia.com/terms/o/overfitting.asp",
  },
  parameter_search: {
    term: "Parameter search",
    explain:
      "Systematically trying many settings for a strategy (min gain, stops, take-profit) through the SAME backtester, instead of guessing one by hand — this is a search, not 'AI'. QT searches on the first ~70% of the history and reports the result on the final ~30% it never optimized on, so a lucky in-sample fit gets caught. The output is a HYPOTHESIS: an editable draft strategy, born disabled, that still has to prove itself in shadow then paper. It never enables anything for you.",
    url: "https://www.investopedia.com/terms/b/backtesting.asp",
  },
};
