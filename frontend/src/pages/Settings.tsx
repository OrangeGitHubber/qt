import { FormEvent, useCallback, useEffect, useState } from "react";
import {
  addAllowlist,
  AssetStatus,
  EngineState,
  getAllowlist,
  getAssetStatus,
  getEngine,
  removeAllowlist,
  RiskConfig,
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
  const [note, setNote] = useState<string | null>(null);

  const refresh = useCallback(() => {
    getEngine().then((e) => {
      setEngine(e);
      setRiskLocal(e.risk);
    });
    getAllowlist().then(setAllow).catch(() => setAllow(null));
    getAssetStatus().then(setAssetStatus).catch(() => setAssetStatus(null));
  }, []);

  useEffect(refresh, [refresh]);

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
