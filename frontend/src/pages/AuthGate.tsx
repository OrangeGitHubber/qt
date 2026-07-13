import { FormEvent, useState } from "react";
import { AuthState, bootstrapAuth } from "../api";

export function AuthBootstrap({ state, onDone }: { state: AuthState; onDone: () => void }) {
  const [clientId, setClientId] = useState("");
  const [clientSecret, setClientSecret] = useState("");
  const [email, setEmail] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await bootstrapAuth(clientId.trim(), clientSecret.trim(), email.trim());
      onDone();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="card setup">
      <h2>Secure this app with Google Sign-In</h2>
      <p className="hint">
        QT will hold trading API keys, so the whole interface sits behind Google login. One-time setup (~5 minutes):
      </p>
      <ol className="steps">
        <li>
          Open the{" "}
          <a href="https://console.cloud.google.com/apis/credentials" target="_blank" rel="noreferrer">
            Google Cloud console → Credentials
          </a>{" "}
          (any Google account; create a project if asked).
        </li>
        <li>
          "Create credentials" → <strong>OAuth client ID</strong> → type <strong>Web application</strong>. If prompted
          first, configure the consent screen: user type <em>External</em>, then add your own email as a test user.
        </li>
        <li>
          Under <em>Authorized redirect URIs</em> add exactly: <code>{state.redirect_uri}</code>
        </li>
        <li>Copy the Client ID and Client Secret below.</li>
      </ol>
      <form onSubmit={submit}>
        <label>
          Client ID
          <input value={clientId} onChange={(e) => setClientId(e.target.value)} required autoComplete="off" />
        </label>
        <label>
          Client Secret
          <input
            type="password"
            value={clientSecret}
            onChange={(e) => setClientSecret(e.target.value)}
            required
            autoComplete="off"
          />
        </label>
        <label>
          Your Google account email (becomes the owner)
          <input type="email" value={email} onChange={(e) => setEmail(e.target.value)} required />
        </label>
        {error && <div className="error">{error}</div>}
        <button disabled={busy}>{busy ? "Saving…" : "Save & continue to sign-in"}</button>
      </form>
      <p className="hint">
        The Client Secret is stored encrypted on your server. Add more allowed accounts (e.g. your brother) later in
        Settings.
      </p>
    </div>
  );
}

export function Login() {
  const denied = new URLSearchParams(window.location.search).has("denied");
  return (
    <div className="card setup login">
      <h2>Sign in</h2>
      {denied && (
        <div className="error">
          That Google account isn't on the allowlist. Ask the owner to add it in Settings, then try again.
        </div>
      )}
      <p className="hint">QT is locked to approved Google accounts.</p>
      <a className="button-link" href="/api/auth/login">
        Sign in with Google
      </a>
    </div>
  );
}
