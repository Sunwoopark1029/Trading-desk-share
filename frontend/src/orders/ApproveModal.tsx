import { useEffect, useState } from "react";
import { ApiError, orderApi, type DraftView, type SubmitResult } from "../api";
import { fmtNum } from "../format";
import { useT, type Key } from "../i18n";
import { useAuth } from "../Login";
import { issueText } from "./OrderTicket";

interface Props {
  draft: DraftView;
  onClose: () => void;
  onDone: () => void;
}

const FIELDS: [string, Key][] = [
  ["symbol", "tk.symbol"], ["side", "ord.side"], ["order_type", "ord.type"], ["price", "tk.price"],
  ["quantity", "tk.qty"], ["amount", "tk.amount"], ["time_in_force", "tk.tif"], ["expire_date", "tk.expire"],
];

export function resultText(t: (k: Key, v?: Record<string, string | number>) => string, r: SubmitResult): string {
  if (r.status === "simulated") return t("ap.simulated", { id: r.order_id ?? r.conditional_order_id ?? "" });
  if (r.status === "accepted") return t("ap.accepted", { id: r.order_id ?? r.conditional_order_id ?? "" });
  if (r.issues?.length) return r.issues.map((i) => issueText(t, i)).join(" · ");
  const key = `terr.${r.error}` as Key;
  const s = t(key);
  return s === key ? t("ap.failed", { code: r.error ?? r.status }) : s;
}

/** The approval screen: big summary, all fields, estimate, then ONE press sends (once). */
export default function ApproveModal({ draft, onClose, onDone }: Props) {
  const { t, lang } = useT();
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<SubmitResult | null>(null);
  const [left, setLeft] = useState(draft.seconds_left);
  const auth = useAuth().status;
  const phone = !!auth?.remote && !auth?.trusted;   // trusted browser: home-PC rules (cloud plan)
  const [pin, setPin] = useState("");

  useEffect(() => {
    const id = window.setInterval(() => {
      setLeft(Math.max(0, Math.round((new Date(draft.expires).getTime() - Date.now()) / 1000)));
    }, 500);
    return () => window.clearInterval(id);
  }, [draft.expires]);

  const send = async () => {
    setBusy(true);
    try {
      setResult(await orderApi.submit(draft.id, phone ? pin : undefined));
      setPin("");
    } catch (e) {
      const body = (e as ApiError & { body?: SubmitResult }).body;
      setResult(body ?? { ok: false, status: "error", error: e instanceof Error ? e.message : String(e) });
    } finally {
      onDone();
    }
  };

  const v = draft.values as Record<string, unknown>;
  const legs = (v.legs as { side: string; trigger_price: string | null; order_price: string | null }[] | undefined) ?? [];
  const e = draft.estimate;
  const real = !draft.dry_run;
  const market = String(v.order_type ?? "").toUpperCase() === "MARKET";
  const phoneBlock = !phone ? null : !auth?.remote_orders ? t("ap.phoneOff") : market && auth.block_market ? t("ap.phoneMarket") : null;
  return (
    <div className="modal-back" onClick={(ev) => ev.target === ev.currentTarget && !busy && onClose()}>
      <div className={`modal approve${phone ? " big" : ""}`} role="dialog" aria-modal="true">
        <div className={`dry-banner ${real ? "live" : ""}`}>{real ? t("ap.real") : t("ap.dry")}</div>
        <div className="ap-summary">{lang === "ko" ? draft.summary_ko : draft.summary_en}</div>
        <table className="otable ap-fields">
          <tbody>
            {FIELDS.filter(([k]) => v[k] !== null && v[k] !== undefined && v[k] !== "").map(([k, label]) => (
              <tr key={k}><th>{t(label)}</th><td>{String(v[k])}{k === "price" || k === "amount" ? " USD" : ""}</td></tr>
            ))}
            {legs.map((l, i) => (
              <tr key={i}><th>{t("tk.trigger")} {i + 1}</th>
                <td>{l.side} · {l.trigger_price} USD → {l.order_price ? `${l.order_price} USD` : t("tk.market")}</td></tr>
            ))}
            {e.notional_usd && (
              <tr><th>{t("dr.est")}</th><td>
                USD {fmtNum(e.notional_usd, 2)} · {t("dr.fee", { f: `USD ${e.fee_usd_est}` })} · {t("dr.total", { t: `USD ${fmtNum(e.total_usd_est, 2)}` })}
                {e.krw_ref && <div className="muted small">{t("dr.krw", { k: fmtNum(e.krw_ref, 0), r: fmtNum(e.fx_mid, 2) })}</div>}
                {e.price_is_estimate && <div className="muted small">{t("dr.priceEst")}</div>}
              </td></tr>
            )}
          </tbody>
        </table>
        {draft.warnings.map((i) => <div key={i.code} className="st-stale small">⚠ {issueText(t, i)}</div>)}
        <div className="muted small">{t("ap.recheck")}</div>

        {result ? (
          <div className={`ap-result ${result.ok ? "ok" : "bad"}`}>
            {resultText(t, result)}
            <div><button onClick={onClose}>{t("ap.close")}</button></div>
          </div>
        ) : (
          <div className="ap-actions">
            {phoneBlock && <div className="st-stale ap-phone">{phoneBlock}</div>}
            {phone && !phoneBlock && (
              <input className="ap-pin" type="password" inputMode="numeric" autoComplete="off" value={pin}
                onChange={(e) => setPin(e.target.value.replace(/\D/g, "").slice(0, 8))} placeholder={t("ap.pin")} />
            )}
            <button className={`ap-send ${real ? "real" : ""}`} onClick={() => void send()}
              disabled={busy || left <= 0 || !!phoneBlock || (phone && pin.length < 4)}>
              {real ? t("ap.sendReal") : t("ap.sendDry")}
            </button>
            <button onClick={onClose} disabled={busy}>{t("ap.cancel")}</button>
            <span className="small muted">{left > 0 ? t("dr.expiresIn", { s: left }) : t("ap.expired")}</span>
          </div>
        )}
      </div>
    </div>
  );
}
