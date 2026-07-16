import { useEffect, useRef, useState } from "react";
import { AssetRow, searchAssets } from "../api";

/** Type-ahead over the local symbol directory — matches ticker OR company
 *  name. `multi` mode keeps a chip list; single mode returns one symbol. */
export default function SymbolPicker({
  assetClass,
  value,
  onChange,
  multi = false,
  placeholder,
}: {
  assetClass?: "stock" | "crypto";
  value: string[];
  onChange: (symbols: string[]) => void;
  multi?: boolean;
  placeholder?: string;
}) {
  const [q, setQ] = useState("");
  const [rows, setRows] = useState<AssetRow[]>([]);
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!q.trim()) {
      setRows([]);
      return;
    }
    let cancelled = false;
    const timer = setTimeout(() => {
      searchAssets(q, assetClass)
        .then((r) => {
          if (!cancelled) {
            setRows(r);
            setActive(0);
            setOpen(true);
          }
        })
        .catch(() => setRows([]));
    }, 150); // debounce keystrokes
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [q, assetClass]);

  useEffect(() => {
    const onDown = (e: PointerEvent) => {
      if (!ref.current?.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("pointerdown", onDown);
    return () => document.removeEventListener("pointerdown", onDown);
  }, []);

  function pick(row: AssetRow) {
    onChange(multi ? Array.from(new Set([...value, row.symbol])) : [row.symbol]);
    setQ("");
    setRows([]);
    setOpen(false);
  }

  function onKeyDown(e: React.KeyboardEvent) {
    if (!open || rows.length === 0) return;
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setActive((a) => Math.min(a + 1, rows.length - 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setActive((a) => Math.max(a - 1, 0));
    } else if (e.key === "Enter") {
      e.preventDefault();
      pick(rows[active]);
    } else if (e.key === "Escape") {
      setOpen(false);
    }
  }

  return (
    <div className="picker" ref={ref}>
      {multi && value.length > 0 && (
        <div className="chips">
          {value.map((s) => (
            <span className="chip" key={s}>
              {s}
              <button
                type="button"
                onClick={(e) => {
                  e.preventDefault();
                  onChange(value.filter((x) => x !== s));
                }}
                aria-label={`Remove ${s}`}
              >
                ×
              </button>
            </span>
          ))}
        </div>
      )}
      <input
        value={q}
        onChange={(e) => setQ(e.target.value)}
        onKeyDown={onKeyDown}
        onFocus={() => rows.length && setOpen(true)}
        placeholder={placeholder ?? (assetClass === "crypto" ? "Search: bitcoin or BTC/USD" : "Search: nvidia or NVDA")}
        autoComplete="off"
      />
      {!multi && value.length > 0 && !q && <div className="picked">Selected: {value[0]}</div>}
      {open && rows.length > 0 && (
        <ul className="picker-pop">
          {rows.map((r, i) => (
            <li
              key={`${r.asset_class}:${r.symbol}`}
              className={i === active ? "active" : ""}
              onMouseEnter={() => setActive(i)}
              // preventDefault stops a wrapping <label> from re-dispatching this
              // click to the first control inside it — which, once chips exist,
              // is a chip's remove button, silently deleting a selection.
              onClick={(e) => {
                e.preventDefault();
                pick(r);
              }}
            >
              <span className="sym">{r.symbol}</span>
              <span className="asset-name">{r.name}</span>
              <span className="hint">
                {r.exchange}
                {r.asset_class === "stock" && !r.fractionable ? " · whole shares only" : ""}
              </span>
            </li>
          ))}
        </ul>
      )}
      {open && q.trim() && rows.length === 0 && (
        <ul className="picker-pop">
          <li className="empty">
            No match for "{q}". If the directory is empty, sync it in Settings.
          </li>
        </ul>
      )}
    </div>
  );
}
