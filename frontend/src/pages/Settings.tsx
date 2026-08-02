import { FormEvent, useCallback, useEffect, useRef, useState } from "react";
import {
  addAllowlist,
  AssetStatus,
  BarCacheStats,
  BarCacheStatus,
  EngineState,
  getAllowlist,
  getAssetStatus,
  getBarCacheStatus,
  getEngine,
  getSlackPrefs,
  getStatus,
  liquidateAll,
  removeAllowlist,
  RiskConfig,
  saveAlpacaKeys,
  StatusResponse,
  runBarSweep,
  runBarReconstruct,
  runIntradaySweep,
  runCryptoSweep,
  runCryptoIntradaySweep,
  setRegimeEnabled,
  setRisk,
  setSlack,
  setSlackPrefs,
  SlackPrefs,
  syncAssets,
  testSlack,
} from "../api";
import InfoTip from "../components/InfoTip";
import NumberField from "../components/NumberField";
import { IconWarn } from "../components/icons";

// One asset class's cache totals, rendered identically for stocks and crypto so
// the two columns line up row-for-row. `stats` is null until that class has been
// swept (or transiently while a sweep runs and the API withholds totals).
function CacheMetrics({ stats, hasIntraday, unitPairs }: { stats: BarCacheStats | null; hasIntraday: boolean; unitPairs?: boolean }) {
  return (
    <dl className="cache-metrics">
      <dt>{unitPairs ? "Pairs cached" : "Symbols cached"}</dt>
      <dd>{stats ? stats.daily_symbols.toLocaleString() : "not swept yet"}</dd>
      <dt>Days of movers</dt>
      <dd>{stats ? stats.movers_days.toLocaleString() : "—"}</dd>
      <dt>Intraday bars</dt>
      <dd>
        {!stats
          ? "—"
          : hasIntraday
          ? `${stats.intraday_bars.toLocaleString()} — replay ready`
          : "none — daily only"}
      </dd>
      <dt>Data through</dt>
      <dd>{stats?.latest_day ?? "—"}</dd>
    </dl>
  );
}

export default function Settings() {
  const [engine, setEngine] = useState<EngineState | null>(null);
  const [risk, setRiskLocal] = useState<RiskConfig | null>(null);
  const [allow, setAllow] = useState<{ emails: string[]; owner: string } | null>(null);
  const [newEmail, setNewEmail] = useState("");
  const [slackUrl, setSlackUrl] = useState("");
  const [slackPrefs, setSlackPrefsState] = useState<SlackPrefs | null>(null);
  const [leverageConfirm, setLeverageConfirm] = useState("");
  const [assetStatus, setAssetStatus] = useState<AssetStatus | null>(null);
  const [syncing, setSyncing] = useState(false);
  const [bars, setBars] = useState<BarCacheStatus | null>(null);
  const [showFresh, setShowFresh] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  // Broker connection: current account + replace-keys form + liquidation.
  const [status, setStatus] = useState<StatusResponse | null>(null);
  const [keyId, setKeyId] = useState("");
  const [keySecret, setKeySecret] = useState("");
  const [brokerBusy, setBrokerBusy] = useState(false);
  const [showKeyForm, setShowKeyForm] = useState(false);
  const [liqConfirm, setLiqConfirm] = useState("");
  const [liqBusy, setLiqBusy] = useState(false);
  const [liqOrphans, setLiqOrphans] = useState(false);

  // The status endpoint nulls the persisted cache totals while a sweep runs (to
  // avoid counting rows on every 3s poll). Retain the last-known totals so a run
  // on one asset class doesn't blank the other column's numbers.
  const lastStock = useRef<BarCacheStats | null>(null);
  const lastCrypto = useRef<BarCacheStats | null>(null);
  if (bars?.cache) lastStock.current = bars.cache;
  if (bars?.crypto_cache) lastCrypto.current = bars.crypto_cache;

  const refresh = useCallback(() => {
    getEngine().then((e) => {
      setEngine(e);
      setRiskLocal(e.risk);
    });
    getAllowlist().then(setAllow).catch(() => setAllow(null));
    getAssetStatus().then(setAssetStatus).catch(() => setAssetStatus(null));
    getBarCacheStatus().then(setBars).catch(() => setBars(null));
    getSlackPrefs().then(setSlackPrefsState).catch(() => setSlackPrefsState(null));
    getStatus().then(setStatus).catch(() => setStatus(null));
  }, []);

  useEffect(refresh, [refresh]);

  // Poll the sweep status while a sweep is running so progress updates live.
  useEffect(() => {
    if (!bars?.running) return;
    const t = setInterval(() => getBarCacheStatus().then(setBars).catch(() => {}), 3000);
    return () => clearInterval(t);
  }, [bars?.running]);

  async function replaceKeys(e: FormEvent) {
    e.preventDefault();
    setNote(null);
    setBrokerBusy(true);
    try {
      const res = await saveAlpacaKeys(keyId.trim(), keySecret.trim());
      setKeyId("");
      setKeySecret("");
      setShowKeyForm(false);
      await new Promise((r) => setTimeout(r, 150));
      refresh();
      setNote(`Connected to Alpaca account ${res.account_number} (${res.status}).`);
    } catch (err) {
      setNote((err as Error).message);
    } finally {
      setBrokerBusy(false);
    }
  }

  async function doLiquidate() {
    setNote(null);
    setLiqBusy(true);
    try {
      const res = await liquidateAll(liqOrphans);
      setLiqConfirm("");
      refresh();
      const tail = res.orphans_cleared.length
        ? ` (${res.orphans_cleared.length} were orphans QT didn't track)`
        : res.orphans_left.length
          ? `; left ${res.orphans_left.length} untracked position(s) alone`
          : "";
      setNote(
        `Liquidated: closed ${res.positions_closed} position(s), reconciled ${res.trades_reconciled} open trade(s)${tail}.`,
      );
    } catch (err) {
      setNote((err as Error).message);
    } finally {
      setLiqBusy(false);
    }
  }

  async function runSweep() {
    setNote(null);
    try {
      await runBarSweep();
      const s = await getBarCacheStatus();
      setBars(s); // flips running=true, which starts the poll above
    } catch (e) {
      setNote((e as Error).message);
    }
  }

  async function runReconstruct() {
    setNote(null);
    try {
      await runBarReconstruct();
      setBars(await getBarCacheStatus());
    } catch (e) {
      setNote((e as Error).message);
    }
  }

  async function runIntraday() {
    setNote(null);
    try {
      await runIntradaySweep();
      setBars(await getBarCacheStatus()); // flips running=true, starts the poll
    } catch (e) {
      setNote((e as Error).message);
    }
  }

  async function runCrypto() {
    setNote(null);
    try {
      await runCryptoSweep();
      setBars(await getBarCacheStatus()); // flips running=true, starts the poll
    } catch (e) {
      setNote((e as Error).message);
    }
  }

  async function runCryptoIntraday() {
    setNote(null);
    try {
      await runCryptoIntradaySweep();
      setBars(await getBarCacheStatus());
    } catch (e) {
      setNote((e as Error).message);
    }
  }

  function num(key: keyof RiskConfig) {
    return {
      value: (risk?.[key] as number) ?? 0,
      onChange: (n: number) => setRiskLocal((r) => (r ? { ...r, [key]: n } : r)),
    };
  }

  // The regime filter saves instantly on its own endpoint. Update ONLY the flag
  // (optimistically) and persist it — do NOT call refresh(), which would reload
  // the whole engine state and clobber any unsaved edits in the risk-rails form
  // below. Revert the checkbox if the save fails.
  async function toggleRegime(enabled: boolean) {
    setEngine((prev) => (prev ? { ...prev, regime_filter_enabled: enabled } : prev));
    setNote(null);
    try {
      await setRegimeEnabled(enabled);
    } catch (err) {
      setEngine((prev) => (prev ? { ...prev, regime_filter_enabled: !enabled } : prev));
      setNote((err as Error).message);
    }
  }

  // Slack message-category toggles: persist instantly, optimistic with revert.
  async function toggleSlackCat(key: string, enabled: boolean) {
    const flip = (want: boolean) =>
      setSlackPrefsState((p) =>
        p ? { ...p, categories: p.categories.map((c) => (c.key === key ? { ...c, enabled: want } : c)) } : p,
      );
    flip(enabled);
    setNote(null);
    try {
      await setSlackPrefs({ [key]: enabled });
    } catch (err) {
      flip(!enabled);
      setNote((err as Error).message);
    }
  }

  async function saveRisk(e: FormEvent) {
    e.preventDefault();
    if (!risk) return;
    setNote(null);
    try {
      await setRisk({ ...risk, leverage_confirm: leverageConfirm });
      setLeverageConfirm("");
      setNote("Risk settings saved.");
      refresh();
    } catch (err) {
      setNote((err as Error).message);
    }
  }

  if (!engine || !risk) return <div className="card">Loading…</div>;

  return (
    <>
      <div className="toolbar">
        <h2>Settings</h2>
      </div>
      {note && (
        <div className="card note" onClick={() => setNote(null)}>
          {note}
        </div>
      )}

      {/* Every settings card folds to its title (+/− on the right) so the page
          scans as a short list; open only what you're working on. */}
      <details className="card fold">
        <summary>
          <h3>Broker connection</h3>
        </summary>
        {status?.broker ? (
          <dl className="cache-metrics">
            <dt>Account</dt>
            <dd>
              {status.broker.account_number} · {status.broker.status}
            </dd>
            <dt>Equity</dt>
            <dd>${Number(status.broker.equity).toLocaleString(undefined, { maximumFractionDigits: 2 })}</dd>
            <dt>Cash</dt>
            <dd>${Number(status.broker.cash).toLocaleString(undefined, { maximumFractionDigits: 2 })}</dd>
            <dt>Mode</dt>
            <dd>Paper</dd>
          </dl>
        ) : (
          <p className="hint">
            {status?.alpaca_configured
              ? "Keys are saved, but the account couldn't be read right now."
              : "No Alpaca keys saved yet."}
          </p>
        )}

        {!showKeyForm ? (
          <button type="button" className="small" onClick={() => setShowKeyForm(true)}>
            Change / replace API keys
          </button>
        ) : (
          <form onSubmit={replaceKeys} style={{ marginTop: "0.5rem" }}>
            <p className="hint">
              Paste a different Alpaca <strong>paper</strong> (or live) key pair to point QT at another account — QT
              verifies them against Alpaca before saving. If you're <strong>switching accounts</strong>, pause the
              engine and liquidate first: your current open trades reference the old account.
            </p>
            <label>
              API key ID
              <input value={keyId} onChange={(e) => setKeyId(e.target.value)} autoComplete="off" required />
            </label>
            <label>
              API secret
              <input
                type="password"
                value={keySecret}
                onChange={(e) => setKeySecret(e.target.value)}
                autoComplete="off"
                required
              />
            </label>
            <div className="toolbar">
              <button disabled={brokerBusy}>{brokerBusy ? "Verifying…" : "Verify & save"}</button>
              <button
                type="button"
                className="small btn-ghost"
                onClick={() => {
                  setShowKeyForm(false);
                  setKeyId("");
                  setKeySecret("");
                }}
              >
                Cancel
              </button>
            </div>
          </form>
        )}

        {/* Destructive — folded shut by default even inside the expanded card. */}
        <details className="atr-section fold">
          <summary>
            <h5>Danger zone — liquidate holdings</h5>
          </summary>
          <p className="hint">
            Closes the positions QT holds (at market, by QT's own quantity), cancels its resting orders, and marks QT's
            open trades closed. Use it to start fresh, e.g. before switching accounts. It does <strong>not</strong> touch
            your cash or the account itself, but the position closes <strong>cannot be undone</strong>. Pause the engine
            first so it doesn't re-buy.
          </p>
          <label className="check">
            <input type="checkbox" checked={liqOrphans} onChange={(e) => setLiqOrphans(e.target.checked)} />
            Also close positions QT doesn't track (orphans)
          </label>
          <p className="hint warn">
            {liqOrphans ? (
              <>
                <IconWarn className="icon-inline" /> This will flatten <strong>every</strong> position on the account, including ones QT never opened. If
                another bot or you trade this same Alpaca account, <strong>its positions will be closed too.</strong>
              </>
            ) : (
              <>Off by default: untracked positions are left alone — they may belong to another bot on this account.</>
            )}
          </p>
          <label>
            Type <strong>LIQUIDATE</strong> to confirm
            <input
              value={liqConfirm}
              onChange={(e) => setLiqConfirm(e.target.value)}
              placeholder="LIQUIDATE"
              autoComplete="off"
            />
          </label>
          <button
            type="button"
            className="small danger"
            disabled={liqBusy || liqConfirm !== "LIQUIDATE"}
            onClick={doLiquidate}
          >
            {liqBusy ? "Liquidating…" : liqOrphans ? "Liquidate ALL holdings (incl. orphans)" : "Liquidate QT holdings"}
          </button>
        </details>
      </details>

      <details className="card fold">
        <summary>
          <h3>Risk rails (apply to every strategy, every mode)</h3>
        </summary>
        <form onSubmit={saveRisk}>
        <div className="filter-grid">
          <label>
            Max daily loss ($) <InfoTip k="daily_loss_limit" />
            <NumberField min={10} step="any" {...num("max_daily_loss_usd")} />
          </label>
          <label>
            Max daily loss (% of account) <InfoTip k="daily_loss_limit" />
            <NumberField min={0.5} step={0.5} {...num("max_daily_loss_pct")} />
          </label>
          <label>
            Max open positions (total) <InfoTip k="max_positions" />
            <NumberField min={1} step={1} {...num("max_total_positions")} />
          </label>
          <label>
            Max total exposure ($) <InfoTip k="max_exposure" />
            <NumberField min={10} step="any" {...num("max_total_exposure_usd")} />
          </label>
          <label>
            Max new trades per day <InfoTip k="trade_rate" />
            <NumberField min={1} step={1} {...num("max_trades_per_day")} />
          </label>
          <label>
            Cooldown after a loss (hours) <InfoTip k="cooldown" />
            <NumberField min={0} step="any" {...num("cooldown_hours_after_loss")} />
          </label>
          <label>
            Wash-sale guard <InfoTip k="wash_sale" />
            <select
              value={risk.wash_sale_guard}
              onChange={(e) => setRiskLocal({ ...risk, wash_sale_guard: e.target.value as RiskConfig["wash_sale_guard"] })}
            >
              <option value="block">Block re-buys (safest)</option>
              <option value="warn">Warn only</option>
              <option value="off">Off</option>
            </select>
          </label>
          <label className="check">
            <input
              type="checkbox"
              checked={engine.regime_filter_enabled}
              onChange={(e) => toggleRegime(e.target.checked)}
            />
            Regime filter <InfoTip k="regime_filter" />
          </label>
        </div>

        {engine.leverage.unlockable ? (
          <div className="danger-zone">
            <h4><IconWarn className="icon-inline" /> Leverage (unlocked at server level)</h4>
            <p className="hint">
              The container has <code>QT_ALLOW_LEVERAGE=true</code>, so this option is visible. Borrowed money
              multiplies losses as fast as gains — a 4x leveraged position losing 25% wipes out the entire stake.{" "}
              <InfoTip k="leverage" />
            </p>
            <label className="check">
              <input
                type="checkbox"
                checked={risk.leverage_enabled}
                onChange={(e) => setRiskLocal({ ...risk, leverage_enabled: e.target.checked })}
              />
              Allow the bot to exceed account equity (use margin)
            </label>
            {risk.leverage_enabled && !engine.leverage.enabled && (
              <label>
                Type <code>I ACCEPT AMPLIFIED LOSSES</code> to confirm
                <input value={leverageConfirm} onChange={(e) => setLeverageConfirm(e.target.value)} />
              </label>
            )}
          </div>
        ) : (
          <p className="hint">
            Leverage: <strong>locked</strong>. The bot can never invest more than the account's cash value. (To even
            see the option, set <code>QT_ALLOW_LEVERAGE=true</code> on the Docker container — deliberately a
            server-level act.) <InfoTip k="leverage" />
          </p>
        )}
          <button>Save risk settings</button>
        </form>
      </details>

      <details className="card fold">
        <summary>
          <h3>Symbol directory</h3>
        </summary>
        <p className="hint">
          A local copy of Alpaca's tradable symbols and company names, so search boxes autocomplete instantly without
          calling Alpaca on every keystroke. Refreshes automatically once a day.
        </p>
        {assetStatus && (
          <dl>
            <dt>Symbols</dt>
            <dd>
              {assetStatus.stocks.toLocaleString()} stocks · {assetStatus.crypto} crypto pairs{" "}
              {assetStatus.stale && <span className="pill warn">needs sync</span>}
            </dd>
            <dt>Updated</dt>
            <dd>{assetStatus.updated_at ? new Date(assetStatus.updated_at).toLocaleString() : "never"}</dd>
          </dl>
        )}
        <button
          className="small btn-ghost"
          disabled={syncing}
          onClick={() => {
            setSyncing(true);
            syncAssets()
              .then((s) => {
                setAssetStatus(s);
                setNote(`Symbol directory synced: ${s.stocks.toLocaleString()} stocks, ${s.crypto} crypto pairs.`);
              })
              .catch((e: Error) => setNote(e.message))
              .finally(() => setSyncing(false));
          }}
        >
          {syncing ? "Syncing…" : "Sync now"}
        </button>
      </details>

      <details className="card fold">
        <summary>
          <div className="cache-head">
            <h3>Historical bar cache</h3>
            <span className={`pill ${bars?.running ? "warn" : "muted"}`}>
              {bars?.running
                ? `${bars.market === "crypto" ? "crypto " : ""}${
                    { intraday: "intraday sweep", reconstruct: "re-ranking" }[bars.kind] ?? "daily sweep"
                  }…`
                : "idle"}
            </span>
          </div>
        </summary>
        <p className="hint">
          The data a <strong>"scanner replay" backtest</strong> reads, stored in{" "}
          {bars ? (
            <strong>
              {bars.backend.kind === "postgres"
                ? `Postgres${bars.backend.host ? ` (${bars.backend.host})` : ""}`
                : "a local SQLite file"}
            </strong>
          ) : (
            "…"
          )}
          . Stocks and crypto keep separate caches — sweep each on its own side below. Order per class:{" "}
          <strong>Run sweep → Sweep intraday → backtest</strong>. (See “How the sweep works” at the bottom.)
        </p>

        {bars?.running && (
          <div className="cache-progress">
            <span>
              {bars.kind === "reconstruct"
                ? `${bars.phase || "re-ranking"}${bars.batches_total ? ` — ${bars.batches_done.toLocaleString()} / ${bars.batches_total.toLocaleString()}` : "…"}`
                : bars.kind === "intraday"
                ? `${bars.symbols_saved.toLocaleString()} symbol-days${bars.batches_total ? ` · day ${bars.batches_done}/${bars.batches_total}` : ""} · ${bars.intraday_bars.toLocaleString()} bars pulled`
                : `${bars.symbols_saved.toLocaleString()} symbols saved${bars.symbols_total ? ` of ${bars.symbols_total.toLocaleString()}` : ""}${bars.batches_total ? ` · batch ${bars.batches_done}/${bars.batches_total}` : ""}`}
            </span>
            {bars.batches_total > 0 && (
              <progress className="sweep-bar" value={bars.batches_done} max={bars.batches_total} />
            )}
          </div>
        )}
        {bars?.last_error && !bars.running && (
          <p className="hint error">Last sweep error: {bars.last_error}</p>
        )}

        <div className="cache-grid">
          <section className="cache-col">
            <h4>Stocks</h4>
            <CacheMetrics stats={lastStock.current} hasIntraday={bars?.has_intraday ?? false} />
            <div className="card-actions">
              <button
                className="small"
                disabled={bars?.running}
                onClick={runSweep}
                title="Download daily bars for the whole US-stock universe, then rank each day's risers"
              >
                {bars?.running ? "Sweeping…" : "⭳ Run sweep"}
              </button>
              <button
                className="small btn-accent-ghost"
                disabled={bars?.running}
                onClick={runIntraday}
                title="Pull 15-minute bars for the ranked movers (enables intraday replay)"
              >
                ⭳ Sweep intraday
              </button>
              <button
                className="small btn-ghost"
                disabled={bars?.running}
                onClick={runReconstruct}
                title="Recompute the risers from bars already cached — no download"
              >
                ↻ Re-rank
              </button>
              <button
                className="small btn-info"
                type="button"
                title="Freshest cached riser + whether its 15-min data is in the cache"
                onClick={() => setShowFresh((v) => !v)}
              >
                ⓘ
              </button>
            </div>
          </section>

          <section className="cache-col">
            <h4>Crypto</h4>
            <CacheMetrics stats={lastCrypto.current} hasIntraday={bars?.crypto_has_intraday ?? false} unitPairs />
            <div className="card-actions">
              <button
                className="small"
                disabled={bars?.running}
                onClick={runCrypto}
                title="Download daily bars for every crypto USD pair, then rank each day's risers (one step)"
              >
                {bars?.running ? "Sweeping…" : "⭳ Run crypto sweep"}
              </button>
              <button
                className="small btn-accent-ghost"
                disabled={bars?.running}
                onClick={runCryptoIntraday}
                title="Pull 15-minute bars for the ranked crypto movers (enables intraday crypto replay)"
              >
                ⭳ Sweep crypto intraday
              </button>
            </div>
          </section>
        </div>

        {showFresh &&
          (lastStock.current?.freshest_mover ? (
            <p className="hint">
              Freshest stock riser: <strong>{lastStock.current.freshest_mover.symbol}</strong>{" "}
              {lastStock.current.freshest_mover.change_pct >= 0 ? "+" : ""}
              {lastStock.current.freshest_mover.change_pct}% on {lastStock.current.freshest_mover.day} —{" "}
              {lastStock.current.freshest_mover.has_intraday ? (
                <span className="ok-text">✓ 15-min data cached (intraday-ready)</span>
              ) : (
                <span className="warn-text">✗ no 15-min data yet — run Sweep intraday</span>
              )}
            </p>
          ) : (
            <p className="hint">No movers cached yet — run a sweep first.</p>
          ))}

        <details className="cache-help">
          <summary>How the sweep works (three steps)</summary>
          <p className="hint">
            <strong>1. Run sweep</strong> downloads one daily price bar (open/high/low/close) for the <em>entire</em>{" "}
            tradable US-stock universe — thousands of symbols, every stock on a real exchange, not just the movers. It's a
            raw price dump: at this point nothing yet knows which stocks were the day's risers. Takes several minutes, and
            it's cached and idempotent — run it again with more history to reach further back and only the missing older
            days are added, never re-downloaded. QT also re-runs it automatically each trading evening. A full sweep
            finishes by ranking the risers for you (step 2), so normally you never press Re-rank yourself.
          </p>
          <p className="hint">
            <strong>2. Re-rank</strong> turns that raw dump into <em>each past day's top risers</em>: for every day it
            measures each stock's gain at its intraday peak (daily high vs. the prior close), drops penny and low-volume
            junk (the scanner's price and dollar-volume filters), and keeps the top 100 (the backtest then picks how many
            of those to use per day). This is the step that <em>creates</em> the risers list — the raw sweep doesn't
            contain it. It recomputes from bars <em>already</em> cached, so no download and it takes seconds. You only
            need it on its own after changing the ranking criteria (the scanner's filters, the gain metric, or how many
            risers to keep); otherwise a full sweep already did it.
          </p>
          <p className="hint">
            <strong>3. Sweep intraday</strong> then pulls 15-minute bars for those ranked movers only (just those names,
            on their mover-days, plus a short prior-session baseline), so the backtest can judge an <em>intraday</em>{" "}
            strategy on how each day actually traded — VWAP, the entry window, and flatten-before-close all behave for
            real. Without it, replay falls back to daily bars, which can't simulate any intraday exit. Needs a daily
            sweep first so there are movers to fetch.
          </p>
          <p className="hint">
            <strong>Crypto</strong> uses its own separate cache. The crypto universe is tiny (~20–40 USD pairs), so one
            button sweeps <em>every</em> pair's daily bars and ranks each day's risers in a single step (no separate
            Re-rank); then sweep crypto intraday for 15-minute bars. Crypto is 24/7, so its "day" is the UTC calendar
            day.
          </p>
        </details>
      </details>

      <details className="card fold">
        <summary>
          <h3>Slack notifications</h3>
        </summary>
        <p className="hint">
          Create an{" "}
          <a href="https://api.slack.com/messaging/webhooks" target="_blank" rel="noreferrer">
            incoming webhook
          </a>{" "}
          in your Slack workspace and paste its URL. Trade alerts, errors, and daily summaries will post there.
          {engine.slack_configured && " (currently configured ✓)"}
        </p>
        <div className="addform">
          <input
            placeholder="https://hooks.slack.com/services/…"
            value={slackUrl}
            onChange={(e) => setSlackUrl(e.target.value)}
            style={{ width: 360 }}
          />
          <button
            className="small"
            onClick={() =>
              setSlack(slackUrl)
                .then(() => {
                  setNote("Slack webhook saved.");
                  setSlackUrl("");
                  refresh();
                })
                .catch((e: Error) => setNote(e.message))
            }
          >
            Save
          </button>
          <button
            className="small btn-ghost"
            onClick={() => testSlack().then(() => setNote("Test message sent — check Slack.")).catch((e: Error) => setNote(e.message))}
          >
            Send test
          </button>
        </div>

        {slackPrefs && (
          <div className="notify-prefs">
            <h4>What to send</h4>
            <p className="hint">
              Pick which messages QT posts. Changes save immediately.
              {!slackPrefs.configured && " Add a webhook above for these to actually send."}
            </p>
            {slackPrefs.categories.map((c) => (
              <label key={c.key} className="notify-pref">
                <input type="checkbox" checked={c.enabled} onChange={(e) => toggleSlackCat(c.key, e.target.checked)} />
                <span className="notify-pref-text">
                  <span className="notify-pref-label">{c.label}</span>
                  <span className="hint">{c.description}</span>
                </span>
              </label>
            ))}
          </div>
        )}
      </details>

      <details className="card fold">
        <summary>
          <h3>Who can sign in</h3>
        </summary>
        {!allow ? (
          <p className="hint">Allowlist unavailable.</p>
        ) : (
          <>
            <div className="table-scroll">
            <table>
              <tbody>
                {allow.emails.map((e) => (
                  <tr key={e}>
                    <td>{e}</td>
                    <td>{e.toLowerCase() === allow.owner?.toLowerCase() ? <span className="pill ok">owner</span> : ""}</td>
                    <td>
                      {e.toLowerCase() !== allow.owner?.toLowerCase() && (
                        <button className="small danger" onClick={() => removeAllowlist(e).then(refresh)}>
                          Remove
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            </div>
            <div className="addform">
              <input
                type="email"
                placeholder="brother@gmail.com"
                value={newEmail}
                onChange={(e) => setNewEmail(e.target.value)}
              />
              <button
                className="small"
                onClick={() =>
                  addAllowlist(newEmail)
                    .then(() => {
                      setNewEmail("");
                      refresh();
                    })
                    .catch((e: Error) => setNote(e.message))
                }
              >
                Add
              </button>
            </div>
          </>
        )}
      </details>
    </>
  );
}
