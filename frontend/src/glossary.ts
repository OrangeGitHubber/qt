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
  regime_filter: {
    term: "Regime filter",
    explain:
      "Only buy stocks while the S&P 500 is above its 200-day moving average — a common definition of a rising market. When it's below, QT stops opening stock positions (exits still work).",
    url: "https://www.investopedia.com/terms/m/movingaverage.asp",
  },
  dollar_volume: {
    term: "Dollar volume",
    explain:
      "Shares traded × price: how much money changed hands today. Low dollar volume means it may be hard to sell without moving the price. Note: free IEX data sees only a slice of total volume.",
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
  swing_mode: {
    term: "Swing mode",
    explain:
      "Hold positions overnight and judge exits over days, instead of scalping intraday. Default for stocks: bid-ask spreads and free-data limitations punish rapid stock trading; the downside is overnight gaps.",
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
  trade_rate: {
    term: "Trade-rate limit",
    explain:
      "A self-imposed cap on how many new positions the bot may open per day, across all strategies. Overtrading — trading too often, paying the spread each time — is one of the most reliable ways retail traders lose.",
    url: "https://www.investopedia.com/terms/o/overtrading.asp",
  },
};
