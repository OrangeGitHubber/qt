import { useCallback, useEffect, useState } from "react";
import {
  Basket,
  createStrategy,
  deleteStrategy,
  getBaskets,
  getPresets,
  getStrategies,
  Preset,
  RankBy,
  StrategyRow,
  toggleStrategy,
  updateStrategy,
} from "../api";
import InfoTip from "../components/InfoTip";
import NumberField from "../components/NumberField";

const RANK_LABELS: Record<RankBy, string> = {
  momentum_today: "Today's % move (momentum)",
  return_30d: "30-day return",
  relative_strength: "Relative strength (vs 200-day average)",
};

const EMPTY: Partial<StrategyRow> = {
  name: "",
  asset_class: "stock",
  universe: "scanner",
  basket_id: null,
  rank_by: "momentum_today",
  top_n: 10,
  preset: "custom",
  swing_mode: true,
  ignore_regime: false,
  sizing_usd: 200,
  sleeve_usd: 1000,
  max_positions: 3,
  params: {
    entry: { min_day_gain_pct: 3, require_above_vwap: true, entry_window_start: "10:00", entry_window_end: "15:30" },
    exit: {
      trailing_stop_pct: 5,
      stop_loss_pct: 4,
      take_profit_pct: 12,
      max_holding_hours: 120,
      flatten_before_close: false,
      exit_below_vwap: false,
    },
  },
};

function Editor({
  initial,
  presets,
  baskets,
  onSaved,
  onCancel,
}: {
  initial: Partial<StrategyRow>;
  presets: Record<string, Preset>;
  baskets: Basket[];
  onSaved: () => void;
  onCancel: () => void;
}) {
  const [s, setS] = useState<Partial<StrategyRow>>(JSON.parse(JSON.stringify(initial)));
  const [error, setError] = useState<string | null>(null);

  function applyPreset(key: string) {
    if (key === "custom") {
      setS({ ...s, preset: "custom" });
      return;
    }
    const p = presets[key];
    setS({
      ...s,
      preset: key,
      name: s.name || p.label,
      asset_class: p.asset_class,
      universe: p.universe as StrategyRow["universe"],
      swing_mode: p.swing_mode,
      params: JSON.parse(JSON.stringify(p.params)),
    });
  }

  function setEntry(key: string, value: unknown) {
    setS((cur) => ({ ...cur, params: { ...cur.params!, entry: { ...cur.params!.entry, [key]: value } } }));
  }
  function setExit(key: string, value: unknown) {
    setS((cur) => ({ ...cur, params: { ...cur.params!, exit: { ...cur.params!.exit, [key]: value } } }));
  }

  async function save(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    try {
      if (s.id) await updateStrategy(s.id, s);
      else await createStrategy(s);
      onSaved();
    } catch (err) {
      setError((err as Error).message);
    }
  }

  const p = s.params!;
  return (
    <form className="card editor" onSubmit={save}>
      <h3>{s.id ? `Edit: ${s.name}` : "New strategy"}</h3>
      <div className="filter-grid">
        <label>
          Start from preset
          <select value={s.preset} onChange={(e) => applyPreset(e.target.value)}>
            <option value="custom">Custom</option>
            {Object.entries(presets).map(([k, v]) => (
              <option key={k} value={k}>
                {v.label}
              </option>
            ))}
          </select>
        </label>
        <label>
          Name
          <input value={s.name ?? ""} onChange={(e) => setS({ ...s, name: e.target.value })} required />
        </label>
        <label>
          Asset class
          <select value={s.asset_class} onChange={(e) => setS({ ...s, asset_class: e.target.value as "stock" | "crypto" })}>
            <option value="stock">Stocks</option>
            <option value="crypto">Crypto</option>
          </select>
        </label>
        <label>
          Universe
          <select value={s.universe} onChange={(e) => setS({ ...s, universe: e.target.value as StrategyRow["universe"] })}>
            <option value="scanner">Scanner (today's risers)</option>
            <option value="watchlist">Watchlist only</option>
            <option value="both">Scanner + watchlist</option>
            <option value="basket">Basket (sector/theme)</option>
          </select>
        </label>
      </div>
      {s.preset !== "custom" && presets[s.preset!] && <p className="hint">{presets[s.preset!].description}</p>}

      {s.universe === "basket" && (
        <>
          <h4>
            Basket ranking <InfoTip k="rank_by" />
          </h4>
          <div className="filter-grid">
            <label>
              Basket
              <select
                value={s.basket_id ?? ""}
                onChange={(e) => setS({ ...s, basket_id: e.target.value ? Number(e.target.value) : null })}
                required
              >
                <option value="">— pick a basket —</option>
                {baskets.map((b) => (
                  <option key={b.id} value={b.id}>
                    {b.name} ({b.count})
                  </option>
                ))}
              </select>
            </label>
            <label>
              Rank by
              <select value={s.rank_by} onChange={(e) => setS({ ...s, rank_by: e.target.value as RankBy })}>
                {(Object.keys(RANK_LABELS) as RankBy[]).map((k) => (
                  <option key={k} value={k}>
                    {RANK_LABELS[k]}
                  </option>
                ))}
              </select>
            </label>
            <label>
              Take top N
              <NumberField step="1" min="1" max="50" value={s.top_n!} onChange={(n) => setS({ ...s, top_n: n })} />
            </label>
          </div>
          <p className="hint">
            The live engine ranks the basket's members by this metric and considers the top {s.top_n} as candidates
            (your entry rules still apply). Baskets are <strong>curated lists, not a sector database</strong>. Top-N
            ranking is a live feature — a backtest of this strategy tests the whole basket over history.
          </p>
        </>
      )}

      <h4>Entry rules</h4>
      <div className="filter-grid">
        <label>
          Min gain today (%)
          <NumberField step="0.1" min="0" value={p.entry.min_day_gain_pct}
            onChange={(n) => setEntry("min_day_gain_pct", n)} />
        </label>
        <label className="check">
          <input type="checkbox" checked={p.entry.require_above_vwap}
            onChange={(e) => setEntry("require_above_vwap", e.target.checked)} />
          Require price above VWAP <InfoTip k="vwap" />
        </label>
        <label>
          Entry window start (ET, blank = any)
          <input value={p.entry.entry_window_start ?? ""} placeholder="10:00"
            onChange={(e) => setEntry("entry_window_start", e.target.value || null)} />
        </label>
        <label>
          Entry window end (ET)
          <input value={p.entry.entry_window_end ?? ""} placeholder="15:30"
            onChange={(e) => setEntry("entry_window_end", e.target.value || null)} />
        </label>
      </div>

      <h4>Exit rules — "the configurable downturn"</h4>
      <div className="filter-grid">
        <label>
          Trailing stop (%) <InfoTip k="trailing_stop" />
          <NumberField step="0.1" min="0.5" value={p.exit.trailing_stop_pct}
            onChange={(n) => setExit("trailing_stop_pct", n)} />
        </label>
        <label>
          Stop-loss (%) — required <InfoTip k="stop_loss" />
          <NumberField step="0.1" min="0.1" value={p.exit.stop_loss_pct}
            onChange={(n) => setExit("stop_loss_pct", n)} />
        </label>
        <label>
          Take-profit (%, 0 = off) <InfoTip k="take_profit" />
          <NumberField step="0.1" min="0" value={p.exit.take_profit_pct}
            onChange={(n) => setExit("take_profit_pct", n)} />
        </label>
        <label>
          Max holding time (hours, 0 = off)
          <NumberField step="1" min="0" value={p.exit.max_holding_hours}
            onChange={(n) => setExit("max_holding_hours", n)} />
        </label>
        <label className="check">
          <input type="checkbox" checked={p.exit.exit_below_vwap}
            onChange={(e) => setExit("exit_below_vwap", e.target.checked)} />
          Exit if price falls below VWAP <InfoTip k="vwap" />
        </label>
        <label className="check">
          <input type="checkbox" checked={p.exit.flatten_before_close}
            onChange={(e) => setExit("flatten_before_close", e.target.checked)} />
          Flatten before market close
        </label>
      </div>

      <h4>Sizing & safety</h4>
      <div className="filter-grid">
        <label>
          $ per trade
          <NumberField step="any" min="10" value={s.sizing_usd!}
            onChange={(n) => setS({ ...s, sizing_usd: n })} />
        </label>
        <label>
          Sleeve budget ($) <InfoTip k="sleeve" />
          <NumberField step="any" min="10" value={s.sleeve_usd!}
            onChange={(n) => setS({ ...s, sleeve_usd: n })} />
        </label>
        <label>
          Max positions (this strategy)
          <NumberField step="1" min="1" max="25" value={s.max_positions!}
            onChange={(n) => setS({ ...s, max_positions: n })} />
        </label>
        <label className="check">
          <input type="checkbox" checked={s.swing_mode}
            onChange={(e) => setS({ ...s, swing_mode: e.target.checked })} />
          Swing mode <InfoTip k="swing_mode" />
        </label>
        <label className="check">
          <input type="checkbox" checked={s.ignore_regime}
            onChange={(e) => setS({ ...s, ignore_regime: e.target.checked })} />
          Ignore regime filter (not recommended) <InfoTip k="regime_filter" />
        </label>
      </div>
      {error && <div className="error">{error}</div>}
      <div className="toolbar">
        <button>{s.id ? "Save (creates new config version)" : "Create strategy"}</button>
        <button type="button" className="small" onClick={onCancel}>
          Cancel
        </button>
      </div>
    </form>
  );
}

export default function Strategies() {
  const [rows, setRows] = useState<StrategyRow[] | null>(null);
  const [presets, setPresets] = useState<Record<string, Preset>>({});
  const [baskets, setBaskets] = useState<Basket[]>([]);
  const [editing, setEditing] = useState<Partial<StrategyRow> | null>(null);
  const [note, setNote] = useState<string | null>(null);

  const refresh = useCallback(() => {
    getStrategies().then(setRows).catch((e: Error) => setNote(e.message));
  }, []);

  useEffect(() => {
    refresh();
    getPresets().then(setPresets);
    getBaskets().then(setBaskets).catch(() => setBaskets([]));
  }, [refresh]);

  const basketName = (id: number | null) => baskets.find((b) => b.id === id)?.name ?? `#${id}`;

  async function toggle(row: StrategyRow) {
    await toggleStrategy(row.id);
    refresh();
  }

  async function remove(row: StrategyRow) {
    try {
      await deleteStrategy(row.id);
      refresh();
    } catch (e) {
      setNote((e as Error).message);
    }
  }

  return (
    <>
      <div className="toolbar">
        <h2>Strategies</h2>
        <button className="small" onClick={() => setEditing(EMPTY)}>
          + New strategy
        </button>
      </div>
      {note && (
        <div className="card note" onClick={() => setNote(null)}>
          {note}
        </div>
      )}
      {editing && (
        <Editor
          initial={editing}
          presets={presets}
          baskets={baskets}
          onSaved={() => {
            setEditing(null);
            refresh();
          }}
          onCancel={() => setEditing(null)}
        />
      )}
      {!rows ? (
        <div className="card">Loading…</div>
      ) : rows.length === 0 ? (
        <div className="card">
          <p className="hint">
            No strategies yet. Create one from a preset — it starts <strong>disabled</strong> and trades nothing until
            you enable it AND turn the engine on (shadow mode first).
          </p>
        </div>
      ) : (
        <div className="grid">
          {rows.map((r) => (
            <div className="card" key={r.id}>
              <h3>
                {r.name} <span className={`pill ${r.enabled ? "ok" : "muted"}`}>{r.enabled ? "ENABLED" : "paused"}</span>
              </h3>
              <dl>
                <dt>Trades</dt>
                <dd>
                  {r.asset_class} ·{" "}
                  {r.universe === "basket"
                    ? `basket "${basketName(r.basket_id)}" · top ${r.top_n} by ${RANK_LABELS[r.rank_by]}`
                    : r.universe}{" "}
                  · {r.swing_mode ? "swing" : "intraday"}
                </dd>
                <dt>Entry</dt>
                <dd>
                  +{r.params.entry.min_day_gain_pct}% day{r.params.entry.require_above_vwap ? ", above VWAP" : ""}
                </dd>
                <dt>Exit</dt>
                <dd>
                  trail {r.params.exit.trailing_stop_pct}% · stop {r.params.exit.stop_loss_pct}%
                  {r.params.exit.take_profit_pct ? ` · target ${r.params.exit.take_profit_pct}%` : ""}
                </dd>
                <dt>Sizing</dt>
                <dd>
                  ${r.sizing_usd} / trade, ${r.sleeve_usd} sleeve, max {r.max_positions}
                </dd>
                <dt>Config</dt>
                <dd>
                  v{r.version} · {r.open_trades ?? 0} open trade(s)
                </dd>
              </dl>
              <div className="toolbar">
                <button className="small" onClick={() => toggle(r)}>
                  {r.enabled ? "Pause" : "Enable"}
                </button>
                <button className="small" onClick={() => setEditing(r)}>
                  Edit
                </button>
                <button className="small danger" onClick={() => remove(r)}>
                  Delete
                </button>
              </div>
            </div>
          ))}
        </div>
      )}
    </>
  );
}
