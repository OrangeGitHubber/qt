# The bar cache

QT keeps a **bar cache**: bulk, rebuildable historical market data — daily
[bars](https://www.investopedia.com/terms/o/ohlcchart.asp) plus computed
"movers" — used to reconstruct what the scanner would have surfaced on past
days. It is deliberately kept **separate from the main `qt.db`**, which holds
your encrypted Alpaca keys, config, and trade journal. The cache is public
market data only: no keys, no config, no journal.

By default the cache is a local SQLite file and needs zero setup. Pointing it
at Postgres is an **optional** upgrade for a durable, shareable cache — the app
is fully functional without it.

## Why it exists

The cache is the foundation for an upcoming backtesting feature: replaying a
strategy against "the day's risers" the scanner would have surfaced
historically. That needs a place to cache a broad sweep of daily bars, which is
what the bar cache provides. The setup below is available now; the
scanner-replay backtest is upcoming.

## Choosing a backend: `QT_BAR_CACHE_URL`

The cache backend is chosen by a single environment variable,
`QT_BAR_CACHE_URL` (a [SQLAlchemy](https://www.sqlalchemy.org/) connection URL):

- **Unset (default)** — a local `bars.db` SQLite file in the container's
  `/data`. Per-instance, simplest, zero setup. This is the default, and the app
  works fully this way.
- **Set to a Postgres DSN** — `postgresql://<user>:<password>@<host>:5432/<database>`
  — a durable, shared cache that survives container recreation and can be shared
  across instances.

You only ever need Postgres if you want durability across container recreation
or want to share one download between instances. Otherwise leave it unset.

## Setting up Postgres (optional)

### 1. Pick the right host — it is NOT `localhost`

From inside the QT container, `localhost` means the QT container itself, **not**
your Postgres. Use one of:

- Your server's **LAN IP** and the Postgres port, e.g. `192.168.1.50:5432`.
- If QT and Postgres containers share a custom Docker network, the **Postgres
  container's name** as the host.

### 2. Create a dedicated, scoped DB user — not the superuser

Do not use the `postgres` superuser. In a DB tool (e.g. Adminer → SQL command),
create a scoped user and an empty database:

```sql
CREATE USER qt WITH PASSWORD 'a-strong-password';
CREATE DATABASE qt_bars OWNER qt;
```

Then, **connected to the `qt_bars` database**, give the owner the public schema.
Postgres 15+ locks down `public` by default, so without this the app's table
creation fails:

```sql
ALTER SCHEMA public OWNER TO qt;
```

The app creates the *tables* itself on first use — you only need the empty
database and the user.

### 3. URL-encode special characters in the password

If the password contains characters that are special in a URL, encode them:

| Character | Encoded |
|---|---|
| `@` | `%40` |
| `#` | `%23` |
| `/` | `%2F` |

For example, a password of `p@ss/word` becomes `p%40ss%2Fword` in the DSN.

### 4. Set it on unraid

On the QT container: **Add another Path, Port, Variable, Label or Device** →
Variable, Key `QT_BAR_CACHE_URL`, Value the DSN, then **Apply**. Example value:

```
postgresql://qt:a-strong-password@192.168.1.50:5432/qt_bars
```

## Security & sharing

The cache holds **only public market data** — daily bars and computed movers.
Your API keys, config, and trade journal live in each instance's own `qt.db`
and are never written to the cache and never shared. That makes sharing one
Postgres across instances both safe and optional.

- **Same LAN (e.g. sharing QT with family):** a second person can point their
  container at the same Postgres, using the same scoped user, and share one
  download of the historical data.
- **Different network:** just leave `QT_BAR_CACHE_URL` **unset** so each
  instance uses its own local SQLite cache. Do not expose Postgres to the
  internet for this.

Always use a scoped user, never the `postgres` superuser.
