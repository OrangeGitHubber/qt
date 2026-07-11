import { useEffect, useState } from "react";
import { getBars } from "../api";

export default function Sparkline({ symbol, assetClass }: { symbol: string; assetClass: string }) {
  const [points, setPoints] = useState<number[] | null>(null);

  useEffect(() => {
    let cancelled = false;
    getBars(symbol, assetClass)
      .then((d) => {
        if (!cancelled) setPoints(d.bars.map((b) => b.c));
      })
      .catch(() => {
        if (!cancelled) setPoints([]);
      });
    return () => {
      cancelled = true;
    };
  }, [symbol, assetClass]);

  if (points === null) return <span className="spark-empty">…</span>;
  if (points.length < 2) return <span className="spark-empty">no data</span>;

  const w = 120;
  const h = 28;
  const min = Math.min(...points);
  const max = Math.max(...points);
  const span = max - min || 1;
  const step = w / (points.length - 1);
  const path = points
    .map((p, i) => `${i === 0 ? "M" : "L"}${(i * step).toFixed(1)},${(h - ((p - min) / span) * h).toFixed(1)}`)
    .join(" ");
  const up = points[points.length - 1] >= points[0];

  return (
    <svg width={w} height={h} className="spark" aria-label={`${symbol} recent trend`}>
      <path d={path} fill="none" stroke={up ? "var(--ok)" : "var(--err)"} strokeWidth="1.5" />
    </svg>
  );
}
