import { FormEvent, useCallback, useEffect, useState } from "react";
import {
  addAllowlist,
  AssetStatus,
  BarCacheStatus,
  EngineState,
  getAllowlist,
  getAssetStatus,
  getBarCacheStatus,
  getEngine,
  removeAllowlist,
  RiskConfig,
  runBarSweep,
  runBarReconstruct,
  runIntradaySweep,
  setRegimeEnabled,
  setRisk,
  setSlack,
  syncAssets,
  testSlack,
} from "../api";
import InfoTip from "../components/InfoTip";
import NumberField from "../components/NumberField";

export default function Settings() {
  const [engine, setEngine] = useState<EngineState | null>(null);
  const [risk, setRiskLocal] = useState<RiskConfig | null>(null);
  const [allow, setAllow] = useState<{ emails: string[]; owner: string } | null>(null);
  const [newEmail, setNewEmail] = useState("");
  const [slackUrl, setSlackUrl] = useState("");
  const [leverageConfirm, setLeverageConfirm] = useState("");
  const [assetStatus, setAssetStatus] = useState<AssetStatus | null>(null);
  const [syncing, setSyncing] = useState(false);
  const [bars, setBars] = useState<BarCacheStatus | null>(null);
  const [note, setNote] = useState<string | null>(null);

  const refresh = useCallback(() => {
    getEngine().then((e) => {
      setEngine(e);
      setRiskLocal(e.risk);
    });
    getAllowlist().then(setAllow).catch(() => setAllow(null));
    getAssetStatus().then(setAssetStatus).catch(() => setAssetStatus(null));
    getBarCacheStatus().then(setBars).catch(() => setBars(null));
  }, []);

  useEffect(refresh, [refresh]);

  // Poll the sweep status while a sweep is running so progress updates live.
  useEffect(() => {
    if (!bars?.running) return;
    const t = setInterval(() => getBarCacheStatus().then(setBars).catch(() => {}), 3000);
    return () => clearInterval(t);
  }, [bars?.running]);

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

      <form className="card" onSubmit={saveRisk}>
        <h3>Risk rails (apply to every strategy, every mode)</h3>
        <div className="filter-grid">
          <label>
            Max daily loss ($) <InfoTip k="daily_loss_limit" />
            <NumberField min={10} step="any" {...num("max_daily_loss_usd")} />
          </label>
          <label>
            Max daily loss (% of account)
            <NumberField min={0.5} step={0.5} {...num("max_daily_loss_pct")} />
          </label>
          <label>
            Max open positions (total)
            <NumberField min={1} step={1} {...num("max_total_positions")} />
          </label>
          <label>
            Max total exposure ($)
            <NumberField min={10} step="any" {...num("max_total_exposure_usd")} />
          </label>
          <label>
            Max new trades per day <InfoTip k="trade_rate" />
            <NumberField min={1} step={1} {...num("max_trades_per_day")} />
          </label>
          <label>
            Cooldown after a loss (hours)
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
            <h4>⚠ Leverage (unlocked at server level)</h4>
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

      <div className="card">
        <h3>Symbol directory</h3>
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
          className="small"
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
      </div>

      <div className="card">
        <h3>Historical bar cache</h3>
        <p className="hint">
          The historical data a <strong>"scanner replay" backtest</strong> reads, built in three steps and stored in{" "}
          {bars ? (
            <strong>
              {bars.backend.kind === "postgres"
                ? `Postgres${bars.backend.host ? ` (${bars.backend.host})` : ""}`
                : "a local SQLite file"}
            </strong>
          ) : (
            "…"
          )}
          . Normal order: <strong>Run sweep → Sweep intraday → backtest</strong> — only touch Re-rank if you change the
          ranking criteria (see below).
        </p>
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
          contain it. It recomputes from bars <em>already</em> cached, so no download and it takes seconds. You only need
          it on its own after changing the ranking criteria (the scanner's filters, the gain metric, or how many risers
          to keep); otherwise a full sweep already did it.
        </p>
        <p className="hint">
          <strong>3. Sweep intraday</strong> then pulls 15-minute bars for those ranked movers only (just those names, on
          their mover-days, plus a short prior-session baseline), so the backtest can judge an <em>intraday</em> strategy
          on how each day actually traded — VWAP, the entry window, and flatten-before-close all behave for real. Without
          it, replay falls back to daily bars, which can't simulate any intraday exit. Needs a daily sweep first so there
          are movers to fetch.
        </p>
        {bars && (
          <dl>
            <dt>Status</dt>
            <dd>
              {bars.running
                ? { intraday: "intraday sweep…", reconstruct: "re-ranking…" }[bars.kind] ?? "daily sweep…"
                : "idle"}
              {bars.last_error && <span className="error"> — {bars.last_error}</span>}
            </dd>
            {bars.running ? (
              // Live progress of the in-flight sweep (resets on redeploy — that's fine, it's ephemeral).
              <>
                <dt>Progress</dt>
                <dd>
                  {bars.kind === "intraday"
                    ? `${bars.symbols_saved.toLocaleString()} symbol-days${bars.batches_total ? ` · day ${bars.batches_done}/${bars.batches_total}` : ""}`
                    : `${bars.symbols_saved.toLocaleString()} symbols saved${bars.symbols_total ? ` of ${bars.symbols_total.toLocaleString()}` : ""}${bars.batches_total ? ` · batch ${bars.batches_done}/${bars.batches_total}` : ""}`}
                </dd>
                {bars.kind === "intraday" && <dt>Intraday bars</dt>}
                {bars.kind === "intraday" && <dd>{bars.intraday_bars.toLocaleString()} pulled (in progress)</dd>}
              </>
            ) : (
              // Persisted cache contents — read from the DB, so they survive redeploys.
              <>
                <dt>Symbols cached</dt>
                <dd>{(bars.cache?.daily_symbols ?? 0).toLocaleString()}</dd>
                <dt>Days of movers</dt>
                <dd>{(bars.cache?.movers_days ?? 0).toLocaleString()}</dd>
                <dt>Intraday bars</dt>
                <dd>
                  {bars.has_intraday
                    ? `${(bars.cache?.intraday_bars ?? 0).toLocaleString()} cached — intraday replay ready`
                    : "none yet — replay uses daily bars"}
                </dd>
                <dt>Data through</dt>
                <dd>{bars.cache?.latest_day ?? "—"}</dd>
              </>
            )}
          </dl>
        )}
        <button className="small" disabled={bars?.running} onClick={runSweep}>
          {bars?.running ? "Sweeping…" : "Run sweep"}
        </button>{" "}
        <button className="small" disabled={bars?.running} onClick={runReconstruct}>
          Re-rank
        </button>{" "}
        <button className="small" disabled={bars?.running} onClick={runIntraday}>
          Sweep intraday
        </button>
      </div>

      <div className="card">
        <h3>Slack notifications</h3>
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
            className="small"
            onClick={() => testSlack().then(() => setNote("Test message sent — check Slack.")).catch((e: Error) => setNote(e.message))}
          >
            Send test
          </button>
        </div>
      </div>

      <div className="card">
        <h3>Who can sign in</h3>
        {!allow ? (
          <p className="hint">Allowlist unavailable.</p>
        ) : (
          <>
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
      </div>
    </>
  );
}
