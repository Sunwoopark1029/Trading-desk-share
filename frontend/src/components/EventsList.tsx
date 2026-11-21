import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { eventsApi, type EventItem } from "../api";
import { useT, type Key } from "../i18n";
import { usePoll } from "../usePoll";
import { TickerLink } from "../TickerLink";

interface Props {
  now: number;
  onClose: () => void;
}

const ICON: Record<string, string> = { macro: "📊", fomc: "🏛", earnings: "💼", market: "🗓", opex: "⏳", user: "⭐" };

const kstDay = (e: EventItem) => (e.time_kst ? e.time_kst.slice(0, 10) : e.date);

// floating window: position and size are remembered in this browser
interface Box { x: number; y: number; w: number; h: number }
const LS_BOX = "desk.events.box";
const MIN_W = 320;
const MIN_H = 200;

function fit(b: Box): Box {
  const w = Math.min(Math.max(b.w, MIN_W), window.innerWidth - 16);
  const h = Math.min(Math.max(b.h, MIN_H), window.innerHeight - 16);
  return { w, h, x: Math.min(Math.max(b.x, 8), window.innerWidth - w - 8), y: Math.min(Math.max(b.y, 8), window.innerHeight - h - 8) };
}

function loadBox(): Box {
  const def = { w: 620, h: Math.round(window.innerHeight * 0.7), x: window.innerWidth - 640, y: 60 };
  try {
    const v = JSON.parse(localStorage.getItem(LS_BOX) ?? "null") as Box | null;
    return fit(v && [v.x, v.y, v.w, v.h].every(Number.isFinite) ? v : def);
  } catch {
    return fit(def);
  }
}

function saveBox(b: Box): void {
  try {
    localStorage.setItem(LS_BOX, JSON.stringify(b));
  } catch {
    /* storage unavailable: the window opens at the default place next time */
  }
}

/** Floating window from the "Next event" box: the coming 14 days from every source, in KST. Drag the title bar,
 *  resize from the bottom-right corner; Esc or ✕ closes. */
export default function EventsList({ now, onClose }: Props) {
  const { t, lang } = useT();
  const q = usePoll(() => eventsApi.get(14), 60_000);
  const d = q.data;
  const [box, setBox] = useState<Box>(loadBox);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {                                  // Esc closes
    const key = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", key);
    return () => window.removeEventListener("keydown", key);
  }, [onClose]);

  useEffect(() => {                                  // the browser's resize corner -> remember the size
    const el = ref.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(() => {
      const r = el.getBoundingClientRect();
      setBox((b) => {
        if (Math.abs(b.w - r.width) < 1 && Math.abs(b.h - r.height) < 1) return b;
        const nb = { ...b, w: Math.round(r.width), h: Math.round(r.height) };
        saveBox(nb);
        return nb;
      });
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const startDrag = (e: React.PointerEvent<HTMLDivElement>) => {
    if ((e.target as HTMLElement).closest("button")) return;
    e.preventDefault();
    const bar = e.currentTarget;
    const sx = e.clientX, sy = e.clientY, start = box;
    bar.setPointerCapture(e.pointerId);
    const move = (ev: PointerEvent) => setBox(fit({ ...start, x: start.x + ev.clientX - sx, y: start.y + ev.clientY - sy }));
    const up = (ev: PointerEvent) => {
      bar.releasePointerCapture(ev.pointerId);
      bar.removeEventListener("pointermove", move);
      bar.removeEventListener("pointerup", up);
      setBox((b) => { saveBox(b); return b; });
    };
    bar.addEventListener("pointermove", move);
    bar.addEventListener("pointerup", up);
  };

  const until = (e: EventItem) => {
    const target = e.time_kst ? new Date(e.time_kst).getTime() : new Date(`${e.date}T00:00:00-05:00`).getTime();
    const s = Math.round((target - now) / 1000);
    if (s <= 0) return e.time_kst ? t("strip.now") : t("ev.today");
    const days = Math.floor(s / 86400);
    const h = Math.floor((s % 86400) / 3600);
    const m = Math.floor((s % 3600) / 60);
    return days > 0 ? t("strip.inD", { d: days, h }) : h > 0 ? t("strip.inH", { h, m }) : t("strip.inM", { m });
  };

  const groups: [string, EventItem[]][] = [];
  for (const e of d?.events ?? []) {
    const day = kstDay(e);
    const g = groups.find(([k]) => k === day);
    if (g) g[1].push(e);
    else groups.push([day, [e]]);
  }
  const bad = (d?.sources ?? []).filter((s) => s.error);
  const loading = (d?.sources ?? []).filter((s) => !s.ok && !s.error);   // not loaded yet (refresh still running)
  const src = (name: string) => t(`ev.src.${name}` as Key);

  return createPortal(
    <div ref={ref} className="events-pop" role="dialog" aria-label={t("ev.title")}
      style={{ left: box.x, top: box.y, width: box.w, height: box.h }}>
      <div className="panel-head ev-bar" onPointerDown={startDrag} onDoubleClick={() => { const b = fit({ ...box, x: window.innerWidth - box.w - 20, y: 60 }); setBox(b); saveBox(b); }}
        title={t("ev.drag")}>
        <h2>{t("ev.title")}</h2>
        <button className="small" onClick={onClose}>✕</button>
      </div>
      {q.error && !d && <div className="st-error small">{q.error}</div>}
      {d && d.events.length === 0 && <div className="muted small">{t("ev.none")}</div>}
      {groups.map(([day, list]) => (
        <div key={day} className="ev-day">
          <div className="ev-date">{day} <span className="muted">{t(`ev.wd.${new Date(`${day}T12:00:00+09:00`).getDay()}` as Key)}</span></div>
          {list.map((e) => (
            <div key={e.id} className={`ev-row imp-${e.importance}`} title={e.note ?? undefined}>
              <span className="ev-time">{e.time_kst ? e.time_kst.slice(11, 16) : t("ev.dateOnly")}</span>
              <span className="ev-icon" aria-hidden>{ICON[e.kind] ?? "•"}</span>
              <span className="ev-title">{(() => {
                const title = lang === "ko" ? e.title_ko : e.title;
                return e.symbol && title.startsWith(e.symbol)
                  ? <><TickerLink sym={e.symbol} />{title.slice(e.symbol.length)}</> : title;
              })()}
                {e.kind === "earnings" && e.note && <span className="muted small"> · {e.note}</span>}</span>
              <span className="ev-until muted small">{until(e)}</span>
              <span className="ev-src muted small">{e.source}</span>
            </div>
          ))}
        </div>
      ))}
      <div className="muted small ev-foot">
        {t("ev.foot", { n: Object.keys(d?.watched ?? {}).length })}
        {bad.length > 0 && (
          <div className="st-stale">{t("ev.sourceErr", { s: bad.map((s) => `${src(s.name)} (${s.error})`).join(", ") })}</div>
        )}
        {loading.length > 0 && (
          <div className="ev-loading">{t("ev.loading", { s: loading.map((s) => src(s.name)).join(", ") })}</div>
        )}
      </div>
    </div>,
    document.body,
  );
}
