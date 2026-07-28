import { useEffect, useState } from "react";
import { AccountsResponse, getAccounts } from "../api";

const short = (id: string | null) => (id ? `…${id.slice(-4)}` : "earlier");

/** Broker-account picker for the journal / P&L views. Value "" = the current
 *  account (the default), "all" = every account, "untagged" = the legacy
 *  pre-tagging trades, or a specific account id. Renders nothing when there's
 *  only the current account (nothing to choose). */
export default function AccountSelect({ value, onChange }: { value: string; onChange: (v: string) => void }) {
  const [data, setData] = useState<AccountsResponse | null>(null);
  useEffect(() => {
    getAccounts().then(setData).catch(() => setData(null));
  }, []);

  const opts = data?.accounts ?? [];
  if (!opts.some((a) => !a.is_current)) return null; // only the current account — no picker needed

  return (
    <label className="account-select">
      Account
      <select value={value} onChange={(e) => onChange(e.target.value)}>
        <option value="">This account{data?.current ? ` (${short(data.current)})` : ""}</option>
        {opts
          .filter((a) => !a.is_current)
          .map((a) => (
            <option key={a.id ?? "untagged"} value={a.untagged ? "untagged" : a.id ?? ""}>
              {a.untagged ? "Earlier / untagged" : short(a.id)} ({a.trades})
            </option>
          ))}
        <option value="all">All accounts</option>
      </select>
    </label>
  );
}
