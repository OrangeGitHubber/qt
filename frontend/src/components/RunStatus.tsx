import { useEffect, useRef, useState } from "react";

/** "Still working" line for anything long-running: a spinner, the phase the
 *  server reports, an optional percentage, and a clock.
 *
 *  Shared rather than copied, because the backtest and the two optimizer jobs
 *  had drifted into three different answers to the same question — one counted
 *  seconds, one drew a bar, one just disabled its button — and a frozen button
 *  is indistinguishable from a broken one whichever page you're on.
 *
 *  `startedAt` (server ISO) is preferred over a local timer: these jobs run in
 *  the background, so a page reload mid-run would otherwise restart the clock at
 *  zero and report a five-minute sweep as ten seconds old.
 */
export default function RunStatus({
  running,
  phase,
  pct,
  startedAt,
}: {
  running: boolean;
  phase?: string | null;
  pct?: number | null;
  startedAt?: string | null;
}) {
  const [now, setNow] = useState(() => Date.now());
  // Fallback origin for callers with no server timestamp — set once per run.
  const localStart = useRef<number | null>(null);

  useEffect(() => {
    if (!running) {
      localStart.current = null;
      return;
    }
    localStart.current = Date.now();
    setNow(Date.now());
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, [running]);

  if (!running) return null;

  const parsed = startedAt ? Date.parse(startedAt) : NaN;
  const startMs = Number.isNaN(parsed) ? (localStart.current ?? now) : parsed;
  const secs = Math.max(0, Math.round((now - startMs) / 1000));
  const since = secs >= 60 ? `${Math.floor(secs / 60)}m ${secs % 60}s` : `${secs}s`;

  return (
    <span className="run-status" role="status" aria-live="polite">
      <span className="spinner" aria-hidden="true" />
      {phase || "Starting…"}
      {pct != null && ` ${pct}%`}
      <span className="hint"> · {since}</span>
    </span>
  );
}
