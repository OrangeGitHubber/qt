import { useCallback, useEffect, useState } from "react";
import { getStatus, StatusResponse } from "./api";
import Dashboard from "./pages/Dashboard";
import Setup from "./pages/Setup";

export default function App() {
  const [status, setStatus] = useState<StatusResponse | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  const refresh = useCallback(() => {
    getStatus()
      .then((s) => {
        setStatus(s);
        setLoadError(null);
      })
      .catch((e: Error) => setLoadError(e.message));
  }, []);

  useEffect(() => {
    refresh();
    const timer = setInterval(refresh, 30_000);
    return () => clearInterval(timer);
  }, [refresh]);

  if (loadError) {
    return (
      <div className="shell">
        <div className="card error">Backend unreachable: {loadError}</div>
      </div>
    );
  }
  if (!status) {
    return (
      <div className="shell">
        <div className="card">Loading…</div>
      </div>
    );
  }
  return (
    <div className="shell">
      <header>
        <h1>
          QT <span className="subtitle">Auto-Trader</span>
        </h1>
        <span className={`mode-badge mode-${status.trading_mode}`}>
          {status.trading_mode === "paper" ? "PAPER MODE — no real money" : status.trading_mode.toUpperCase()}
        </span>
      </header>
      {status.alpaca_configured ? (
        <Dashboard status={status} onRefresh={refresh} />
      ) : (
        <Setup onDone={refresh} />
      )}
      <footer>v{status.version} · GPLv3 · paper-first by design</footer>
    </div>
  );
}
