import { useCallback, useEffect, useState } from "react";
import { AuthState, getAuthState, getStatus, logout, StatusResponse } from "./api";
import About from "./pages/About";
import { AuthBootstrap, Login } from "./pages/AuthGate";
import Backtest from "./pages/Backtest";
import Dashboard from "./pages/Dashboard";
import Journal from "./pages/Journal";
import Scanner from "./pages/Scanner";
import Settings from "./pages/Settings";
import Setup from "./pages/Setup";
import Strategies from "./pages/Strategies";
import Watchlist from "./pages/Watchlist";

type Tab = "dashboard" | "scanner" | "watchlist" | "strategies" | "backtest" | "journal" | "settings" | "about";

export default function App() {
  const [auth, setAuth] = useState<AuthState | null>(null);
  const [status, setStatus] = useState<StatusResponse | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [tab, setTab] = useState<Tab>("dashboard");

  const refreshAuth = useCallback(() => {
    getAuthState()
      .then((a) => {
        setAuth(a);
        setLoadError(null);
      })
      .catch((e: Error) => setLoadError(e.message));
  }, []);

  const refresh = useCallback(() => {
    getStatus()
      .then((s) => {
        setStatus(s);
        setLoadError(null);
      })
      .catch((e: Error) => setLoadError(e.message));
  }, []);

  const signedIn = !!auth?.email;

  useEffect(() => {
    refreshAuth();
  }, [refreshAuth]);

  useEffect(() => {
    if (!signedIn) return;
    refresh();
    const timer = setInterval(refresh, 30_000);
    return () => clearInterval(timer);
  }, [refresh, signedIn]);

  if (loadError) {
    return (
      <div className="shell">
        <div className="card error">Backend unreachable: {loadError}</div>
      </div>
    );
  }
  if (!auth) {
    return (
      <div className="shell">
        <div className="card">Loading…</div>
      </div>
    );
  }
  if (!auth.configured && !auth.auth_disabled) {
    return (
      <div className="shell">
        <header>
          <h1>
            QT <span className="subtitle">Auto-Trader</span>
          </h1>
        </header>
        <AuthBootstrap state={auth} onDone={refreshAuth} />
      </div>
    );
  }
  if (!signedIn) {
    return (
      <div className="shell">
        <header>
          <h1>
            QT <span className="subtitle">Auto-Trader</span>
          </h1>
        </header>
        <Login />
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
      {status.data_persistent === false && (
        <div className="persist-banner" role="alert">
          <strong>⚠ Your data directory is not persistent.</strong> Configuration, keys and
          trade history will be lost the next time this container updates. Fix the{" "}
          <code>/data</code> volume mapping —{" "}
          <a
            href="https://github.com/OrangeGitHubber/qt/blob/main/docs/data-persistence.md"
            target="_blank"
            rel="noreferrer"
          >
            how to fix this
          </a>
          .
        </div>
      )}
      {status.secrets_without_key && (
        <div className="persist-banner" role="alert">
          <strong>⚠ Saved API keys can't be decrypted.</strong> The database has encrypted
          secrets but <code>instance.key</code> is missing. Restore the original key file, or
          re-enter your Alpaca keys in Setup.
        </div>
      )}
      <header>
        <h1>
          QT <span className="subtitle">Auto-Trader</span>
        </h1>
        <span className={`mode-badge mode-${status.trading_mode}`}>
          {status.trading_mode === "paper" ? "PAPER MODE — no real money" : status.trading_mode.toUpperCase()}
        </span>
        {auth.auth_disabled && <span className="pill warn">AUTH DISABLED (dev)</span>}
        <span className="userbox">
          {auth.email}{" "}
          <button
            className="small"
            onClick={() => logout().then(() => window.location.reload())}
          >
            Sign out
          </button>
        </span>
      </header>
      {status.alpaca_configured ? (
        <>
          <nav className="tabs">
            {(["dashboard", "scanner", "watchlist", "strategies", "backtest", "journal", "settings", "about"] as Tab[]).map((t) => (
              <button key={t} className={tab === t ? "tab active" : "tab"} onClick={() => setTab(t)}>
                {t.charAt(0).toUpperCase() + t.slice(1)}
              </button>
            ))}
          </nav>
          {tab === "dashboard" && <Dashboard status={status} onRefresh={refresh} />}
          {tab === "scanner" && <Scanner />}
          {tab === "watchlist" && <Watchlist />}
          {tab === "strategies" && <Strategies />}
          {tab === "backtest" && <Backtest />}
          {tab === "journal" && <Journal />}
          {tab === "settings" && <Settings />}
          {tab === "about" && <About />}
        </>
      ) : (
        <Setup onDone={refresh} />
      )}
      <footer>v{status.version} · GPLv3 · paper-first by design</footer>
    </div>
  );
}
