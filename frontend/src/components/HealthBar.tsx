import { useRef, useState } from "react";
import { api } from "../api";
import { trBackend, useT } from "../i18n";
import { usePoll } from "../usePoll";

/** One line of problems only (missing keys, IP not allowed, token replaced, stale sources). */
export default function HealthBar() {
  const { t } = useT();
  const h = usePoll(api.health, 15_000);
  const [msg, setMsg] = useState<string | null>(null);
  const lastOk = useRef<number | null>(null);
  if (h.data && !h.error) lastOk.current = Date.now();

  if (h.error) {
    const at = lastOk.current
      ? new Intl.DateTimeFormat("en-GB", { timeZone: "Asia/Seoul", hour: "2-digit", minute: "2-digit", second: "2-digit" })
        .format(lastOk.current) + " KST"
      : "—";
    return <div className="health st-error">{t("health.lost", { t: at })} ({trBackend(h.error)})</div>;
  }
  const d = h.data;
  if (!d) return null;
  // development copy (config deploy.role: workshop): always say so, above any other problem line
  const workshop = d.deploy_role === "workshop"
    ? <div className="health st-workshop">{t("health.workshop")}</div> : null;

  const problems: string[] = [];
  const optional = new Set(d.optional_keys ?? []);
  const missing = Object.entries(d.keys).filter(([k, v]) => v !== "set" && !optional.has(k)).map(([k]) => k);
  if (missing.length) problems.push(t("health.notSet", { keys: missing.join(", ") }));
  const seen = new Set<string>();
  for (const [name, s] of Object.entries(d.sources)) {
    if (s.status !== "error" && s.status !== "stale") continue;
    if (name.startsWith("toss.candles.") && s.status === "stale") continue; // on demand: idle when no chart shows it
    if (s.error_code === "no-key") continue; // already listed above
    const text = `${name}: ${s.status === "stale" ? t("st.stale") : trBackend(s.error ?? s.error_code ?? "error")}`;
    const dedupe = s.error ?? text;
    if (seen.has(dedupe)) continue;
    seen.add(dedupe);
    problems.push(text);
  }
  if (!problems.length && !d.toss.revoked) return workshop;

  const retry = async () => {
    try {
      setMsg((await api.tossRetry()).message);
      h.refresh();
    } catch (e) {
      setMsg(e instanceof Error ? e.message : String(e));
    }
  };

  return (
    <>{workshop}
    <div className="health st-stale">
      {problems.join(" · ")}
      {d.toss.revoked && (
        <>
          {" "}<button className="small" onClick={retry}>{t("health.retry")}</button>
        </>
      )}
      {msg && <span> · {trBackend(msg)}</span>}
    </div>
    </>
  );
}
