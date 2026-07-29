// Every configurable market term gets a plain-English explanation and a link
// to a reliable source — the UI must teach as it configures.
//
// Style: `explain` is rendered as plain text, so reserve capitals for genuine
// acronyms and proper nouns (MACD, VWAP, ATR, RSI, SPY, IEX, ET…) — never for
// emphasis. Keep it consistent across every entry.

export interface GlossaryEntry {
  term: string;
  explain: string;
  url: string;
}

export type InfoKey = keyof typeof GLOSSARY;

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
  macd: {
    term: "MACD",
    explain:
      "Moving Average Convergence Divergence — a momentum gauge built from two moving averages of price. The MACD line is the fast average minus the slow one (default 12 vs 26 days); the signal line is a smoothed version of that (default 9 days); the histogram is the gap between them. When the line crosses above its signal it's a bullish signal (momentum turning up); crossing below is bearish. QT computes it from completed daily bars only — never today's unfinished bar — and leaves it off by default: turn it on to only enter while momentum is bullish, and/or to exit when it turns bearish. Best for swing / daily strategies — it avoids buying into fading momentum. Leave it off for fast intraday trades.",
    url: "https://www.investopedia.com/terms/m/macd.asp",
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
      "Backtests against the market's actual top risers on each past day — the names the live scanner would have surfaced — instead of a fixed symbol list. Risers are reconstructed from a cached year of daily bars, ranked by each day's intraday peak gain (so pump-and-fade names aren't missed). Run a daily sweep in Settings first; add an intraday sweep to replay on 15-minute bars, which lets intraday exits (flatten-before-close, VWAP, entry window) behave for real. Each day only that day's top-N are eligible; your entry rules then decide. The closest a backtest gets to the live 'today's risers' engine.",
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
      "Quantity traded × price. Note: this is only what Alpaca's free feed saw, not the whole market. Stocks use the last completed trading session on IEX (~2–3% of US volume) — a stable full day, since today's bar is only partial while the market is open. Crypto is a rolling 24-hour total from Alpaca's aggregated feed. So a real BTC day is billions, but this column may show only a few thousand — treat it as a liquidity signal to compare like-for-like across symbols, not the true market total.",
    url: "https://www.investopedia.com/terms/v/volume.asp",
  },
  wash_sale: {
    term: "Wash sale",
    explain:
      "Selling a stock at a loss and re-buying it within 30 days disallows the tax deduction for that loss. QT can block or warn. The IRS counts your other accounts too — QT can only see its own trades.",
    url: "https://www.investopedia.com/terms/w/washsalerule.asp",
  },
  leverage: {
    term: "Leverage / margin",
    explain:
      "Trading with borrowed money. Since June 2026, accounts over $2k can trade with up to 4x intraday buying power — meaning losses are also 4x faster, and you can lose more than a position's worth in hours. QT keeps this off unless you unlock it at the server level and confirm the risk.",
    url: "https://www.investopedia.com/terms/m/margin.asp",
  },
  entry_slippage: {
    term: "Entry slippage (marketable buffer)",
    explain:
      "QT never sends a naked market order — it sends a 'marketable' limit priced a little through the market so it crosses the spread and fills. This is how far through, for buys: 0.5% = default. Higher fills more reliably on fast/thin names but at a worse price; 0% is a passive limit at the quote that may not fill. Live/paper only — the backtest models fills with its own spread-cost input.",
    url: "https://www.investopedia.com/terms/s/slippage.asp",
  },
  exit_slippage: {
    term: "Exit slippage + escalating chase",
    explain:
      "How far below the market QT prices the marketable sell limit when exiting (1% = default). If a sell misses the fill (price dropping faster than the order, or a thin book), QT cancels and retries next cycle. Set 'Max exit slippage' above the base to escalate: each miss widens the sell price one step further down, up to the max, so a fast drop still gets out — still a limit, never a market order. Equal base and max = no escalation. Wider = more certain exits, worse price. Live/paper only.",
    url: "https://www.investopedia.com/terms/s/slippage.asp",
  },
  swing_mode: {
    term: "Trading style: Swing vs Intraday",
    explain:
      "Two opposite styles, so it's one choice. Swing holds positions overnight and judges exits over days (soft exits like take-profit wait until the day after entry; stops still act same-day). Intraday flattens before the close and never holds overnight (for stocks; crypto has no close). Stocks default to Swing — spreads and free-data limits punish rapid stock trading, though the trade-off is overnight-gap risk. Crypto is the intraday lab (24/7, cleaner data).",
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
      "The engine runs its full decision loop and writes every would-be trade to the journal, but places no orders anywhere — not even simulated ones. The zero-risk first rung of the autonomy ladder.",
    url: "https://en.wikipedia.org/wiki/Shadow_system",
  },
  daily_loss_limit: {
    term: "Daily loss kill switch",
    explain:
      "If today's realized losses reach this limit (in dollars or % of account, whichever is lower), the bot stops opening new positions until tomorrow and alerts you.",
    url: "https://www.investopedia.com/terms/r/riskmanagement.asp",
  },
  bar: {
    term: "Bar (candle)",
    explain:
      "One slice of price history: the open, high, low, close and volume for a period. A '1 hour' bar summarises an hour of trading. Smaller bars = more detail and slower backtests; bigger bars = faster but coarser. Two years of hourly bars for one stock is ~3,500 bars.",
    url: "https://www.investopedia.com/terms/c/candlestick.asp",
  },
  atr: {
    term: "Average True Range (ATR)",
    explain:
      "The average size of a symbol's daily move, as a % of price — gaps between one day's close and the next included, not just the day's high-to-low range. It's the noise floor for your stops: a 2% stop on a stock that routinely swings 4% will shake you out of good trades for no reason. QT can use it two ways (both off by default): an ATR stop places the stop a multiple of ATR below entry, so volatile names get a wider stop and calm ones a tighter stop; ATR sizing sizes each position so a stop-out loses about the same dollars whatever the symbol's volatility.",
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
    url: "https://www.investopedia.com/terms/a/assetallocation.asp",
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
      "A named group of symbols you pick — a theme like Defense or Banking. It is not an authoritative sector database: Alpaca provides no sector or industry data on this plan, so the starter baskets are hand-picked and yours to edit, and they drift as companies change. A basket is just a convenient, editable list.",
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
      "The live engine snapshots the pool, ranks it by the metric you choose, and considers only the top N as buy candidates (your entry rules still apply). Works for a basket (always ranked) and — when you switch ranking on — a watchlist or a custom list. Metrics: today's % move, 30-day return, relative strength (price vs its own 200-day average), or relative strength vs the S&P 500. A backtest can't do this — it tests the whole pool over history because the historical daily ranking can't be reconstructed. Top-N is a live feature only.",
    url: "https://www.investopedia.com/terms/r/relativestrength.asp",
  },
  rank_enabled: {
    term: "Rank & take top N",
    explain:
      "For a watchlist or a custom list, turn this on to trade only the strongest few names instead of the whole list: each cycle the engine ranks the pool by your chosen metric and keeps the top N as candidates (your entry rules still decide from there). Off = consider the whole list, entry rules alone deciding. Baskets are always ranked, so this is implicit there. It's a live feature — a backtest can't reconstruct the historical daily ranking, so it tests the whole pool. Pair it with 'rotate out when it leaves the top N' to rotate into and out of names as their strengths change.",
    url: "https://www.investopedia.com/terms/r/relativestrength.asp",
  },
  rs_vs_spy: {
    term: "Relative strength vs the market",
    explain:
      "Ranks each basket member by how much it has out-performed the S&P 500 (SPY) over a ~90-day window — the member's return minus SPY's return over the same span. This is the classic sector-rotation 'relative strength', and it differs from 'Relative strength (vs 200-day average)': a name can sit above its own 200-day trend yet still lag the market. Positive = beating SPY; negative = trailing it. Stocks only (SPY is a stock benchmark). Like all top-N ranking it's a live feature — a backtest can't reconstruct the historical daily basket ranking.",
    url: "https://www.investopedia.com/terms/r/relativestrength.asp",
  },
  rotate_on_rank_dropout: {
    term: "Rotate on rank drop-out",
    explain:
      "For a ranked strategy (a basket, or a watchlist/custom list with 'Rank & take top N' on), sell a holding the moment it falls out of the current top-N ranking — the essence of a rotation strategy: always hold the strongest few, rotating into new leaders as the ranking shifts. It's a live feature (the engine re-ranks each cycle); a backtest can't reconstruct the historical daily ranking. Pair it with a wide stop-loss as a safety net and the rotation does the rest.",
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
      "Restricts buying only, on purpose, and only within regular US market hours. New positions are only opened between these US-Eastern times; outside the window entries are skipped. Selling is never narrowed by this window — exits, trailing stops and stop-losses fire whenever they're triggered during market hours, because you must always be able to get out (a window that could block a stop-out would be dangerous). That's why it's an 'entry' window, not a 'trading' window. Note for stocks: QT trades the regular session only, so neither buys nor sells happen in pre-/after-hours anyway — this window just narrows the open further (e.g. skip the chaotic first minutes after 09:30, or stop opening late in the day). Crypto trades 24/7, so ET hours mean little there — usually leave this off for crypto.",
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
      "Dollars committed to each new position. For stocks it's rounded down to whole shares, so the real amount is a little less (unless market + fractional trading is on). Bigger = fewer positions fit the sleeve and each trade moves the account more; smaller = more diversification but small wins barely register. Must be ≤ the sleeve budget.",
    url: "https://www.investopedia.com/terms/p/positionsizing.asp",
  },
  market_fractional: {
    term: "Market orders + fractional shares",
    explain:
      "Off by default, this strategy uses price-protected marketable limit orders and buys whole shares — so a small $ per trade can't buy an expensive name (e.g. $200 buys 0 whole shares of a $700 stock, and the buy is skipped). Turn it on and it instead sends plain market orders sized by dollar amount, so that $200 buys a fractional slice (~0.28 shares) and fills immediately. The trade-off: a market order takes whatever price is available, with no limit to protect you on a fast or thin move, so the fill can be worse than expected. Best for liquid, higher-priced names you'd otherwise miss; leave it off for thin small-caps where the spread can bite. It also makes crypto fills immediate, which avoids the orphan positions a slow limit fill can leave behind.",
    url: "https://www.investopedia.com/terms/f/fractionalshare.asp",
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
      "Where a strategy's buy candidates come from: Scanner (today's risers), a Basket (your curated list, ranked top-N), Watchlist (your pinned symbols), or a Custom fixed list. The strategy's entry rules and the safety rails then decide what actually trades from that pool. A strategy trades one asset class, so its universe is scoped to that class — a stocks strategy never sees crypto, and vice-versa.",
    url: "https://www.investopedia.com/terms/s/stockscreener.asp",
  },
  custom_symbols: {
    term: "Specific symbols",
    explain:
      "The engine considers exactly the symbols you list each cycle — your entry and exit rules still apply. Good for a focused, one-off strategy (e.g. just SPCX) without building a whole basket. Only the current asset class is searchable here, because a strategy trades one asset class.",
    url: "https://www.investopedia.com/terms/s/stock.asp",
  },
  atr_stop: {
    term: "ATR stop (× ATR)",
    explain:
      "Sets the stop at a multiple of this symbol's Average True Range — its typical daily move — instead of a fixed %. A volatile stock gets a wider stop, a calm one a tighter stop, so ordinary daily wiggle doesn't shake you out. It's recomputed each bar, so it breathes with the symbol's volatility. 0 = off (use the fixed stop-loss above).",
    url: "https://www.investopedia.com/terms/a/atr.asp",
  },
  atr_risk: {
    term: "Risk $ per trade (ATR sizing)",
    explain:
      "Sizes each position so a stop-out loses about this many dollars, no matter how volatile the stock is — a wild stock gets a smaller position, a calm one a larger position, for the same risk. Needs the ATR stop turned on. 0 = off (use the fixed $ per trade).",
    url: "https://www.investopedia.com/terms/p/positionsizing.asp",
  },
  atr_period: {
    term: "ATR period (days)",
    explain:
      "How many completed daily bars the Average True Range averages over. 14 is standard. Computed from completed daily bars only — never today's in-progress bar — so it's look-ahead-safe, and the stop is recomputed each bar, breathing with the symbol's volatility.",
    url: "https://www.investopedia.com/terms/a/atr.asp",
  },
  order_fills: {
    term: "Order fills (slippage)",
    explain:
      "How aggressively QT prices its marketable limit orders. Defaults match the built-in behaviour; widen them if exits or entries miss fills on fast, thin movers. These affect live/paper orders only — the backtest uses its own spread-cost setting and assumes fills.",
    url: "https://www.investopedia.com/terms/s/slippage.asp",
  },
  starting_cash: {
    term: "Starting cash ($)",
    explain:
      "The simulated account the backtest begins with. It defaults to the selected strategy's sleeve (the most that one strategy is ever allowed to deploy) — a single-strategy backtest can never put more than its sleeve to work, so a bigger account would just sit idle and dilute the account-% return. Give it room: several times your $-per-trade, or the account can't hold multiple positions and one early loss can lock it out of further trades. When unsure, read 'return on money used', not account %.",
    url: "https://www.investopedia.com/terms/b/backtesting.asp",
  },
  spread_cost: {
    term: "Spread cost per side (%)",
    explain:
      "Models the bid-ask spread you pay on entry and exit — subtracted from every fill, each side. Thin small-caps have wide spreads, so set this honestly (0.2–0.5%+); too low flatters the result. It does not model missed fills, gaps, or slippage beyond the spread — real fills can be worse.",
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
    url: "https://www.investopedia.com/terms/s/stockscreener.asp",
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
      "Systematically trying many settings for a strategy (min gain, stops, take-profit) through the same backtester, instead of guessing one by hand — this is a search, not 'AI'. QT searches on the first ~70% of the history and reports the result on the final ~30% it never optimized on, so a lucky in-sample fit gets caught. The output is a hypothesis: an editable draft strategy, born disabled, that still has to prove itself in shadow then paper. It never enables anything for you.",
    url: "https://www.investopedia.com/terms/b/backtesting.asp",
  },

  // ---- Symbol-detail chart overlays (display-only) ----
  chart_markers: {
    term: "Buy / sell markers",
    explain:
      "The green ▲ (buys) and red ▼ (sells) come from this symbol's trade journal, placed on the day each trade happened — hover for the price, reason and P&L. They let you see where the strategy actually entered and exited against the price action: did it buy strength and sell into weakness, or the other way around? Nothing to interpret numerically — they're a record of what the bot did here.",
    url: "https://www.investopedia.com/terms/t/trade.asp",
  },
  ma_overlay: {
    term: "50 & 200-day moving averages",
    explain:
      "The average closing price over the last 50 days (gold) and 200 days (cyan), redrawn each day. They smooth out the noise to show the trend: price above a rising average is an uptrend, below a falling one is a downtrend. Traders watch the cross — the 50-day rising above the 200-day is the bullish 'golden cross', dropping below is the bearish 'death cross'. The 200-day is the slow, big-picture line; the 50-day turns sooner.",
    url: "https://www.investopedia.com/terms/m/movingaverage.asp",
  },
  ema_overlay: {
    term: "EMA 9 & 21",
    explain:
      "Exponential moving averages that weight recent prices more, so they react faster than the 50/200-day lines — this is short-term trend and momentum. Price above a rising 9 and 21 EMA, with the 9 above the 21, is short-term bullish; the 9 crossing below the 21 is an early warning that momentum is fading. Use them for timing within a trend, not the big-picture direction.",
    url: "https://www.investopedia.com/terms/e/ema.asp",
  },
  bollinger_overlay: {
    term: "Bollinger Bands",
    explain:
      "A 20-day average with an upper and lower band set 2 standard deviations away, so the bands widen when volatility rises and pinch in when it falls. Price riding the upper band = stretched/strong; hugging the lower band = weak or oversold; a 'squeeze' (very narrow bands) often comes before a big move. It frames how far price has stretched from normal — not a buy/sell signal on its own.",
    url: "https://www.investopedia.com/terms/b/bollingerbands.asp",
  },
  atr_stop_line: {
    term: "ATR-stop level",
    explain:
      "An illustrative trailing stop drawn at close − 2×ATR (ATR = the symbol's typical daily move). It shows how far a volatility-based stop would sit below price and how it 'breathes' — wider when the stock is volatile, tighter when calm — so ordinary wiggle doesn't shake you out. It is not a specific position's live stop, just a visual for where a sensible stop might ride.",
    url: "https://www.investopedia.com/terms/a/atr.asp",
  },
  volume_overlay: {
    term: "Volume",
    explain:
      "Shares (or contracts) traded each day — green when the day closed up, red when down. It's the conviction behind a move: a breakout on high volume is far more trustworthy than one on light volume, and a rally on fading volume is suspect. Note for stocks: the free IEX feed sees only a slice of true volume, so read it for relative comparison (this bar vs recent bars), not absolute size.",
    url: "https://www.investopedia.com/terms/v/volume.asp",
  },
  rsi: {
    term: "RSI (Relative Strength Index)",
    explain:
      "A 0–100 momentum oscillator over 14 days. Above 70 = overbought (stretched, prone to pull back); below 30 = oversold; the 50 line is the momentum midline — above 50 is bullish, below is bearish. The shaded 50–70 band (green) is the sweet spot: healthy uptrend momentum that isn't yet overbought. Above 70 momentum is strong but chasing it is risky; that's why 'good' here is the band, not simply 'as high as possible'.",
    url: "https://www.investopedia.com/terms/r/rsi.asp",
  },
  rsi_entry: {
    term: "RSI entry band",
    explain:
      "Only buy when the symbol's 14-day RSI sits inside this band (0 on a bound = that side is off). Max RSI is the useful one for chasing risers: set it to, say, 70 to skip names that are already overbought and prone to pull back, so you enter strength that still has room rather than buying the top. Min RSI sets a floor if you only want names with real momentum behind them. RSI is measured on completed daily closes, so it's a swing-timeframe filter, not an intraday one.",
    url: "https://www.investopedia.com/terms/r/rsi.asp",
  },
  rsi_exit: {
    term: "Sell when RSI is overbought",
    explain:
      "Sell the position once its 14-day RSI rises to or above this level (0 = off). It's a take-profit on froth: an RSI of 70–80+ means the move is stretched and often about to cool, so this books the gain into strength instead of waiting for a stop. Pairs naturally with ranking a basket by RSI (you rotate into the strongest names, then step out as each gets overextended). Like the entry band it's computed on completed daily closes, so it fires at most once per day, not on every intraday wiggle.",
    url: "https://www.investopedia.com/terms/r/rsi.asp",
  },
  rs_ratio: {
    term: "Relative strength vs SPY",
    explain:
      "This symbol's price divided by the S&P 500's (SPY), rebased to 1.0 at the start of the window. Above 1.0 (shaded green) means it has out-performed the market since then; a rising line means it's leading right now. Below 1.0 or falling means it's lagging. It answers 'is this a leader or a laggard?' independent of whether the whole market went up or down — the good zone is above 1.0 and rising.",
    url: "https://www.investopedia.com/terms/r/relativestrength.asp",
  },
};
