import { useEffect, useState } from "react";

/** Remembered column choice for one table, persisted per browser.
 *
 *  Shared because three tables now offer this and a third bespoke copy is how
 *  they start disagreeing about small things — whether the choice survives a
 *  reload, what happens when a stored key no longer exists, whether hiding
 *  everything is allowed.
 *
 *  Unknown stored keys are dropped rather than trusted: a column removed in an
 *  update would otherwise sit in localStorage forever, and a renamed one would
 *  silently hide itself for anyone who had ever touched the menu.
 */
export function useColumnPrefs<K extends string>(
  storageKey: string,
  all: readonly K[],
  defaults: readonly K[] = all,
): [Set<K>, (k: K) => void] {
  const [visible, setVisible] = useState<Set<K>>(() => {
    try {
      const raw = localStorage.getItem(storageKey);
      if (raw) {
        const keys = (JSON.parse(raw) as string[]).filter((k) => (all as readonly string[]).includes(k)) as K[];
        return new Set(keys);
      }
    } catch {
      /* corrupt or unavailable storage — fall through to the defaults */
    }
    return new Set(defaults);
  });

  useEffect(() => {
    try {
      localStorage.setItem(storageKey, JSON.stringify([...visible]));
    } catch {
      /* private mode etc. — the session still works, it just won't be remembered */
    }
  }, [storageKey, visible]);

  return [
    visible,
    (k: K) =>
      setVisible((cur) => {
        const next = new Set(cur);
        if (next.has(k)) next.delete(k);
        else next.add(k);
        return next;
      }),
  ];
}

/** The "Columns ▾" menu itself. */
export default function ColumnPicker<K extends string>({
  columns,
  visible,
  onToggle,
}: {
  columns: readonly { key: K; label: string }[];
  visible: Set<K>;
  onToggle: (k: K) => void;
}) {
  return (
    <details className="cols-menu">
      <summary className="small btn-ghost" title="Choose which columns to show">
        Columns
      </summary>
      <div className="cols-popover" role="group" aria-label="Choose columns">
        {columns.map((c) => (
          <label key={c.key} className="check">
            <input type="checkbox" checked={visible.has(c.key)} onChange={() => onToggle(c.key)} />
            {c.label}
          </label>
        ))}
      </div>
    </details>
  );
}
