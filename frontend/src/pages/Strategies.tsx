import { useCallback, useEffect, useState } from "react";
import {
  Basket,
  createStrategy,
  deleteStrategy,
  getBaskets,
  getPresets,
  getStatus,
  getStrategies,
  Preset,
  RankBy,
  StrategyRow,
  toggleStrategy,
  updateStrategy,
} from "../api";
import InfoTip from "../components/InfoTip";
import NumberField from "../components/NumberField";
import SymbolPicker from "../components/SymbolPicker";

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
  symbols: [],
  rank_by: "momentum_today",
  top_n: 10,
  preset: "custom",
  swing_mode: true,
  ignore_regime: false,
  sizing_usd: 200,
  sleeve_usd: 1000,
  max_positions: 3,
  params: {
    entry: {
      min_day_gain_pct: 3,
      max_day_gain_pct: 0,
      min_price: 0,
      max_price: 0,
      require_above_vwap: true,
      entry_window_start: "09:30",
      entry_window_end: "15:30",
      entry_slippage_pct: 0.5,
    },
    exit: {
      trailing_stop_pct: 5,
      stop_loss_pct: 4,
      take_profit_pct: 12,
      max_holding_hours: 120,
      flatten_before_close: false,
      exit_below_vwap: false,
      exit_slippage_pct: 1,
      exit_slippage_max_pct: 1,
    },
  },
};

function Editor({
  initial,
  presets,
  baskets,
  allStrategies,
  equity,
  onSaved,
  onCancel,
}: {
  initial: Partial<StrategyRow>;
  presets: Record<string, Preset>;
  baskets: Basket[];
  allStrategies: StrategyRow[];
  equity: number | null;
  onSaved: () => void;
  onCancel: () => void;
}) {
  const [s, setS] = useState<Partial<StrategyRow>>(JSON.parse(JSON.stringify(initial)));
  const [error, setError] = useState<string | null>(null);

  // Live sleeve-allocation readout. Sleeves are allowed to overlap (sum > equity)
  // on purpose — whichever strategy trades first draws the shared cash, and the
  // no-leverage rail caps total spending at real equity. So this only informs,
  // never blocks.
  const otherSleeves = allStrategies
    .filter((r) => r.id !== s.id)
    .reduce((sum, r) => sum + (r.sleeve_usd || 0), 0);
  const totalSleeves = otherSleeves + (s.sleeve_usd || 0);
  const overAllocated = equity != null && totalSleeves > equity;
  const money = (n: number) => `$${Math.round(n).toLocaleString()}`;

  // Config traps that silently ruin a backtest / live run — warn, never block.
  const sizing = s.sizing_usd || 0;
  const sleeve = s.sleeve_usd || 0;
  // A second position needs another full trade to fit under the sleeve; if the
  // sleeve is under 2x the per-trade size, only ONE position can ever be open.
  const oneShotSleeve = sizing > 0 && sleeve > 0 && sleeve < sizing * 2;
  const maxConcurrent = sizing > 0 ? Math.floor(sleeve / sizing) : 0;
  const stopPct = s.params?.exit.stop_loss_pct ?? 0;
  // A tight stop held overnight is hit by normal daily noise — a swing killer.
  const tightSwingStop = !!s.swing_mode && stopPct > 0 && stopPct < 3;

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
      // Basket presets (e.g. sector rotation) carry their own ranking + count.
      rank_by: (p.rank_by ?? s.rank_by ?? "momentum_today") as StrategyRow["rank_by"],
      top_n: p.top_n ?? s.top_n ?? 10,
      params: JSON.parse(JSON.stringify(p.params)),
    });
  }

  function setEntry(key: string, value: unknown) {
    setS((cur) => ({ ...cur, params: { ...cur.params!, entry: { ...cur.params!.entry, [key]: value } } }));
  }
  function setExit(key: string, value: unknown) {
    setS((cur) => ({ ...cur, params: { ...cur.params!, exit: { ...cur.params!.exit, [key]: value } } }));
  }

  // Swing vs intraday are opposites (hold overnight vs flatten before the close),
  // so they're one choice, not two independent checkboxes. Intraday implies
  // flatten-before-close for stocks; crypto has no close, so flatten stays off.
  const tradingStyle: "swing" | "intraday" = s.swing_mode ? "swing" : "intraday";
  function setStyle(style: "swing" | "intraday") {
    setS((cur) => ({
      ...cur,
      swing_mode: style === "swing",
      params: {
        ...cur.params!,
        exit: {
          ...cur.params!.exit,
          flatten_before_close: style === "intraday" && cur.asset_class === "stock",
        },
      },
    }));
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
          Universe <InfoTip k="universe" />
          <select value={s.universe} onChange={(e) => setS({ ...s, universe: e.target.value as StrategyRow["universe"] })}>
            <option value="scanner">Scanner (today's risers)</option>
            <option value="watchlist">Watchlist only</option>
            <option value="both">Scanner + watchlist</option>
            <option value="basket">Basket (sector/theme)</option>
            <option value="custom">Specific symbols (pick your own)</option>
          </select>
        </label>
      </div>
      <p className="hint">
        This is a <strong>{s.asset_class === "crypto" ? "crypto" : "stocks"}</strong> strategy, so its universe is{" "}
        {s.asset_class === "crypto" ? "crypto" : "stocks"}-only: it draws from the{" "}
        {s.asset_class === "crypto" ? "crypto" : "stocks"} side of the scanner/watchlist and never sees{" "}
        {s.asset_class === "crypto" ? "stocks" : "crypto"}. Change <em>Asset class</em> above to trade the other.
      </p>
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

      {s.universe === "custom" && (
        <>
          <h4>Specific symbols</h4>
          <div className="field">
            {s.asset_class === "crypto" ? "Crypto pairs" : "Stocks"} to trade
            <SymbolPicker
              assetClass={s.asset_class}
              value={s.symbols ?? []}
              onChange={(syms) => setS({ ...s, symbols: syms })}
              multi
              placeholder={s.asset_class === "crypto" ? "Search: bitcoin or BTC/USD" : "Search: SPCX or a company name"}
            />
          </div>
          <p className="hint">
            The engine considers <strong>exactly these symbols</strong> each cycle (your entry and exit rules still
            apply). Good for a focused, one-off strategy — e.g. just SPCX — without building a whole basket. Only{" "}
            {s.asset_class === "crypto" ? "crypto pairs" : "stocks"} are searchable here because this is a{" "}
            {s.asset_class === "crypto" ? "crypto" : "stocks"} strategy.
          </p>
        </>
      )}

      <h4>Entry rules</h4>
      <div className="filter-grid">
        <label>
          Min gain today (%) <InfoTip k="min_day_gain" />
          <NumberField step="0.1" min="0" value={p.entry.min_day_gain_pct}
            onChange={(n) => setEntry("min_day_gain_pct", n)} />
        </label>
        <label>
          Max gain today (%, 0 = off) <InfoTip k="max_day_gain" />
          <NumberField step="0.1" min="0" value={p.entry.max_day_gain_pct ?? 0}
            onChange={(n) => setEntry("max_day_gain_pct", n)} />
        </label>
        <label>
          Min share price ($, 0 = any) <InfoTip k="share_price_band" />
          <NumberField step="any" min="0" value={p.entry.min_price ?? 0}
            onChange={(n) => setEntry("min_price", n)} />
        </label>
        <label>
          Max share price ($, 0 = none) <InfoTip k="share_price_band" />
          <NumberField step="any" min="0" value={p.entry.max_price ?? 0}
            onChange={(n) => setEntry("max_price", n)} />
        </label>
        <label className="check">
          <input type="checkbox" checked={p.entry.require_above_vwap}
            onChange={(e) => setEntry("require_above_vwap", e.target.checked)} />
          Require price above VWAP <InfoTip k="vwap" />
        </label>
        <label className="check">
          <input
            type="checkbox"
            checked={!!(p.entry.entry_window_start && p.entry.entry_window_end)}
            onChange={(e) => {
              if (e.target.checked) {
                setEntry("entry_window_start", "09:30");
                setEntry("entry_window_end", "15:30");
              } else {
                setEntry("entry_window_start", null);
                setEntry("entry_window_end", null);
              }
            }}
          />
          Limit entries to a time window (ET) <InfoTip k="entry_window" />
        </label>
        {p.entry.entry_window_start && p.entry.entry_window_end && (
          <>
            <label>
              Entry window start (ET)
              <input type="time" value={p.entry.entry_window_start}
                onChange={(e) => setEntry("entry_window_start", e.target.value || null)} />
            </label>
            <label>
              Entry window end (ET)
              <input type="time" value={p.entry.entry_window_end}
                onChange={(e) => setEntry("entry_window_end", e.target.value || null)} />
            </label>
          </>
        )}
      </div>
      {s.asset_class === "crypto" && (
        <p className="hint">
          Crypto trades 24/7, so the <strong>entry window</strong> is usually left <strong>off</strong> (uncheck
          "Limit entries to a time window") — the ET hours don't mean much for a 24-hour market. Turn it on only if
          you want to avoid, say, thin overnight liquidity.
        </p>
      )}

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
          Max holding time (hours, 0 = off) <InfoTip k="max_holding" />
          <NumberField step="1" min="0" value={p.exit.max_holding_hours}
            onChange={(n) => setExit("max_holding_hours", n)} />
        </label>
        <label className="check">
          <input type="checkbox" checked={p.exit.exit_below_vwap}
            onChange={(e) => setExit("exit_below_vwap", e.target.checked)} />
          Exit if price falls below VWAP <InfoTip k="vwap" />
        </label>
        {s.universe === "basket" && (
          <label className="check">
            <input type="checkbox" checked={!!p.exit.rotate_on_rank_dropout}
              onChange={(e) => setExit("rotate_on_rank_dropout", e.target.checked)} />
            Rotate out when it leaves the top {s.top_n} <InfoTip k="rotate_on_rank_dropout" />
          </label>
        )}
      </div>

      <h4>Sizing & safety</h4>
      <div className="filter-grid">
        <label>
          $ per trade <InfoTip k="sizing" />
          <NumberField step="any" min="10" value={s.sizing_usd!}
            onChange={(n) => setS({ ...s, sizing_usd: n })} />
        </label>
        <label>
          Sleeve budget ($) <InfoTip k="sleeve" />
          <NumberField step="any" min="10" value={s.sleeve_usd!}
            onChange={(n) => setS({ ...s, sleeve_usd: n })} />
        </label>
        <label>
          Max positions (this strategy) <InfoTip k="max_positions" />
          <NumberField step="1" min="1" max="25" value={s.max_positions!}
            onChange={(n) => setS({ ...s, max_positions: n })} />
        </label>
        <label>
          Trading style <InfoTip k="swing_mode" />
          <select value={tradingStyle} onChange={(e) => setStyle(e.target.value as "swing" | "intraday")}>
            <option value="swing">Swing — hold overnight, exit over days</option>
            <option value="intraday">
              Intraday — {s.asset_class === "stock" ? "flatten before the close, " : ""}no overnight hold
            </option>
          </select>
        </label>
        {/* The regime filter is a stocks-only gate (S&P 500 vs its 200-day MA),
            so this toggle does nothing for a crypto strategy — hide it there. */}
        {s.asset_class === "stock" && (
          <label className="check">
            <input type="checkbox" checked={s.ignore_regime}
              onChange={(e) => setS({ ...s, ignore_regime: e.target.checked })} />
            Ignore regime filter (not recommended) <InfoTip k="regime_filter" />
          </label>
        )}
      </div>

      <h4>Advanced — order fills</h4>
      <p className="hint">
        How aggressively QT prices its marketable limit orders. Defaults match the built-in behaviour; widen them if
        exits (or entries) miss fills on fast, thin movers. These affect live/paper orders only — the backtest uses its
        own spread-cost setting and assumes fills.
      </p>
      <div className="filter-grid">
        <label>
          Entry slippage (%) <InfoTip k="entry_slippage" />
          <NumberField step="0.1" min="0" max="5" value={p.entry.entry_slippage_pct ?? 0.5}
            onChange={(n) => setEntry("entry_slippage_pct", n)} />
        </label>
        <label>
          Exit slippage (%) <InfoTip k="exit_slippage" />
          <NumberField step="0.1" min="0" max="10" value={p.exit.exit_slippage_pct ?? 1}
            onChange={(n) => setExit("exit_slippage_pct", n)} />
        </label>
        <label>
          Max exit slippage (%) <InfoTip k="exit_slippage" />
          <NumberField step="0.1" min="0" max="20" value={p.exit.exit_slippage_max_pct ?? 1}
            onChange={(n) => setExit("exit_slippage_max_pct", n)} />
        </label>
      </div>
      <p className="hint">
        Set <strong>Max exit slippage</strong> above <strong>Exit slippage</strong> to enable an escalating chase: each
        time an exit misses, the sell price widens one step further below the market (up to the max) so a fast drop
        still gets out — always a limit, never a naked market order. Equal values = no escalation. The retry interval
        itself (~1 min) is a global engine setting, not per-strategy.
      </p>

      <p className={`sleeve-readout${overAllocated ? " over" : ""}`}>
        All strategy sleeves total <strong>{money(totalSleeves)}</strong>
        {equity != null ? (
          <>
            {" "}
            of your <strong>{money(equity)}</strong> Alpaca equity.
            {overAllocated && (
              <>
                {" "}
                That's more than your balance — which is fine: sleeves may overlap on purpose, so
                whichever strategy trades first draws the shared cash, and the no-leverage rail caps
                total spending at your real equity. Nothing borrows.
              </>
            )}
          </>
        ) : (
          <> across all strategies. Connect Alpaca to compare against your live balance.</>
        )}
      </p>
      {oneShotSleeve && (
        <p className="hint warn">
          ⚠ <strong>Only one position can be open at a time.</strong> Your sleeve budget ({money(sleeve)}) is less than
          twice your {money(sizing)} per trade, so a second buy would exceed the sleeve and be blocked — "Max positions"
          can't take effect. Backtests also stop trading once a losing streak leaves less cash than one full trade. Set
          the sleeve to a few times the per-trade size (e.g. {money(sizing * 5)} for ~5 concurrent positions).
        </p>
      )}
      {!oneShotSleeve && maxConcurrent > 0 && sizing > 0 && (
        <p className="hint">
          Room for about <strong>{maxConcurrent}</strong> position{maxConcurrent === 1 ? "" : "s"} at once
          ({money(sleeve)} sleeve ÷ {money(sizing)} per trade).
        </p>
      )}
      {tightSwingStop && (
        <p className="hint warn">
          ⚠ <strong>Tight stop-loss ({stopPct}%) with swing mode on.</strong> Holding overnight but bailing on a{" "}
          {stopPct}% move means normal daily noise (most stocks swing more than that intraday) will stop you out almost
          immediately — usually at a small loss. Swing stops should be wider than the symbol's typical daily move (ATR),
          often 5–8%. For a stop this tight, set <strong>Trading style</strong> to <em>Intraday</em> instead.
        </p>
      )}
      {error && <div className="error">{error}</div>}
      <div className="toolbar">
        <button>{s.id ? "Save (creates new config version)" : "Create strategy"}</button>
        <button type="button" className="small btn-ghost" onClick={onCancel}>
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
  const [equity, setEquity] = useState<number | null>(null);

  const refresh = useCallback(() => {
    getStrategies().then(setRows).catch((e: Error) => setNote(e.message));
  }, []);

  useEffect(() => {
    refresh();
    getPresets().then(setPresets);
    getBaskets().then(setBaskets).catch(() => setBaskets([]));
    getStatus()
      .then((st) => setEquity(st.broker ? Number(st.broker.equity) : null))
      .catch(() => setEquity(null));
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

  const live = (rows ?? []).filter((r) => r.enabled);
  const paused = (rows ?? []).filter((r) => !r.enabled);

  function strategyCard(r: StrategyRow) {
    return (
      <div className={`card${r.enabled ? " card-live" : ""}`} key={r.id}>
        <h3>
          {r.name}{" "}
          <span className={`pill ${r.enabled ? "ok pill-live" : "muted"}`}>
            {r.enabled ? "● ENABLED" : "disabled"}
          </span>
        </h3>
        <dl>
          <dt>Trades</dt>
          <dd>
            {r.asset_class} ·{" "}
            {r.universe === "basket"
              ? `basket "${basketName(r.basket_id)}" · top ${r.top_n} by ${RANK_LABELS[r.rank_by]}`
              : r.universe === "custom"
              ? `custom: ${r.symbols.join(", ") || "(none)"}`
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
        <div className="card-actions">
          <button
            className={`small ${r.enabled ? "btn-pause" : "btn-enable"}`}
            onClick={() => toggle(r)}
            title={r.enabled ? "Stop this strategy from opening new trades" : "Arm this strategy (it trades once the engine is on)"}
          >
            {r.enabled ? "❚❚ Pause" : "▶ Enable"}
          </button>
          <button className="small btn-ghost" onClick={() => setEditing(r)} title="Edit this strategy's settings">
            ✎ Edit
          </button>
          <button className="small danger" onClick={() => remove(r)} title="Delete this strategy permanently">
            ✕ Delete
          </button>
        </div>
      </div>
    );
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
          allStrategies={rows ?? []}
          equity={equity}
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
        <>
          {live.length > 0 && (
            <section className="strat-section">
              <h3 className="section-head">
                Enabled <span className="section-count">{live.length}</span>
                <span className="section-sub">armed — they trade once the engine is on</span>
              </h3>
              <div className="grid">{live.map(strategyCard)}</div>
            </section>
          )}
          {paused.length > 0 && (
            <section className="strat-section">
              <h3 className="section-head section-head-muted">
                Disabled / drafts <span className="section-count">{paused.length}</span>
              </h3>
              <div className="grid">{paused.map(strategyCard)}</div>
            </section>
          )}
        </>
      )}
    </>
  );
}
