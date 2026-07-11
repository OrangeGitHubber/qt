import { FormEvent, useState } from "react";
import { saveAlpacaKeys } from "../api";

export default function Setup({ onDone }: { onDone: () => void }) {
  const [keyId, setKeyId] = useState("");
  const [keySecret, setKeySecret] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await saveAlpacaKeys(keyId.trim(), keySecret.trim());
      onDone();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="card setup">
      <h2>Welcome — let's connect Alpaca (paper trading)</h2>
      <ol className="steps">
        <li>
          Create a free account at <a href="https://alpaca.markets" target="_blank" rel="noreferrer">alpaca.markets</a>{" "}
          (a paper-only account needs just an email).
        </li>
        <li>
          In the Alpaca dashboard, make sure the toggle says <strong>Paper</strong>, then generate an API key pair.
        </li>
        <li>Paste both values below. They are verified against Alpaca, then stored encrypted on your server.</li>
      </ol>
      <form onSubmit={submit}>
        <label>
          API Key ID
          <input value={keyId} onChange={(e) => setKeyId(e.target.value)} autoComplete="off" required />
        </label>
        <label>
          API Secret Key
          <input
            type="password"
            value={keySecret}
            onChange={(e) => setKeySecret(e.target.value)}
            autoComplete="off"
            required
          />
        </label>
        {error && <div className="error">{error}</div>}
        <button disabled={busy}>{busy ? "Verifying with Alpaca…" : "Verify & save"}</button>
      </form>
      <p className="hint">
        Only paper (simulated) trading is possible in this version. Live trading arrives in a later phase, behind
        additional safeguards.
      </p>
    </div>
  );
}
