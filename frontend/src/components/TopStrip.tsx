import { useState } from "react";
import { api, type StripItem } from "../api";
import { fmtAge, fmtKstDateTime, fmtNum, fmtPct, num, signClass } from "../format";
import { trBackend, useT } from "../i18n";
import { usePoll } from "../usePoll";
import EventsList from "./EventsList";
import { TickerLink, useKnownTickers } from "../TickerLink";

interface Props {
  pollMs: number;
  now: number;
}

function valueText(it: StripItem): string {
  const v = num(it.value);
  if (v === null) return "—";
  if (it.unit === "%") return `${fmtNum(v, 3)}%`;
  if (it.unit === "KRW") return `KRW ${fmtNum(v, 2)}`;
  if (it.unit === "USD") return `USD ${fmtNum(v, v < 10 ? 4 : 2)}`;
  return fmtNum(v, 2);
}

/** USD/KRW · NQ · ES · ICESEMI · VIX · 10Y (FRED + ^TNX) · SOXS · SOXX · next event. */
export default function TopStrip({ pollMs, now }: Props) {
  const { t, lang } = useT();
  const [showEvents, setShowEvents] = useState(false);
  const known = useKnownTickers();
  const strip = usePoll(api.strip, pollMs);
  const items = strip.data?.items ?? [];
  const ev = strip.data?.next_event;

  const countdown = (iso: string) => {
    const s = Math.round((new Date(iso).getTime() - now) / 1000);
    if (s <= 0) return t("strip.now");
    const d = Math.floor(s / 86400);
    const h = Math.floor((s % 86400) / 3600);
    const m = Math.floor((s % 3600) / 60);
    return d > 0 ? t("strip.inD", { d, h }) : h > 0 ? t("strip.inH", { h, m }) : t("strip.inM", { m });
  };

  return (
    <header className="strip">
      {strip.error && <div className="strip-item st-error">{t("strip.error", { msg: trBackend(strip.error) })}</div>}
      {!strip.data && !strip.error && <div className="strip-item muted">{t("strip.loading")}</div>}
      {items.map((it) => {
        const bad = it.status === "error" || it.status === "missing";
        const stale = it.status === "stale";
        const pct = num(it.change_pct);
        return (
          <div key={it.key} className={`strip-item${bad ? " st-error" : stale ? " st-stale" : ""}`}
            title={[trBackend(it.error), it.attribution, it.bar_date ? t("strip.barDate", { d: it.bar_date }) : null,
              it.prev_close_date ? t("strip.prevClose", { d: it.prev_close_date }) : null].filter(Boolean).join("\n") || undefined}>
            <div className="strip-top">
              <span className="strip-label">{known?.has(it.key) ? <TickerLink sym={it.key}>{it.label}</TickerLink> : it.label}</span>
              {it.flag && <span className="flag">{trBackend(it.flag)}</span>}
            </div>
            <div className="strip-val">
              {valueText(it)} {pct !== null && <span className={signClass(pct)}>{fmtPct(pct)}</span>}
            </div>
            <div className="strip-meta">
              {trBackend(it.delay)} · {bad ? trBackend(it.error ?? "error") : fmtAge(it.updated, now)}
              {stale && ` · ${t("st.stale")}`}
            </div>
          </div>
        );
      })}
      <div className="strip-item event clickable" role="button" tabIndex={0} title={t("ev.open")}
        onClick={() => setShowEvents(!showEvents)}
        onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); setShowEvents(!showEvents); } }}>
        <div className="strip-top">
          <span className="strip-label">{t("strip.nextEvent")}</span>
          {ev?.count !== undefined && ev.count > 1 && <span className="flag">+{ev.count - 1}</span>}
        </div>
        {ev ? (
          <>
            <div className="strip-val" title={ev.note ?? undefined}>{lang === "ko" && ev.name_ko ? ev.name_ko : ev.name}</div>
            <div className="strip-meta">
              {ev.time ? <>{fmtKstDateTime(ev.time)} · {countdown(ev.time)}</> : <>{ev.date} (US) · {t("ev.dateOnly")}</>}
              {ev.source && ` · ${ev.source}`}
            </div>
          </>
        ) : (
          <div className="strip-meta">{t("strip.noEvent")}</div>
        )}
      </div>
      {showEvents && <EventsList now={now} onClose={() => setShowEvents(false)} />}
    </header>
  );
}
