import type { Status } from "../api";
import { fmtAge } from "../format";
import { trBackend, useT } from "../i18n";

interface Props {
  status: Status | string | null | undefined;
  updated: string | null | undefined;
  error?: string | null;
  now: number;
  label?: string; // e.g. "Toss · real-time"
  fetchError?: string | null; // the browser could not reach the backend
}

/** "Toss · real-time · updated 12 s ago" with stale / error marking. */
export default function StatusLine({ status, updated, error, now, label, fetchError }: Props) {
  const { t } = useT();
  const st = fetchError ? "error" : status ?? "pending";
  const cls = st === "ok" ? "st-ok" : st === "stale" ? "st-stale" : st === "pending" ? "st-pending" : "st-error";
  return (
    <span className={`status ${cls}`} title={trBackend(error ?? fetchError) || undefined}>
      {label && <span>{label} · </span>}
      {st === "pending" ? t("st.loading") : t("st.updated", { age: fmtAge(updated, now) })}
      {st === "stale" && <b> · {t("st.stale")}</b>}
      {(st === "error" || st === "missing") && <b> · {trBackend(fetchError ?? error ?? st)}</b>}
    </span>
  );
}
