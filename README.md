<p align="center">
  <img src="frontend/public/icon.png" width="96" height="96" alt="QT Auto-Trader logo" />
</p>

# QT Auto-Trader

Self-hosted momentum trading bot for US stocks and crypto, built on the official
[Alpaca](https://alpaca.markets) API. It scans for what's rising today, buys into
momentum, and sells when a configurable downturn is detected — with hard safety
rails (daily loss kill switch, Pattern-Day-Trader guard, wash-sale awareness)
built in from the start.

**Paper-first by design:** the bot trades simulated money until you deliberately
graduate it, phase by phase, to real trading with human approval.

> ⚠️ Nothing here is financial advice. Automated trading can lose money quickly.
> You are responsible for anything this software does with your accounts.

## Status

**Phase 2 — paper-trading engine (in progress).** Scanner and watchlist are
live; Google Sign-In, strategies, shadow mode, and paper execution are landing
now. No real-money trading exists yet.

Roadmap: ~~0) skeleton~~ → ~~1) market scanner~~ → **2) paper-trading engine**
→ 2.5) minimal backtester → 3) reliability hardening → 4) full backtesting →
5) graduated live trading → 6) multi-user/sharing.

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
