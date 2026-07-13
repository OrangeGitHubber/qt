# What we've done — plain-English changelog

Newest first. Each phase links to the technical details in
[how-it-works.md](how-it-works.md) and the reasoning in [decisions.md](decisions.md).

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
