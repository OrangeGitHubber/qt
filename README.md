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

## License

GPLv3 — see [LICENSE](LICENSE).
