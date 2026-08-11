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
  setDisplayTimezone,
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
import { browserZone, fmtDateTime, setDisplayZone, useDisplayZone, ZONE_CHOICES, zoneAbbr } from "../lib/datetime";

// Which clock every timestamp in the app is drawn on. Display only — the
// database is UTC and stays UTC — but it is the difference between "the replay
// sold three hours later" being a bug report and being an artefact of reading a
// New York market on a Johannesburg clock. Saved server-side, so it holds
// across a container restart and reads the same on a phone as on the desktop.
function DisplayZoneCard({ onError }: { onError: (msg: string) => void }) {
  const zone = useDisplayZone();
  const [busy, setBusy] = useState(false);
  // Extra rows below the fixed list: the browser's own zone, and — if the saved
  // setting is some other IANA name (the API accepts any valid one) — that too,
  // so the select never renders blank against a value it has no option for.
  const known = new Set(ZONE_CHOICES.map((c) => c.zone));
  const mine = browserZone();
  const extras: { zone: string; label: string }[] = [];
  if (!known.has(mine)) extras.push({ zone: mine, label: `${mine} — this browser's zone` });
  if (!known.has(zone) && zone !== mine) extras.push({ zone, label: zone });

  async function save(next: string) {
    const previous = zone;
    setDisplayZone(next); // optimistic: the sample below updates as you pick
    setBusy(true);
    try {
      await setDisplayTimezone(next);
    } catch (err) {
      setDisplayZone(previous);
      onError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <details className="card fold">
      <summary>
        <div className="cache-head">
          <h3>Display</h3>
          <span className="pill muted">{zoneAbbr()}</span>
        </div>
      </summary>
      <p className="hint">
        The clock QT shows you. Everything is <strong>stored</strong> in UTC and nothing here changes that — this
        only decides how those moments are written out. It defaults to New York because that is the market's own
        clock: an entry at 09:45 ET means something, and the same instant as 15:45 at home does not.
      </p>
      <label>
        Timezone <InfoTip k="display_timezone" />
        <select value={zone} disabled={busy} onChange={(e) => save(e.target.value)}>
          {ZONE_CHOICES.map((c) => (
            <option key={c.zone} value={c.zone}>
              {c.label}
            </option>
          ))}
          {extras.map((c) => (
            <option key={c.zone} value={c.zone}>
              {c.label}
            </option>
          ))}
        </select>
      </label>
      <p className="hint">
        Right now that reads <strong>{fmtDateTime(new Date().toISOString())}</strong> ({zoneAbbr()}). Daylight saving
        is handled by the zone itself, so a summer trade and a winter one both come out right.
      </p>
    </details>
  );
}

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

// THE FORM'S OWN LABELS, keyed by the field name the API validates on. A
// rejected save names the field in code — "max_total_positions: Input should be
// less than or equal to 50" — and the reader is looking at a form that says
// "Max open positions (total)" somewhere else on the row. Werner hit exactly
// that: the message named one field while he was editing another, so it read as
// though the box under his cursor was wrong.
//
// The risk form PUTs every field at once, so ANY field out of range blocks the
// whole save — including one the user has not touched in months. Naming it in
// the words on screen, and outlining the input itself, is what turns an
// unexplainable rejection into a two-second fix.
const RISK_LABELS: Record<string, string> = {
  max_daily_loss_usd: "Max daily loss ($)",
  max_daily_loss_pct: "Max daily loss (% of account)",
  max_total_positions: "Max open positions (total)",
  max_total_exposure_usd: "Max total exposure ($)",
  max_trades_per_day: "Max new trades per day",
  cooldown_hours_after_loss: "Cooldown after a loss (hours)",
  wash_sale_guard: "Wash-sale guard",
};

/** Field keys named in a validation message, so the inputs can be outlined. */
function offendingFields(message: string): string[] {
  return Object.keys(RISK_LABELS).filter((key) => message.includes(key + ":"));
}

/** The same message with code-level field names replaced by the form's labels. */
function inPlainLabels(message: string): string {
  return Object.entries(RISK_LABELS).reduce(
    (text, [key, label]) => text.split(key + ":").join(label + " —"),
    message,
  );
}

/** The accepted range, shown under the input — and the rejection, when there is
 *  one, under the field the server actually named.
 *
 *  Both come from the SAME numbers as the input's own min/max, so the hint
 *  cannot drift from what the form will accept. Werner typed 1000 into a field
 *  capped at 50 because nothing on screen said 50: the API knew, the input knew
 *  once `max` was added, and the person filling it in was the only one who
 *  didn't. UX guidance calls this `input-helper-text` and `error-placement` —
 *  the error belongs beside the problem, not only in a banner at the top. */
function Limits({ min, max, unit, error }: {
  min: number; max: number; unit?: string; error?: string;
}) {
  return (
    <>
      <span className="field-hint">
        {min.toLocaleString()}–{max.toLocaleString()}
        {unit ? ` ${unit}` : ""}
      </span>
      {error && <span className="field-error">{error}</span>}
    </>
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
  // Success and failure used the same neutral banner, so a rejected save looked
  // like a confirmation. Tracked separately rather than sniffed from the text.
  const [noteIsError, setNoteIsError] = useState(false);
  // Which inputs the rejection actually named. The risk form sends every field
  // at once, so the offending one is routinely NOT the one under the cursor.
  const badFields = noteIsError && note ? offendingFields(note) : [];
  /** [min, max] for a field, from the server's own validators. */
  const lim = (key: string): [number, number] =>
    (engine?.risk_limits?.[key] as [number, number]) ?? [0, 0];
  /** The part of a rejection that concerns one field, without repeating its name. */
  const fieldError = (key: string): string | undefined => {
    if (!note || !badFields.includes(key)) return undefined;
    const part = note.split("; ").find((p) => p.startsWith(key + ":"));
    return part ? part.slice(key.length + 1).trim() : undefined;
  };
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
    setNoteIsError(false);
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
      setNoteIsError(true);
    } finally {
      setBrokerBusy(false);
    }
  }

  async function doLiquidate() {
    setNote(null);
    setNoteIsError(false);
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
      setNoteIsError(true);
    } finally {
      setLiqBusy(false);
    }
  }

  async function runSweep() {
    setNote(null);
    setNoteIsError(false);
    try {
      await runBarSweep();
      const s = await getBarCacheStatus();
      setBars(s); // flips running=true, which starts the poll above
    } catch (e) {
      setNote((e as Error).message);
      setNoteIsError(true);
    }
  }

  async function runReconstruct() {
    setNote(null);
    setNoteIsError(false);
    try {
      await runBarReconstruct();
      setBars(await getBarCacheStatus());
    } catch (e) {
      setNote((e as Error).message);
      setNoteIsError(true);
    }
  }

  async function runIntraday() {
    setNote(null);
    setNoteIsError(false);
    try {
      await runIntradaySweep();
      setBars(await getBarCacheStatus()); // flips running=true, starts the poll
    } catch (e) {
      setNote((e as Error).message);
      setNoteIsError(true);
    }
  }

  async function runCrypto() {
    setNote(null);
    setNoteIsError(false);
    try {
      await runCryptoSweep();
      setBars(await getBarCacheStatus()); // flips running=true, starts the poll
    } catch (e) {
      setNote((e as Error).message);
      setNoteIsError(true);
    }
  }

  async function runCryptoIntraday() {
    setNote(null);
    setNoteIsError(false);
    try {
      await runCryptoIntradaySweep();
      setBars(await getBarCacheStatus());
    } catch (e) {
      setNote((e as Error).message);
      setNoteIsError(true);
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
    setNoteIsError(false);
    try {
      await setRegimeEnabled(enabled);
    } catch (err) {
      setEngine((prev) => (prev ? { ...prev, regime_filter_enabled: !enabled } : prev));
      setNote((err as Error).message);
      setNoteIsError(true);
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
    setNoteIsError(false);
    try {
      await setSlackPrefs({ [key]: enabled });
    } catch (err) {
      flip(!enabled);
      setNote((err as Error).message);
      setNoteIsError(true);
    }
  }

  async function saveRisk(e: FormEvent) {
    e.preventDefault();
    if (!risk) return;
    setNote(null);
    setNoteIsError(false);
    try {
      await setRisk({ ...risk, leverage_confirm: leverageConfirm });
      setLeverageConfirm("");
      setNote("Risk settings saved.");
      refresh();
    } catch (err) {
      const message = (err as Error).message;
      setNote(message);
      setNoteIsError(true);
      // WCAG focus-management: after a rejected submit, put the cursor in the
      // first field the server complained about. The risk form sends every
      // field at once, so without this the user is left hunting for which of
      // six inputs is the problem — which is exactly what happened.
      const first = offendingFields(message)[0];
      if (first) {
        requestAnimationFrame(() => {
          document.querySelector<HTMLInputElement>(`[data-field="${first}"] input`)?.focus();
        });
      }
    }
  }

  if (!engine || !risk) return <div className="card">Loading…</div>;

  return (
    <>
      <div className="toolbar">
        <h2>Settings</h2>
      </div>
      {note && (
        <div
          className={noteIsError ? "card note error" : "card note"}
          role={noteIsError ? "alert" : undefined}
          onClick={() => setNote(null)}
        >
          {noteIsError ? inPlainLabels(note) : note}
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
              {/* FULL-SIZE ghost, not `small`. styles.css already documents the
                  convention — "a full-size ghost sits beside a primary button
                  (e.g. Save/Cancel): absorb its 1px border into the padding so
                  the two are exactly the same height" — and `button.small` also
                  zeroes the 1.2rem top margin the primary keeps, so this pair
                  was mismatched in both height and baseline. */}
              <button
                type="button"
                className="btn-ghost"
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

      <DisplayZoneCard onError={setNote} />

      <details className="card fold">
        <summary>
          <h3>Risk rails (apply to every strategy, every mode)</h3>
        </summary>
        <form onSubmit={saveRisk}>
        <div className="filter-grid">
          <label data-field="max_daily_loss_usd" className={badFields.includes("max_daily_loss_usd") ? "bad" : undefined}>
            Max daily loss ($) <InfoTip k="daily_loss_limit" />
            <NumberField min={lim("max_daily_loss_usd")[0]} max={lim("max_daily_loss_usd")[1]} step="any" {...num("max_daily_loss_usd")} />
            <Limits min={lim("max_daily_loss_usd")[0]} max={lim("max_daily_loss_usd")[1]} unit="$" error={fieldError("max_daily_loss_usd")} />
          </label>
          <label data-field="max_daily_loss_pct" className={badFields.includes("max_daily_loss_pct") ? "bad" : undefined}>
            Max daily loss (% of account) <InfoTip k="daily_loss_limit" />
            <NumberField min={lim("max_daily_loss_pct")[0]} max={lim("max_daily_loss_pct")[1]} step={0.5} {...num("max_daily_loss_pct")} />
            <Limits min={lim("max_daily_loss_pct")[0]} max={lim("max_daily_loss_pct")[1]} unit="%" error={fieldError("max_daily_loss_pct")} />
          </label>
          <label data-field="max_total_positions" className={badFields.includes("max_total_positions") ? "bad" : undefined}>
            Max open positions (total) <InfoTip k="max_positions" />
            <NumberField min={lim("max_total_positions")[0]} max={lim("max_total_positions")[1]} step={1} {...num("max_total_positions")} />
            <Limits min={lim("max_total_positions")[0]} max={lim("max_total_positions")[1]} error={fieldError("max_total_positions")} />
          </label>
          <label data-field="max_total_exposure_usd" className={badFields.includes("max_total_exposure_usd") ? "bad" : undefined}>
            Max total exposure ($) <InfoTip k="max_exposure" />
            <NumberField min={lim("max_total_exposure_usd")[0]} max={lim("max_total_exposure_usd")[1]} step="any" {...num("max_total_exposure_usd")} />
            <Limits min={lim("max_total_exposure_usd")[0]} max={lim("max_total_exposure_usd")[1]} unit="$" error={fieldError("max_total_exposure_usd")} />
          </label>
          <label data-field="max_trades_per_day" className={badFields.includes("max_trades_per_day") ? "bad" : undefined}>
            Max new trades per day <InfoTip k="trade_rate" />
            <NumberField min={lim("max_trades_per_day")[0]} max={lim("max_trades_per_day")[1]} step={1} {...num("max_trades_per_day")} />
            <Limits min={lim("max_trades_per_day")[0]} max={lim("max_trades_per_day")[1]} unit="per day" error={fieldError("max_trades_per_day")} />
          </label>
          <label data-field="cooldown_hours_after_loss" className={badFields.includes("cooldown_hours_after_loss") ? "bad" : undefined}>
            Cooldown after a loss (hours) <InfoTip k="cooldown" />
            <NumberField min={lim("cooldown_hours_after_loss")[0]} max={lim("cooldown_hours_after_loss")[1]} step="any" {...num("cooldown_hours_after_loss")} />
            <Limits min={lim("cooldown_hours_after_loss")[0]} max={lim("cooldown_hours_after_loss")[1]} unit="hours" error={fieldError("cooldown_hours_after_loss")} />
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
            <dd>{assetStatus.updated_at ? fmtDateTime(assetStatus.updated_at) : "never"}</dd>
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
          . <strong>You don't need to do anything here.</strong> QT builds this cache by itself the first
          time an enabled strategy uses the scanner as its universe, keeps it current every day, and pulls
          the 15-minute bars a backtest needs when that backtest asks for them. The buttons below are for
          forcing a rebuild or reaching further back than the automatic year — not steps to work through.
          Stocks and crypto keep separate caches. (See “How this cache works” at the bottom.)
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
                title="Rebuild now instead of waiting for tonight's automatic run, or reach further back than a year"
              >
                {bars?.running ? "Sweeping…" : "⭳ Run sweep"}
              </button>
              <button
                className="small btn-accent-ghost"
                disabled={bars?.running}
                onClick={runIntraday}
                title="Pull 15-minute bars ahead of time. A backtest fetches the ones it needs by itself."
              >
                ⭳ Sweep intraday
              </button>
              <button
                className="small btn-ghost"
                disabled={bars?.running}
                onClick={runReconstruct}
                title="Rarely needed: the nightly run and every full sweep already rank. For after a criteria change."
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
            <p className="hint">
              No movers cached yet — this fills itself in once a scanner strategy is enabled, or now if you
              press Run sweep.
            </p>
          ))}

        <details className="cache-help">
          <summary>How this cache works (and why you don't have to touch it)</summary>
          <p className="hint">
            <strong>It builds itself.</strong> The moment you enable a strategy whose universe is the scanner
            (alone or alongside your watchlist), QT downloads a year of history for that asset class and keeps
            it current daily. Nothing is downloaded for an asset class no enabled strategy replays, so a cache
            you have no use for never costs you anything. The steps below are what that automatic build does —
            worth reading to understand the numbers, not to follow.
          </p>
          <p className="hint">
            <strong>Nothing is thrown away that you could still ask for.</strong> 15-minute bars are the only
            part that grows quickly, so anything older than the longest backtest QT will accept (730 days) is
            reclaimed nightly. A prune can therefore never cause a re-download — it only removes bars no
            backtest is able to reach.
          </p>
          <p className="hint">
            <strong>1. The daily sweep</strong> downloads one daily price bar (open/high/low/close) for the <em>entire</em>{" "}
            tradable US-stock universe — thousands of symbols, every stock on a real exchange, not just the movers. It's a
            raw price dump: at this point nothing yet knows which stocks were the day's risers. Takes several minutes, and
            it's cached and idempotent — run it again with more history to reach further back and only the missing older
            days are added, never re-downloaded. QT re-runs it automatically each trading evening. It finishes by
            ranking the risers for you (step 2), which is why the Re-rank button is almost never the answer.
          </p>
          <p className="hint">
            <strong>2. Ranking</strong> turns that raw dump into <em>each past day's top risers</em>: for every day it
            measures each stock's gain at its intraday peak (daily high vs. the prior close), drops penny and low-volume
            junk (the scanner's price and dollar-volume filters), and keeps the top 100 (the backtest then picks how many
            of those to use per day). This is the step that <em>creates</em> the risers list — the raw sweep doesn't
            contain it. It recomputes from bars <em>already</em> cached, so no download and it takes seconds. You only
            need it on its own after changing the ranking criteria (the scanner's filters, the gain metric, or how many
            risers to keep); otherwise a full sweep already did it.
          </p>
          <p className="hint">
            <strong>3. The 15-minute bars</strong> are pulled for those ranked movers only (just those names, on
            their mover-days, plus a short prior-session baseline), so the backtest can judge an <em>intraday</em>{" "}
            strategy on how each day actually traded — VWAP, the entry window, and flatten-before-close all behave for
            real. A backtest fetches whatever it is missing for itself, so the button is only a way to pull them ahead
            of time. Without them, replay falls back to daily bars, which can't simulate any intraday exit.
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
