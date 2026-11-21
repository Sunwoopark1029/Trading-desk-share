import { useState, type ReactNode } from "react";
import { useT } from "../i18n";

const LS_CHAT_W = "desk.chat.width";
const MIN_CHAT = 320;
const MIN_LEFT = 420;

const clampW = (w: number) => Math.round(Math.min(Math.max(w, MIN_CHAT), Math.max(MIN_CHAT, window.innerWidth - MIN_LEFT)));

function loadW(): number | null {
  try {
    const v = Number(localStorage.getItem(LS_CHAT_W));
    return Number.isFinite(v) && v >= MIN_CHAT ? clampW(v) : null;
  } catch {
    return null;
  }
}

function saveW(w: number | null): void {
  try {
    if (w) localStorage.setItem(LS_CHAT_W, String(w));
    else localStorage.removeItem(LS_CHAT_W);
  } catch {
    /* storage unavailable: width lasts for this visit */
  }
}

/** Desk: left column (chart + panels) | drag handle | chat. Drag to change the chat width; double-click resets. */
export default function DeskSplit({ left, right }: { left: ReactNode; right: ReactNode }) {
  const { t } = useT();
  const [chatW, setChatW] = useState<number | null>(loadW); // null = default (CSS: 40%)

  const startResize = (e: React.PointerEvent<HTMLDivElement>) => {
    e.preventDefault();
    const handle = e.currentTarget;
    const startX = e.clientX;
    const startW = (handle.nextElementSibling as HTMLElement | null)?.getBoundingClientRect().width ?? 480;
    handle.setPointerCapture(e.pointerId);
    const move = (ev: PointerEvent) => setChatW(clampW(startW - (ev.clientX - startX)));
    const up = (ev: PointerEvent) => {
      handle.releasePointerCapture(ev.pointerId);
      handle.removeEventListener("pointermove", move);
      handle.removeEventListener("pointerup", up);
      setChatW((w) => {
        saveW(w);
        return w;
      });
    };
    handle.addEventListener("pointermove", move);
    handle.addEventListener("pointerup", up);
  };
  const reset = () => {
    setChatW(null);
    saveW(null);
  };

  return (
    <main className="grid desk" style={chatW ? ({ "--chat-w": `${chatW}px` } as React.CSSProperties) : undefined}>
      <div className="col-main">{left}</div>
      <div className="col-resize" onPointerDown={startResize} onDoubleClick={reset}
        title={t("desk.resize")} role="separator" aria-orientation="vertical" />
      <aside className="col-chat">{right}</aside>
    </main>
  );
}
