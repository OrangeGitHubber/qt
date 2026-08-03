import { useCallback, useEffect, useState } from "react";
import { AuthState, getAuthState, getStatus, logout, StatusResponse } from "./api";
import About from "./pages/About";
import { AuthBootstrap, Login } from "./pages/AuthGate";
import Backtest from "./pages/Backtest";
import Baskets from "./pages/Baskets";
import Dashboard from "./pages/Dashboard";
import Journal from "./pages/Journal";
import Optimizer from "./pages/Optimizer";
import Scanner from "./pages/Scanner";
import Settings from "./pages/Settings";
import Setup from "./pages/Setup";
import Strategies from "./pages/Strategies";
import Watchlist from "./pages/Watchlist";
import { IconWarn } from "./components/icons";
import { setDisplayZone, useDisplayZone } from "./lib/datetime";
import { onNav } from "./lib/nav";

type Tab =
  | "dashboard"
  | "scanner"
  | "watchlist"
  | "baskets"
  | "strategies"
  | "backtest"
  | "optimizer"
  | "journal"
  | "settings"
  | "about";

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
        setDisplayZone(s.display_timezone);
        setLoadError(null);
      })
      .catch((e: Error) => setLoadError(e.message));
  }, []);

  // Subscribing to the display zone HERE, at the root, is what makes saving it
  // in Settings redraw every timestamp on every page at once — the formatters
  // themselves read a module-level value, so nothing below needs to know.
  useDisplayZone();

  const signedIn = !!auth?.email;

  useEffect(() => {
    refreshAuth();
  }, [refreshAuth]);

  // Cross-page jumps (e.g. a strategy row's "Optimize" button) land here.
  useEffect(() => onNav((r) => setTab(r.tab as Tab)), []);

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
          <IconWarn className="icon-inline" /> <strong>Your data directory is not persistent.</strong> Configuration, keys and
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
          <IconWarn className="icon-inline" /> <strong>Saved API keys can't be decrypted.</strong> The database has encrypted
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
            {/* Journal sits second, beside Dashboard: together they're "what is happening"
                and "what happened" — the two you check most and flip between. The rest
                are configuration and research, visited far less often. */}
            {(["dashboard", "journal", "scanner", "watchlist", "baskets", "strategies", "backtest", "optimizer", "settings", "about"] as Tab[]).map((t) => (
              <button key={t} className={tab === t ? "tab active" : "tab"} onClick={() => setTab(t)}>
                {t.charAt(0).toUpperCase() + t.slice(1)}
              </button>
            ))}
          </nav>
          {tab === "dashboard" && <Dashboard status={status} onRefresh={refresh} />}
          {tab === "scanner" && <Scanner />}
          {tab === "watchlist" && <Watchlist />}
          {tab === "baskets" && <Baskets />}
          {tab === "strategies" && <Strategies />}
          {tab === "backtest" && <Backtest />}
          {tab === "optimizer" && <Optimizer />}
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
