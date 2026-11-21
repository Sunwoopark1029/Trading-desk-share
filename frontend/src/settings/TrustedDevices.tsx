import { useCallback, useEffect, useState } from "react";
import { ApiError, authApi, type TrustedDevice } from "../api";
import { fmtKstDateTime } from "../format";
import { useAuth } from "../Login";
import { useT, type Key } from "../i18n";

/** Cloud plan (2026-11-21): a browser that came in through Tailscale can be trusted with the PIN once.
 *  A trusted browser sends orders and arms rules like the home PC (no PIN each time). */
export default function TrustedDevices() {
  const { t } = useT();
  const auth = useAuth().status;
  const [list, setList] = useState<TrustedDevice[]>([]);
  const [name, setName] = useState("");
  const [pin, setPin] = useState("");
  const [msg, setMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const errText = (e: unknown) => {
    const code = e instanceof ApiError ? e.message : String(e);
    const known: Record<string, Key> = {
      wrong_pin: "terr.wrong_pin", locked: "terr.locked", too_many_devices: "td.err.tooMany",
      device_name_missing: "td.err.name", trust_remote_only: "td.err.remoteOnly", pin_not_set: "td.err.noPin",
    };
    return known[code] ? t(known[code]) : code;
  };
  const load = useCallback(async () => {
    try {
      setList((await authApi.devices()).devices);
    } catch (e) {
      setMsg(e instanceof Error ? e.message : String(e));
    }
  }, []);
  useEffect(() => { void load(); }, [load]);
  if (!auth?.required) return null;   // login switched off (tests)

  const changed = () => {
    window.dispatchEvent(new Event("desk-login"));   // reload the login status (trusted or not)
    void load();
  };
  const run = async (fn: () => Promise<unknown>, ok?: string) => {
    setBusy(true);
    setMsg(null);
    try {
      await fn();
      if (ok) setMsg(ok);
      changed();
    } catch (e) {
      setMsg(errText(e));
    }
    setBusy(false);
  };
  const trust = () => run(async () => { await authApi.trust(pin, name); setPin(""); }, t("td.done"));
  const remove = (d: TrustedDevice) => window.confirm(t("td.removeConfirm", { n: d.name })) && run(() => authApi.removeDevice(d.id));
  const removeAll = () => window.confirm(t("td.removeAllConfirm")) && run(() => authApi.removeAllDevices());

  return (
    <section className="panel"><div className="panel-head"><h2>{t("td.title")}</h2></div>
      <p className="muted small">{t("td.note")}</p>
      <p className="small">
        {!auth.remote ? t("td.home") : auth.trusted ? t("td.thisTrusted") : t("td.thisNot")}
      </p>
      {auth.remote && !auth.trusted && (
        <div style={{ display: "flex", gap: 6, flexWrap: "wrap", margin: "6px 0" }}>
          <input placeholder={t("td.namePh")} value={name} maxLength={40} onChange={(e) => setName(e.target.value)} />
          <input type="password" inputMode="numeric" autoComplete="off" placeholder={t("ap.pin")} value={pin}
            onChange={(e) => setPin(e.target.value)} />
          <button className="primary" disabled={busy || !name.trim() || !pin} onClick={() => void trust()}>{t("td.trust")}</button>
        </div>
      )}
      {list.length > 0 ? (
        <table className="td-list small">
          <thead><tr><th>{t("td.name")}</th><th>{t("td.added")}</th><th>{t("td.lastUsed")}</th><th /></tr></thead>
          <tbody>
            {list.map((d) => (
              <tr key={d.id}>
                <td>{d.name}{d.current && <span className="muted"> · {t("td.thisDevice")}</span>}</td>
                <td>{fmtKstDateTime(d.added)}</td>
                <td>{fmtKstDateTime(d.last_used)}</td>
                <td><button className="small" disabled={busy} onClick={() => void remove(d)}>{t("td.remove")}</button></td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : <p className="muted small">{t("td.none")}</p>}
      {list.length > 1 && <button className="small" disabled={busy} onClick={() => void removeAll()}>{t("td.removeAll")}</button>}
      {msg && <div className="small">{msg}</div>}
    </section>
  );
}
