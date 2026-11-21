import { useEffect, useState } from "react";
import { orderApi, type DraftView, type SubmitResult } from "../api";
import ApproveModal, { resultText } from "./ApproveModal";
import { fmtNum } from "../format";
import { useT } from "../i18n";
import { issueText } from "./OrderTicket";

interface Props {
  drafts: DraftView[];
  done: (DraftView & { result: SubmitResult | null })[];
  onChanged: () => void;
}

/** Drafts with summary, problems, estimate and expiry countdown. Approve & send arrives in part 3/4. */
export default function Drafts({ drafts, done, onChanged }: Props) {
  const { t, lang } = useT();
  const [, tick] = useState(0);
  const [approving, setApproving] = useState<DraftView | null>(null);
  useEffect(() => {
    const id = window.setInterval(() => tick((n) => n + 1), 1000);
    return () => window.clearInterval(id);
  }, []);

  const left = (d: DraftView) => Math.max(0, Math.round((new Date(d.expires).getTime() - Date.now()) / 1000));

  return (
    <section className="panel">
      <div className="panel-head"><h2>{t("dr.title")}</h2></div>
      {drafts.length === 0 && <div className="muted small">{t("dr.none")}</div>}
      {drafts.map((d) => {
        const e = d.estimate;
        const secs = left(d);
        return (
          <div key={d.id} className={`draft ${d.status}`}>
            <div className="draft-summary">{lang === "ko" ? d.summary_ko : d.summary_en}</div>
            <div className="small muted">
              {d.dry_run && <span className="tag">DRY RUN</span>} {t("dr.expiresIn", { s: secs })}
              {" · "}{d.source}
            </div>
            {d.errors.map((i) => <div key={i.code + JSON.stringify(i.params)} className="st-error small">✗ {issueText(t, i)}</div>)}
            {d.warnings.map((i) => <div key={i.code} className="st-stale small">⚠ {issueText(t, i)}</div>)}
            {e.notional_usd && (
              <div className="small">
                {t("dr.est")}: {t("dr.notional", { v: `USD ${fmtNum(e.notional_usd, 2)}` })}
                {e.fee_usd_est && ` · ${t("dr.fee", { f: `USD ${e.fee_usd_est}` })}`}
                {e.total_usd_est && ` · ${t("dr.total", { t: `USD ${fmtNum(e.total_usd_est, 2)}` })}`}
                {e.krw_ref && <span className="muted"> {t("dr.krw", { k: fmtNum(e.krw_ref, 0), r: fmtNum(e.fx_mid, 2) })}</span>}
                {e.price_is_estimate && <span className="muted"> · {t("dr.priceEst")}</span>}
              </div>
            )}
            {d.status === "invalid" && <div className="small st-error">{t("dr.invalid")}</div>}
            <div className="draft-actions">
              {d.status === "draft" && secs > 0 && (
                <button className="ap-open" onClick={() => setApproving(d)}>{t("ap.review")}</button>
              )}
              <button className="small" onClick={() => void orderApi.discard(d.id).then(onChanged)}>{t("dr.discard")}</button>
            </div>
          </div>
        );
      })}
      {done.length > 0 && (
        <>
          <div className="muted small sent-title">{t("ap.sentTitle")}</div>
          {done.map((d) => (
            <div key={d.id} className={`draft sent ${d.result?.ok ? "" : "invalid"}`}>
              <div>{lang === "ko" ? d.summary_ko : d.summary_en}</div>
              {d.result && <div className={`small ${d.result.ok ? "up" : "st-error"}`}>{resultText(t, d.result)}</div>}
            </div>
          ))}
        </>
      )}
      {approving && <ApproveModal draft={approving} onClose={() => setApproving(null)} onDone={onChanged} />}
    </section>
  );
}
