import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, chatApi, sendChat, type Candidates, type ChatEvent, type ChatItem, type ScenarioTable as Table,
  type Tier, type Valuation } from "../api";
import { trBackend, useT, type Key } from "../i18n";
import { usePoll } from "../usePoll";
import CostLine, { modelName } from "./CostLine";
import CreditHeader from "./CreditHeader";
import Markdown from "./Markdown";
import ScenarioTable from "./ScenarioTable";
import ValuationCard from "./ValuationCard";
import { chooseThread, threadKey, type ChatKind } from "./threads";
import type { Prefill } from "../orders/OrderTicket";

interface Props {
  symbol: string;                                       // the ticker on screen: its own chats are shown (D5)
  kind?: ChatKind;                                      // desk (default) | research
  getScreen: () => Record<string, unknown>;
  onOverlayChanged: () => void;
  onOrder?: (p: Omit<Prefill, "nonce">) => void;       // scenario row -> pre-filled ticket (no send)
  onScenarios?: (tables: Table[]) => void;              // the latest scenario tables, for the Orders tab
  onDraftCreated?: () => void;                          // the AI made a draft from the user's typed order
  onOpenOrders?: () => void;
  onOpenDesk?: () => void;                              // research chat: valuation card -> this ticker on the Desk
  onThesisChanged?: () => void;                         // research chat: a thesis was saved (button or AI on request)
  onPickSymbol?: (s: string) => void;                   // research chat: a candidate ticker was clicked
}

interface Live {
  text: string;
  status: string | null;
  notices: string[];
  scenarios: Table[];
  valuations: Valuation[];
  candidates: Candidates[];
  error: string | null;
}

const TOOLS = ["get_levels", "get_screen_state", "add_overlay", "remove_overlay", "search_news", "show_scenarios", "web_search", "create_order_draft",
  "get_fundamentals", "get_financials", "get_earnings", "get_analysts", "compare_peers", "get_company_news", "show_valuation",
  "save_thesis", "get_thesis", "get_rankings", "list_sectors", "get_sector", "search_tickers", "show_candidates"];
const EMPTY_LIVE: Live = { text: "", status: null, notices: [], scenarios: [], valuations: [], candidates: [], error: null };

function CandidateChips({ list, onPick }: { list: Candidates; onPick?: (s: string) => void }) {
  return (
    <div className="cand-list">
      <div className="small"><b>{list.title}</b> <span className="muted">· {list.note}</span></div>
      <div className="cand-chips">
        {list.items.map((i) => (
          <button key={i.symbol} className="small cand-chip" disabled={!onPick} onClick={() => onPick?.(i.symbol)}
            title={i.name ?? ""}>
            <b>{i.symbol}</b>{i.reason ? <span className="muted"> · {i.reason}</span> : null}
          </button>
        ))}
      </div>
    </div>
  );
}
const TIER_KEY: Record<Tier, Key> = { normal: "chat.normal", deep: "chat.deep", very_deep: "chat.veryDeep" };
function lsGet(key: string): string | null {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}
function lsSet(key: string, v: string | null): void {
  try {
    if (v) localStorage.setItem(key, v);
    else localStorage.removeItem(key);
  } catch {
    /* storage unavailable */
  }
}

export default function ChatPanel({ symbol, kind = "desk", getScreen, onOverlayChanged, onOrder, onScenarios, onDraftCreated,
  onOpenOrders, onOpenDesk, onThesisChanged, onPickSymbol }: Props) {
  const { t, lang } = useT();
  const status = usePoll(chatApi.status, 60_000);
  const usage = usePoll(chatApi.usage, 30_000);
  const [convs, setConvs] = useState<{ id: string; title: string }[]>([]);
  const [current, setCurrent] = useState<string | null>(null);
  const viewKey = threadKey(kind, symbol);              // which ticker + page is on screen now
  const viewRef = useRef(viewKey);
  viewRef.current = viewKey;
  const [items, setItems] = useState<ChatItem[]>([]);
  const [live, setLive] = useState<Live | null>(null);
  const [text, setText] = useState("");
  const [tier, setTier] = useState<Tier>("normal");
  const [web, setWeb] = useState<boolean | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [draftMade, setDraftMade] = useState<string | null>(null);   // ticker of the draft the AI just made
  const abort = useRef<AbortController | null>(null);
  // Each send gets a token; changing ticker clears the live one, so an answer for another ticker never writes into
  // this view (coming back mid-answer shows "still answering" and the full answer when it is saved).
  const sendSeq = useRef(0);
  const liveTok = useRef(0);
  const pending = useRef<Set<string>>(new Set());      // views with an answer still streaming
  const [, rerender] = useState(0);
  const currentRef = useRef<string | null>(null);
  currentRef.current = current;
  const bottom = useRef<HTMLDivElement>(null);

  const st = status.data;
  const tiers = st?.tiers ?? [];
  const tierTouched = useRef(false);
  useEffect(() => {
    if (!tierTouched.current && st?.default_tier) setTier(st.default_tier);
  }, [st?.default_tier]);
  const webOn = web ?? st?.web_search_default ?? false;
  const stillAnswering = pending.current.has(threadKey(kind, symbol)) && live === null;
  const busy = (live !== null && live.error === null && live.status !== "done") || stillAnswering;
  const toolName = (n: string) => (TOOLS.includes(n) ? t(`tool.${n}` as Key) : n);

  const loadList = useCallback(async () => {
    const key = threadKey(kind, symbol);
    try {
      const list = (await chatApi.list(symbol, kind)).conversations;
      if (viewRef.current === key) setConvs(list);
      return list;
    } catch {
      return null;                                      /* shown via status */
    }
  }, [symbol, kind]);

  const loadConv = useCallback(async (id: string | null) => {
    if (!id) {
      setItems([]);
      return;
    }
    try {
      const key = viewRef.current;
      const conv = await chatApi.get(id);
      if (viewRef.current === key) setItems(conv.messages);   // ignore a late answer for another ticker
    } catch (e) {
      if (e instanceof ApiError && e.status === 404) {
        setCurrent(null);
        lsSet(viewRef.current, null);
        setItems([]);
      }
    }
  }, []);

  // ticker or page changed: show that ticker's chats and open its remembered (or newest) one. An answer still
  // streaming for the previous ticker keeps running on the server and is saved in its own chat.
  useEffect(() => {
    let gone = false;
    liveTok.current = 0;
    setLive(null);
    setErr(null);
    setItems([]);
    setCurrent(null);
    setConvs([]);
    void loadList().then((list) => {
      if (!gone && list) setCurrent(chooseThread(list, lsGet(viewKey)));
    });
    return () => { gone = true; };
  }, [loadList, viewKey]);
  useEffect(() => {
    void loadConv(current);
  }, [current, loadConv]);
  useEffect(() => {
    bottom.current?.scrollIntoView({ block: "end" });
  }, [items, live?.text, live?.scenarios.length, live?.valuations.length, live?.candidates.length]);
  useEffect(() => {
    if (!onScenarios) return;
    if (live?.scenarios.length) {
      onScenarios(live.scenarios);
      return;
    }
    for (let i = items.length - 1; i >= 0; i--) {
      const s = items[i].scenarios;
      if (s && s.length) {
        onScenarios(s);
        return;
      }
    }
  }, [items, live?.scenarios, onScenarios]);

  const pick = (id: string | null) => {
    setCurrent(id);
    lsSet(viewRef.current, id);
    setLive(null);
    setErr(null);
  };

  const newChat = async () => {
    try {
      const { id } = await chatApi.create(symbol, kind);
      await loadList();
      pick(id);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
  };

  const removeChat = async () => {
    if (!current || !window.confirm(t("chat.deleteConfirm"))) return;
    await chatApi.remove(current).catch(() => undefined);
    pick(null);
    await loadList();
  };

  const send = async (confirm = false, preset?: string) => {
    const q = (preset ?? text).trim();
    if (!q || busy) return;
    setErr(null);
    const myView = viewRef.current;
    const mySymbol = symbol;
    const myTok = ++sendSeq.current;
    liveTok.current = myTok;
    const mine = () => liveTok.current === myTok;      // this view has not been left since the question was sent
    let id = current;
    if (!id) {
      try {
        id = (await chatApi.create(symbol, kind)).id;
        pick(id);
      } catch (e) {
        setErr(e instanceof Error ? e.message : String(e));
        return;
      }
    }
    const body = { text: q, tier, web_search: webOn, confirm_over_budget: confirm, screen: { ...getScreen(), symbol }, lang };
    const ctrl = new AbortController();
    abort.current = ctrl;
    setItems((xs) => [...xs, { kind: "question", text: q, tier }]);
    setText("");
    setLive({ ...EMPTY_LIVE, status: t("chat.sending") });
    setDraftMade(null);
    pending.current.add(myView);
    const onEvent = (e: ChatEvent) => {
      if (e.type === "overlay_changed") onOverlayChanged();
      if (e.type === "thesis_saved") onThesisChanged?.();
      if (e.type === "draft_created") {                 // shown whatever ticker is on screen, named
        onDraftCreated?.();
        setDraftMade(mySymbol);
      }
      if (!mine()) return;                              // another ticker is (or was) on screen: not this view
      setLive((l) => {
        const cur = l ?? EMPTY_LIVE;
        switch (e.type) {
          case "start": return { ...cur, status: t("chat.answering", { model: modelName(e.model) }) };
          case "thinking": return { ...cur, status: t("chat.thinking") };
          case "text": return { ...cur, text: cur.text + e.text, status: null };
          case "tool":
            return { ...cur, status: e.status === "error" ? t("chat.fixing", { tool: toolName(e.name), msg: e.message ?? "error" })
              : e.status === "running" ? `${toolName(e.name)}…` : cur.status };
          case "scenarios": return { ...cur, scenarios: [...cur.scenarios, e.table] };
          case "valuation": return { ...cur, valuations: [...cur.valuations, e.card] };
          case "candidates": return { ...cur, candidates: [...cur.candidates, e.list] };
          case "notice": return { ...cur, notices: [...cur.notices, trBackend(e.message)] };
          case "error": return { ...cur, error: trBackend(e.message) };
          case "done": return { ...cur, status: "done" };
          default: return cur;
        }
      });
    };
    try {
      await sendChat(id!, { ...body }, onEvent, ctrl.signal);
    } catch (e) {
      if (!mine()) return;
      const be = e as ApiError & { body?: { needs_confirm?: boolean } };
      setItems((xs) => xs.slice(0, -1));
      setText(q);
      setLive(null);
      if (be instanceof ApiError && be.status === 409 && be.body?.needs_confirm) {
        if (window.confirm(t("chat.overBudget"))) {
          setTimeout(() => void send(true, q), 0);
        }
        return;
      }
      if (!(e instanceof DOMException && e.name === "AbortError")) setErr(trBackend(e instanceof Error ? e.message : String(e)));
      return;
    } finally {
      if (abort.current === ctrl) abort.current = null;  // never clear a newer answer's Stop
      pending.current.delete(myView);
      rerender((n) => n + 1);
    }
    usage.refresh();
    if (!mine()) {
      if (viewRef.current === myView) {                 // back on that ticker: show the saved full answer
        await loadList();
        if (currentRef.current === id) await loadConv(id);
      }
      return;
    }
    await loadConv(id);
    await loadList();
    setLive((l) => (l && l.error ? l : null));
    status.refresh();
  };

  if (st && !st.enabled) {
    return (
      <section className="panel chat-panel">
        <div className="panel-head"><h2>{t("chat.title")}</h2></div>
        <div className="muted">{t("chat.off", { msg: st.message ?? "" })}</div>
      </section>
    );
  }

  const keyMissing = st?.key === "empty";
  return (
    <section className="panel chat-panel">
      <div className="panel-head">
        <h2>{t(kind === "research" ? "chat.researchTitle" : "chat.title")} <span className="muted small">· {symbol}</span></h2>
        <div className="chat-convs">
          <select value={current ?? ""} onChange={(e) => pick(e.target.value || null)} aria-label={t("chat.title")}>
            <option value="">{t("chat.newOption")}</option>
            {convs.map((c) => <option key={c.id} value={c.id}>{c.title || t("chat.untitled")}</option>)}
          </select>
          <button className="small" onClick={newChat}>{t("chat.new")}</button>
          {current && <button className="small" onClick={removeChat}>{t("common.delete")}</button>}
        </div>
      </div>
      <CreditHeader summary={usage.data} error={usage.error} />
      {keyMissing && <div className="st-error">{t("chat.keyMissing")}</div>}

      <div className="chat-log">
        {items.length === 0 && !live && <div className="muted small">{t(kind === "research" ? "chat.researchEmpty" : "chat.empty")}</div>}
        {items.map((m, i) => (
          <div key={m.id ?? `n${i}`} className={`msg ${m.kind}${m.active === false ? " summarized" : ""}`}>
            {m.kind === "question" ? (
              <>
                <div className="msg-meta muted small">
                  {t("chat.you")} · {m.tier ? t(TIER_KEY[m.tier]) : ""}
                  {m.symbol && ` · ${m.symbol} ${m.timeframe ?? ""}`}{m.web_search && t("chat.webOn")}
                </div>
                <div className="msg-text">{m.text}</div>
              </>
            ) : (
              <>
                {m.text ? <Markdown text={m.text} /> : <div className="muted">{t("chat.noText")}</div>}
                {(m.scenarios ?? []).map((tb, j) => <ScenarioTable key={j} table={tb} onOrder={onOrder} />)}
                {(m.valuations ?? []).map((c, j) => (
                  <ValuationCard key={j} card={c} onOpenDesk={onOpenDesk} onSaved={onThesisChanged}
                    saveRef={kind === "research" && current && m.id !== undefined
                      ? { conversation_id: current, message_id: m.id, card_index: j } : undefined} />
                ))}
                {(m.candidates ?? []).map((c, j) => <CandidateChips key={j} list={c} onPick={onPickSymbol} />)}
                {(m.warnings ?? []).map((w) => <div key={w} className="st-stale small">⚠ {trBackend(w)}</div>)}
                {m.cost && <CostLine cost={m.cost} />}
              </>
            )}
          </div>
        ))}
        {live && (
          <div className="msg answer live">
            {live.notices.map((n) => <div key={n} className="muted small">{n}</div>)}
            {live.text && <Markdown text={live.text} />}
            {live.scenarios.map((tb, j) => <ScenarioTable key={j} table={tb} onOrder={onOrder} />)}
            {live.valuations.map((c, j) => <ValuationCard key={j} card={c} onOpenDesk={onOpenDesk} />)}
            {live.candidates.map((c, j) => <CandidateChips key={j} list={c} onPick={onPickSymbol} />)}
            {live.status && live.status !== "done" && <div className="muted small typing">{live.status}</div>}
            {live.error && <div className="st-error">{live.error}</div>}
          </div>
        )}
        <div ref={bottom} />
      </div>

      {err && <div className="st-error small">{err}</div>}
      {stillAnswering && <div className="muted small">{t("chat.stillAnswering")}</div>}
      {draftMade && (
        <div className="prefill-note small">
          {t("chat.draftCreated", { sym: draftMade })}
          {onOpenOrders && <button className="small" onClick={onOpenOrders}>{t("chat.openOrders")}</button>}
          <button className="small" onClick={() => setDraftMade(null)}>✕</button>
        </div>
      )}
      <div className="chat-input">
        <textarea value={text} onChange={(e) => setText(e.target.value)} rows={2} disabled={keyMissing}
          placeholder={keyMissing ? t("chat.keyPlaceholder") : t("chat.placeholder")}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
              e.preventDefault();
              void send();
            }
          }} />
        <div className="chat-controls">
          <div className="seg" role="radiogroup" aria-label="Model">
            {tiers.map((tr) => (
              <button key={tr.name} className={tier === tr.name ? "on" : ""} onClick={() => { tierTouched.current = true; setTier(tr.name); }}
                title={`${tr.label}: ${tr.model}`} disabled={busy}>
                {t(TIER_KEY[tr.name])}
              </button>
            ))}
          </div>
          <label className="small" title={t("chat.webTitle")}>
            <input type="checkbox" checked={webOn} onChange={() => setWeb(!webOn)} disabled={busy} /> {t("chat.web")}
          </label>
          <button onClick={() => void send(false, kind === "research" ? t("chat.researchQuickPrompt", { sym: symbol })
            : st?.quick_prompt?.trim() || t("chat.quickPrompt"))}
            disabled={busy || keyMissing} title={t(kind === "research" ? "chat.researchQuickTitle" : "chat.quickTitle")}>
            {t(kind === "research" ? "chat.researchQuick" : "chat.quick")}</button>
          {busy ? (
            <button onClick={() => abort.current?.abort()}>{t("chat.stop")}</button>
          ) : (
            <button onClick={() => void send()} disabled={!text.trim() || keyMissing}>{t("chat.send")}</button>
          )}
        </div>
      </div>
    </section>
  );
}
