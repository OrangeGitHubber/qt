<p align="center">
  <img src="frontend/public/icon.png" width="96" height="96" alt="QT Auto-Trader logo" />
</p>

# QT Auto-Trader

Self-hosted momentum trading bot for US stocks and crypto, built on the official
[Alpaca](https://alpaca.markets) API. It scans for what's rising today, buys into
momentum, and sells when a configurable downturn is detected — with hard safety
rails (daily-loss kill switch, exposure capped at your equity so it can never
use margin, a trade-rate brake, wash-sale awareness) built in from the start.

**Paper-first by design:** the bot trades simulated money until you deliberately
graduate it, phase by phase, to real trading with human approval.

> ⚠️ Nothing here is financial advice. Automated trading can lose money quickly.
> You are responsible for anything this software does with your accounts.

## Status

**Paper trading, fully working — no real-money trading exists yet.** What's
live today: Google Sign-In gating the whole app; the movers scanner and a
watchlist with indicator columns (RSI, MACD, ATR, vs 200-day); curated symbol
baskets with top-N ranking; strategies built from presets in the UI (momentum,
rotation, MACD/RSI/VWAP/ATR rules) with config versioning; a shadow → paper
engine with every risk rail enforced and Slack alerts; a decision journal; a
backtester (single / compare / portfolio modes, plus "scanner replay" against
each day's reconstructed top risers) backed by a local bar cache; a parameter
optimizer with out-of-sample validation; and a basket sweep that ranks every
basket's best config by its out-of-sample margin over SPY.

Roadmap: ~~0) skeleton~~ → ~~1) market scanner~~ → ~~2) paper-trading engine~~
→ ~~2.5) minimal backtester~~ → **3) reliability hardening (in progress)** →
~~3.5) baskets~~ → ~~4) backtesting & evaluation~~ → 5) graduated live trading
→ 6) multi-user/sharing.

## Documentation

- [What we've done](docs/CHANGELOG.md) — plain-English changelog
- [How it works](docs/how-it-works.md) — the product, the strategies, the safety rails (all market terms linked to explainers)
- [Decision log](docs/decisions.md) — why it's built this way

## Run on unraid (or any Docker host)

```bash
docker run -d --name qt-autotrader \
  -p 8420:8420 \
  -v /mnt/user/appdata/qt-autotrader:/data \
  --restart unless-stopped \
  ghcr.io/orangegithubber/qt:latest
```

On unraid, add the template from `unraid/qt-autotrader.xml`, or create the
container manually with the same port/volume mapping. Then open
`http://YOUR-SERVER:8420` and follow the setup wizard — everything is configured
in the UI, no config files.

The `/data` volume holds the database, encrypted API keys, and trade history.
Back it up.

## Getting started (first run, step by step)

The app walks you through all of this on-screen too — these are the same steps.

1. **Secure it with Google Sign-In.** On first visit QT asks for a Google OAuth
   client. In [Google Cloud Console](https://console.cloud.google.com/apis/credentials):
   *Create credentials → OAuth client ID → Web application* (if prompted first,
   configure the consent screen: user type *External*, add your own email as a
   test user). Under *Authorized redirect URIs* add **exactly** the URI the QT
   screen shows you (`http://YOUR-SERVER:8420/api/auth/callback`). Paste the
   client ID + secret into QT; the Google account you name becomes the owner,
   and you can allow more emails later in **Settings → Who can sign in**.
2. **Connect Alpaca (paper).** Create a free account at
   [alpaca.markets](https://alpaca.markets) — paper-only needs just an email.
   In the Alpaca dashboard flip the toggle to **Paper**, generate an API key
   pair, and paste it into QT's wizard. Keys are verified against Alpaca and
   stored encrypted on your server.
3. **Let it load symbols.** QT mirrors Alpaca's tradable-symbol directory so
   search boxes autocomplete; it syncs automatically, or on demand in
   **Settings → Symbol directory**.
4. **Pick a universe.** Add a few names to the **Watchlist** (one search box,
   stocks and crypto together) or browse the curated **Baskets** (sector/theme
   lists you can edit).
5. **Create your first strategy** on the Strategies tab from a preset. It's
   created **disabled** — nothing trades yet.
6. **Backtest it** (Backtest tab): the test runs on the strategy's own universe
   and sleeve over past prices, shows the equity curve vs buy-and-hold vs SPY,
   every trade with its reason, and what was held each day. Drag on the chart
   to zoom into a busy stretch.
7. **Optionally optimize** — each strategy row has an *Optimize* action that
   searches better settings and judges them only on data the search never saw
   (out-of-sample). The *basket sweep* on the Optimizer tab runs that search
   across every basket and ranks the winners against SPY.
8. **Turn it on gently.** Enable the strategy and start the engine in
   **shadow mode** (Dashboard): the bot journals every trade it *would* make,
   placing no orders. When you're comfortable, switch to **paper** — simulated
   money, real prices, all risk rails live. Slack alerts are optional in
   **Settings → Slack notifications**.
9. **Judge it honestly.** The Dashboard scoreboard tracks the bot against
   simply holding SPY from day one — that's the number that decides whether it
   ever earns real money (live trading is a later phase, behind additional
   safeguards).

### Optional: shared bar cache (Postgres)

QT caches historical market data (daily bars and computed movers) in a **bar
cache** kept separate from your keys and journal. By default this is a local
SQLite file in `/data` — zero setup, fully functional. Optionally, set the
`QT_BAR_CACHE_URL` environment variable to a Postgres DSN for a durable cache
that survives container recreation and can be shared across instances (it holds
only public market data). See [docs/bar-cache.md](docs/bar-cache.md) for setup.

### ⚠ Get the volume direction right — `host path : /data`

In the `-v host:container` flag (and the unraid "Data" field), the **left**
side is a folder on your server and the **right** side must be exactly `/data`
inside the container:

```
-v /mnt/user/appdata/qt-autotrader : /data
   └── host path (yours) ──────────┘   └ container path (always /data)
```

Inverting these (a real incident on unraid) makes the app write to a throwaway
location, so your config and keys vanish the next time the image updates. QT now
**detects this at startup**, shows a red banner, logs it, and Slack-alerts — but
the fix is to correct the mapping. Full explanation and recovery steps:
[docs/data-persistence.md](docs/data-persistence.md).

> **Do not auto-update the live container.** Tools like Watchtower pulling
> `:latest` can restart QT mid-trade or mid-migration — dangerous with real
> money. Pin a version tag (e.g. `:v0.3.0`) and update deliberately when the
> engine is idle. See [Releases & updating](#releases--updating).

## Develop

```bash
# backend
pip install ./backend[dev]
uvicorn qt.main:app --port 8420 --reload

# tests
pytest backend/tests

# frontend (dev server proxies /api to :8420)
cd frontend && npm install && npm run dev
```

## Releases & updating

QT publishes versioned image tags to GHCR (e.g. `ghcr.io/orangegithubber/qt:v0.3.0`)
alongside `:latest`.

**Pin a version tag for the live container. Do not auto-update it.** An
automatic updater such as [Watchtower](https://containrrr.dev/watchtower/)
watching `:latest` can recreate the container at any moment — including
mid-trade (between an order submit and its confirmation) or mid-migration.
QT is built to survive restarts (graceful shutdown, startup reconciliation),
but an *unattended surprise* restart while positions are open is a needless
risk with money on the line.

Recommended flow:

1. Pin the tag: `ghcr.io/orangegithubber/qt:v0.3.0` (not `:latest`).
2. Watch the [Releases](https://github.com/OrangeGitHubber/qt/releases) page.
3. Update deliberately — set the engine to **Off**, wait for open positions to
   flatten (or flatten them), then pull the new tag and recreate.

## License

GPLv3 — see [LICENSE](LICENSE).
