// Valuation card from the research chat (phase 6 part C): the AI's verdicts on three axes; every number on it was
// filled in by the backend from the code-made fundamentals pack (ids checked), never typed by the AI.
import { useState } from "react";
import { researchApi, type Valuation } from "../api";
import { ThesisEditor } from "../components/ThesisCard";
import { fmtKstDateTime, fmtPrice } from "../format";
import { trBackend, useT, type Key } from "../i18n";

/** Where the card is saved in the chat: the server loads it from there (numbers never come from the screen). */
export interface CardRef { conversation_id: string; message_id: number; card_index: number }

const AXES: ["own_history" | "peers" | "growth", Key][] = [
  ["own_history", "val.ownHistory"], ["peers", "val.peers"], ["growth", "val.growth"]];

export default function ValuationCard({ card, onOpenDesk, saveRef, onSaved }: {
  card: Valuation; onOpenDesk?: () => void; saveRef?: CardRef; onSaved?: () => void;
}) {
  const { t } = useT();
  const [edit, setEdit] = useState<{ summary: string; flags: Record<string, boolean> } | null>(null);
  const [saved, setSaved] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const save = async () => {
    if (!saveRef || !edit) return;
    setErr(null);
    try {
      await researchApi.saveThesis(card.symbol, { ...saveRef, summary: edit.summary, show_on_chart: edit.flags });
      setEdit(null);
      setSaved(true);
      onSaved?.();
    } catch (e) {
      setErr(trBackend(e instanceof Error ? e.message : String(e)));
    }
  };
  return (
    <div className="val-card">
      <div className="val-head">
        <b>{card.symbol}</b> <span className="muted small">{t("val.title")}</span>
        {card.price && (
          <span className="small"> · {fmtPrice(card.price.last, card.price.currency)}{" "}
            <span className="muted">({card.price.source}
              {card.price.time ? ` · ${fmtKstDateTime(card.price.time)}` : card.data_as_of?.price ? ` · ${fmtKstDateTime(card.data_as_of.price)}` : ""})</span>
            {card.price.status && card.price.status !== "ok" && <span className="st-stale"> · {t("st.stale")}</span>}
          </span>
        )}
      </div>
      <div className="val-verdicts">
        {AXES.map(([k, label]) => (
          <span key={k} className={`val-chip v-${card.verdicts[k]}`}>
            {t(label)}: <b>{t(`val.v.${card.verdicts[k]}` as Key)}</b>
          </span>
        ))}
      </div>
      <div className="val-summary">{card.summary}</div>
      {card.reference_levels.length > 0 && (
        <div className="val-refs">
          {card.reference_levels.map((r) => (
            <div key={r.id} className="small" title={r.note ?? ""}>
              {r.label}: <b>{r.display}</b>{" "}
              <span className="muted">({r.id} · {r.source}{r.as_of ? ` · ${fmtKstDateTime(r.as_of)}` : ""})</span>
            </div>
          ))}
        </div>
      )}
      <details className="small">
        <summary>{t("val.basis")}</summary>
        <ul className="val-list">
          {card.basis.map((b) => (
            <li key={b.id}>{b.label}: <b>{b.display}</b> <span className="muted">({b.id} · {b.period} · {b.source})</span></li>
          ))}
        </ul>
        {card.reasoning && <p>{card.reasoning}</p>}
      </details>
      {card.risks.length > 0 && (
        <div className="small"><b>{t("val.risks")}</b>
          <ul className="val-list">{card.risks.map((r) => <li key={r}>{r}</li>)}</ul>
        </div>
      )}
      {card.change_view.length > 0 && (
        <div className="small"><b>{t("val.changeView")}</b>
          <ul className="val-list">{card.change_view.map((r) => <li key={r}>{r}</li>)}</ul>
        </div>
      )}
      {card.warnings.map((w) => <div key={w} className="small st-stale">⚠ {w}</div>)}
      {edit && (
        <ThesisEditor summary={edit.summary} flags={edit.flags} refs={card.reference_levels}
          onChange={(summary, flags) => setEdit({ summary, flags })} onSave={() => void save()} onCancel={() => setEdit(null)} />
      )}
      {err && <div className="st-error small">{err}</div>}
      <div className="val-foot small muted">
        <span>{t("val.note")}{card.data_as_of?.statements ? ` · ${t("val.statementsAsOf")} ${fmtKstDateTime(card.data_as_of.statements)}` : ""}</span>
        <span className="thesis-actions">
          {saved && <span>{t("th.savedOk")}</span>}
          {saveRef && !edit && !saved && (
            <button className="small" onClick={() => setEdit({ summary: card.summary,
              flags: Object.fromEntries(card.reference_levels.map((r) => [r.id, r.id.startsWith("fv_")])) })}>
              {t("th.saveThesis")}</button>
          )}
          {onOpenDesk && <button className="small" onClick={onOpenDesk}>{t("ex.openDesk")}</button>}
        </span>
      </div>
    </div>
  );
}
