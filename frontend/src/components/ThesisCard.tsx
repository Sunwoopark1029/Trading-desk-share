// The ticker's saved thesis + notes (phase 6 part C). Numbers on it come from the valuation card the code made
// (pack values) and from code-made facts (price moved since saved, newer SEC filing, days to earnings).
import { useEffect, useState } from "react";
import { researchApi, type RsThesisView } from "../api";
import { fmtKstDateTime, fmtPrice } from "../format";
import { trBackend, useT, type Key } from "../i18n";
import { usePoll } from "../usePoll";

const AXES: ["own_history" | "peers" | "growth", Key][] = [
  ["own_history", "val.ownHistory"], ["peers", "val.peers"], ["growth", "val.growth"]];
const kstDay = new Intl.DateTimeFormat("sv-SE", { timeZone: "Asia/Seoul", year: "numeric", month: "2-digit", day: "2-digit" });
const kstDate = (iso: string) => { try { return kstDay.format(new Date(iso)); } catch { return iso.slice(0, 10); } };

export default function ThesisCard({ symbol, tick, onChanged, compact = false, onOpenResearch }: {
  symbol: string; tick: number; onChanged?: () => void;
  compact?: boolean;                    // Desk: read-only summary with a link back to Research
  onOpenResearch?: () => void;
}) {
  const { t } = useT();
  const q = usePoll(() => researchApi.thesis(symbol), 120_000, [symbol, tick]);
  const [edit, setEdit] = useState<{ summary: string; flags: Record<string, boolean> } | null>(null);
  const [note, setNote] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const d: RsThesisView | null = q.data;
  const th = d?.thesis ?? null;
  useEffect(() => { setEdit(null); }, [th?.id]);     // a newer thesis arrived: never save over it with old text

  const run = async (f: () => Promise<unknown>, closeEdit = true): Promise<boolean> => {
    setErr(null);
    try {
      await f();
      if (closeEdit) setEdit(null);
      q.refresh();
      onChanged?.();
      return true;
    } catch (e) {
      setErr(trBackend(e instanceof Error ? e.message : String(e)));
      return false;
    }
  };
  const addNote = () => void run(() => researchApi.addNote(symbol, note.trim()), false).then((ok) => { if (ok) setNote(""); });

  return (
    <div className="thesis-card">
      <div className="thesis-head">
        <b>{t("th.title")}</b>
        {th && <span className="muted small"> · {t(th.saved_by === "ai" ? "th.byAi" : "th.byYou")}
          {th.edited_by_user ? ` · ${t("th.editedByYou")}` : ""} · {fmtKstDateTime(th.saved_at)}</span>}
      </div>
      {q.error && <div className="st-error small">{trBackend(q.error)}</div>}
      {d && !th && <div className="muted small">{t(compact ? "th.noneDesk" : "th.none", { sym: symbol })}</div>}
      {th && !edit && (
        <>
          <div className="val-verdicts">
            {AXES.map(([k, label]) => (
              <span key={k} className={`val-chip v-${th.verdict[k] ?? "unclear"}`}>
                {t(label)}: <b>{t(`val.v.${th.verdict[k] ?? "unclear"}` as Key)}</b>
              </span>
            ))}
          </div>
          <div className="val-summary">{th.summary}</div>
          <div className="small thesis-facts">
            {th.price_at_save && (
              <span>{t("th.priceAtSave")} {fmtPrice(th.price_at_save, th.currency)}{" "}
                <span className="muted">({th.price_source ?? "Toss"}{th.price_time ? ` · ${fmtKstDateTime(th.price_time)}` : ""})</span>
                {th.price_status && th.price_status !== "ok" && <span className="st-stale"> {t("st.stale")}</span>}
              </span>
            )}
            {d?.facts.price_now && d.facts.moved_since_save && (
              <span> → {fmtPrice(d.facts.price_now.last, d.facts.price_now.currency)} (<b>{d.facts.moved_since_save.display}</b>,
                {" "}{d.facts.price_now.source}{d.facts.price_now.time ? ` · ${fmtKstDateTime(d.facts.price_now.time)}` : ""})
                {d.facts.moved_since_save.stale && <span className="st-stale"> {t("st.stale")}</span>}</span>
            )}
            {d?.facts.new_filing && <span className="st-stale"> · ⚠ {t("th.newFiling", { d: d.facts.latest_filed ?? "" })}</span>}
            {d?.facts.next_earnings && (
              <span> · {t("th.earningsIn", { n: d.facts.next_earnings.days_away, d: d.facts.next_earnings.date })} {t("chart.usDate")}</span>
            )}
          </div>
          {th.reference_levels.length > 0 && (
            <div className="val-refs">
              {th.reference_levels.map((r) => (
                <div key={r.id} className="small">
                  {r.label}: <b>{r.display}</b> <span className="muted">({r.id} · {r.source}{r.as_of ? ` · ${fmtKstDateTime(r.as_of)}` : ""})</span>
                  {r.show_on_chart && <span className="muted"> · {t("th.onChart")}</span>}
                </div>
              ))}
            </div>
          )}
          {(th.warnings ?? []).map((w) => <div key={w} className="small st-stale">⚠ {trBackend(w)}</div>)}
          {th.risks.length > 0 && (
            <details className="small"><summary>{t("val.risks")}</summary>
              <ul className="val-list">{th.risks.map((r, i) => <li key={i}>{r}</li>)}</ul>
            </details>
          )}
          {compact && onOpenResearch && (
            <div className="thesis-actions"><button className="small" onClick={onOpenResearch}>{t("th.openResearch")}</button></div>
          )}
          {!compact && <div className="thesis-actions">
            <button className="small" onClick={() => setEdit({ summary: th.summary,
              flags: Object.fromEntries(th.reference_levels.map((r) => [r.id, r.show_on_chart])) })}>{t("th.edit")}</button>
            <button className="small" onClick={() => { if (window.confirm(t("th.deleteConfirm"))) void run(() => researchApi.deleteThesis(symbol)); }}>
              {t("common.delete")}</button>
          </div>}
        </>
      )}
      {th && edit && (
        <ThesisEditor summary={edit.summary} flags={edit.flags} refs={th.reference_levels}
          onChange={(summary, flags) => setEdit({ summary, flags })}
          onCancel={() => setEdit(null)}
          onSave={() => void run(() => researchApi.saveThesis(symbol, { summary: edit.summary, show_on_chart: edit.flags }))} />
      )}
      {!compact && d && d.history.length > 1 && (
        <details className="small"><summary>{t("th.history", { n: d.history.length })}</summary>
          <ul className="val-list">
            {d.history.map((h) => (
              <li key={h.id}>{fmtKstDateTime(h.saved_at)} · {t(h.saved_by === "ai" ? "th.byAi" : "th.byYou")}
                {h.current ? ` · ${t("th.current")}` : ""}: {h.summary}</li>
            ))}
          </ul>
        </details>
      )}
      {!compact && <div className="thesis-notes small">
        <b>{t("th.notes")}</b>
        <ul className="val-list">
          {(d?.notes ?? []).map((n) => (
            <li key={n.id}>
              <span className="muted">{kstDate(n.created)}{n.source === "code" ? ` · ${t("th.noteCode")}` : ""}</span> {n.text}
              <button className="small ex-x" title={t("common.delete")}
                onClick={() => void run(() => researchApi.deleteNote(symbol, n.id), false)}>✕</button>
            </li>
          ))}
        </ul>
        <div className="thesis-add">
          <input value={note} maxLength={300} placeholder={t("th.notePlaceholder")} onChange={(e) => setNote(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && note.trim() && !e.nativeEvent.isComposing) {
                addNote();
              }
            }} />
          <button className="small" disabled={!note.trim()}
            onClick={addNote}>{t("th.addNote")}</button>
        </div>
      </div>}
      {err && <div className="st-error small">{err}</div>}
    </div>
  );
}

export function ThesisEditor({ summary, flags, refs, onChange, onSave, onCancel }: {
  summary: string; flags: Record<string, boolean>; refs: { id: string; label: string; display: string }[];
  onChange: (summary: string, flags: Record<string, boolean>) => void; onSave: () => void; onCancel: () => void;
}) {
  const { t } = useT();
  return (
    <div className="thesis-editor small">
      <label>{t("th.summary")} ({summary.length}/400)
        <textarea value={summary} maxLength={400} rows={3} onChange={(e) => onChange(e.target.value, flags)} />
      </label>
      {refs.length > 0 && <div className="muted">{t("th.chartLines")}</div>}
      {refs.map((r) => (
        <label key={r.id} className="thesis-flag">
          <input type="checkbox" checked={!!flags[r.id]} onChange={(e) => onChange(summary, { ...flags, [r.id]: e.target.checked })} />
          {" "}{r.label}: {r.display}
        </label>
      ))}
      <div className="thesis-actions">
        <button className="small" disabled={!summary.trim()} onClick={onSave}>{t("th.save")}</button>
        <button className="small" onClick={onCancel}>{t("th.cancel")}</button>
      </div>
    </div>
  );
}
