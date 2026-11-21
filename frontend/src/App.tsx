import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, orderApi, researchApi, settingsApi, watchApi, type ChartDefaults, type ScenarioTable, type WatchFolder } from "./api";
import ChatPanel from "./chat/ChatPanel";
import ChartPanel from "./components/ChartPanel";
import DeskSplit from "./components/DeskSplit";
import HealthBar from "./components/HealthBar";
import MyInfo from "./components/MyInfo";
import NewsPanel from "./components/NewsPanel";
import OptionsPanel from "./components/OptionsPanel";
import OrderbookPanel from "./components/OrderbookPanel";
import PnlPanel from "./components/PnlPanel";
import ReservedPanel from "./components/ReservedPanel";
import TopStrip from "./components/TopStrip";
import OrdersPage from "./orders/OrdersPage";
import Journal from "./orders/Journal";
import type { Prefill } from "./orders/OrderTicket";
import SettingsPage from "./settings/SettingsPage";
import ResearchPage from "./research/ResearchPage";
import type { WatchState } from "./research/ExploreRail";
import { LoginGate, useAuth } from "./Login";
import { LangProvider, storedLang, trBackend, useT, type Key, type Lang } from "./i18n";
import { useMediaQuery, useNow, usePoll } from "./usePoll";
import { OpenResearchProvider } from "./TickerLink";
import ThesisCard from "./components/ThesisCard";
import { thesisLines } from "./research/thesisLines";

type Tab = "chart" | "pnl" | "chat" | "book" | "research" | "orders" | "journal" | "settings";
const TABS: [Tab, Key][] = [["chart", "tab.chart"], ["pnl", "tab.pnl"], ["chat", "tab.chat"], ["book", "tab.book"],
  ["research", "tab.research"], ["orders", "tab.orders"], ["journal", "view.journal"], ["settings", "view.settings"]];
type View = "desk" | "research" | "orders" | "journal" | "settings";
type DeskTab = "chat" | "pnl" | "book" | "info";
const DESK_TABS: [DeskTab, Key][] = [["chat", "tab.chat"], ["pnl", "tab.pnl"], ["book", "tab.book"], ["info", "tab.info"]];
const VIEWS: View[] = ["desk", "research", "orders", "journal", "settings"];

/** Per-browser memory (active ticker, last view). Storage may be off: then nothing is remembered. */
function remembered(key: string): string | null {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}
function remember(key: string, value: string): void {
  try {
    localStorage.setItem(key, value);
  } catch {
    /* storage off */
  }
}

export default function App() {
  const saved = storedLang();
  return (
    <LangProvider initial={saved ?? "en"}>
      <Shell firstVisit={saved === null} />
    </LangProvider>
  );
}

/** First open: pick a language (saved in this browser). Then the dashboard. */
function Shell({ firstVisit }: { firstVisit: boolean }) {
  const [picked, setPicked] = useState(!firstVisit);
  const { setLang } = useT();
  if (!picked) {
    const choose = (l: Lang) => {
      setLang(l);
      setPicked(true);
    };
    return (
      <div className="lang-pick">
        <h1>Choose a language · 언어를 선택하세요</h1>
        <div className="lang-buttons">
          <button onClick={() => choose("en")}>English</button>
          <button onClick={() => choose("ko")}>한국어</button>
        </div>
        <p className="muted">You can change it later at the top right. · 나중에 오른쪽 위에서 바꿀 수 있습니다.</p>
      </div>
    );
  }
  return <LoginGate><Boot /></LoginGate>;
}

function Boot() {
  const { t } = useT();
  const cfg = usePoll(api.uiConfig, 10_000); // also retries while the backend is starting
  if (!cfg.data) {
    return (
      <div className="boot">
        {cfg.error ? (
          <>
            <h1>{t("app.noBackend")}</h1>
            <p>{trBackend(cfg.error)}</p>
            <p className="muted">{t("app.startHint")}</p>
          </>
        ) : (
          <p className="muted">{t("app.loading")}</p>
        )}
      </div>
    );
  }
  return <Desk defaultSymbol={cfg.data.tickers.default} compare={cfg.data.tickers.compare}
    quotesMs={cfg.data.polling.quotes_seconds * 1000}
    candlesMs={(cfg.data.polling.chart_seconds ?? cfg.data.polling.candles_seconds) * 1000}
    chartDefaults={cfg.data.user?.chart} onSettingsSaved={cfg.refresh} />;
}

interface DeskProps {
  defaultSymbol: string;
  compare: string[];
  quotesMs: number;
  candlesMs: number;
  chartDefaults?: ChartDefaults;
  onSettingsSaved: () => void;
}

function Desk({ defaultSymbol, compare, quotesMs, candlesMs, chartDefaults, onSettingsSaved }: DeskProps) {
  useEffect(() => {
    document.documentElement.classList.toggle("colors-us", chartDefaults?.color_scheme === "us");
  }, [chartDefaults?.color_scheme]);
  const { t, lang, setLang } = useT();
  const auth = useAuth();
  // active ticker and last view are remembered in this browser; config tickers.default is the first-run fallback
  const [symbol, setSymbol] = useState(() => {
    const s = remembered("desk.symbol");
    return s && /^[A-Z0-9.\-]{1,20}$/.test(s) ? s : defaultSymbol;
  });
  const [tab, setTab] = useState<Tab>("chart");
  const [view, setView] = useState<View>(() => {
    const v = remembered("desk.view") as View | null;
    return v && VIEWS.includes(v) ? v : "desk";
  });
  useEffect(() => remember("desk.symbol", symbol), [symbol]);
  useEffect(() => remember("desk.view", view), [view]);
  const phone = useMediaQuery("(max-width: 430px)");
  // medium width (e.g. an unfolded Galaxy Z Fold): chart on top, one tab area under it (Chat first)
  const tablet = useMediaQuery("(min-width: 431px) and (max-width: 900px)");
  const [deskTab, setDeskTab] = useState<DeskTab>("chat");
  // any ticker on screen (TickerLink): make it the current ticker and open its Research page
  const openResearch = useCallback((s: string) => {
    setSymbol(s.toUpperCase());
    if (phone) setTab("research");
    else setView("research");
  }, [phone]);
  const now = useNow(1000);
  const positions = usePoll(api.positions, 30_000);
  const [overlayTick, setOverlayTick] = useState(0);
  const screen = useRef<Record<string, unknown>>({ symbol: defaultSymbol, timeframe: "5m" });
  const onScreen = useCallback((s: Record<string, unknown>) => {
    screen.current = s;
  }, []);
  const getScreen = useCallback(() => screen.current, []);
  // keep the chat's ticker right when the chart isn't mounted (phone Research / Orders tab); when it is, the
  // chart's own effect has already set the full screen state for the new ticker and this does nothing
  useEffect(() => {
    if (screen.current.symbol !== symbol) screen.current = { symbol, timeframe: screen.current.timeframe ?? "5m" };
  }, [symbol]);
  const onOverlayChanged = useCallback(() => setOverlayTick((n) => n + 1), []);

  // watchlist folders; the pinned folder supplies the quick buttons (and, in the backend, the strip slots)
  const wl = usePoll(watchApi.get, 60_000);
  const watch: WatchState = useMemo(() => ({
    doc: wl.data,
    error: wl.error,
    save: async (folders: Partial<WatchFolder>[]) => {
      await watchApi.save({ folders });
      wl.refresh();
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }), [wl.data, wl.error]);
  const pinned = wl.data?.folders.find((f) => f.pinned)?.symbols ?? [];
  const quick = useMemo(() => (pinned.length ? pinned.slice(0, 12) : [...new Set([defaultSymbol, ...compare])]),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [pinned.join(","), defaultSymbol, compare]);
  const drafts = usePoll(() => orderApi.drafts(""), 5_000);
  // saved research thesis of the ticker on screen: its ticked reference prices become chart lines (phase 6 part C)
  const thesis = usePoll(() => researchApi.thesis(symbol), 120_000, [symbol, view]);   // reload when the view changes
  const fills = useMemo(() => (drafts.data?.fills ?? []).filter((f) => f.symbol === symbol), [drafts.data, symbol]);
  const [prefill, setPrefill] = useState<Prefill | null>(null);
  const [latestScen, setLatestScen] = useState<ScenarioTable[]>([]);
  const openOrders = useCallback(() => {
    setView("orders");
    setTab("orders");
  }, []);
  // a scenario row only pre-fills the ticket; the user adds the size and creates / approves the draft
  const onOrder = useCallback((p: Omit<Prefill, "nonce">) => {
    setSymbol(p.symbol);
    setPrefill({ ...p, nonce: Date.now() });
    openOrders();
  }, [openOrders]);
  const derivedLines = useMemo(
    () => [...(positions.data?.overlays ?? []), ...(drafts.data?.overlays ?? []), ...thesisLines(thesis.data?.thesis, symbol)]
      .filter((o) => !o.symbol || o.symbol === symbol),
    [positions.data, drafts.data, thesis.data, symbol],
  );
  const orders = (
    <OrdersPage symbol={symbol} onSymbol={setSymbol} quick={quick} drafts={drafts.data?.drafts ?? []}
      done={drafts.data?.done ?? []} onDraftsChanged={drafts.refresh}
      prefill={prefill} scenarios={latestScen} onOrder={onOrder} />
  );

  const chart = (
    <ChartPanel symbol={symbol} onSymbol={setSymbol} quick={quick} candlesMs={candlesMs} fills={fills}
      derivedLines={derivedLines} now={now} overlayTick={overlayTick} onScreen={onScreen} defaults={chartDefaults} />
  );
  const myInfo = (
    <>
      <ThesisCard key={symbol} symbol={symbol} tick={0} compact onOpenResearch={() => openResearch(symbol)} />
      <MyInfo symbol={symbol} positions={positions.data} error={positions.error} onSaved={positions.refresh} />
    </>
  );
  const pnl = <PnlPanel pollMs={quotesMs} now={now} />;
  const reserved = <ReservedPanel pollMs={quotesMs} now={now} onManage={openOrders} />;
  const chat = (
    <ChatPanel symbol={symbol} kind="desk" getScreen={getScreen} onOverlayChanged={onOverlayChanged} onOrder={onOrder}
      onScenarios={setLatestScen} onDraftCreated={drafts.refresh} onOpenOrders={openOrders} />
  );
  const book = <OrderbookPanel symbol={symbol} />;
  const research = (
    <ResearchPage symbol={symbol} onSymbol={setSymbol} watch={watch} now={now} phone={phone}
      onOpenDesk={() => { setView("desk"); setTab("chart"); }} />
  );
  const news = <NewsPanel symbol={symbol} />;

  return (
    <OpenResearchProvider value={openResearch}>
    <div className={`app${phone ? " phone" : ""}`} lang={lang}>
      <div className="topbar">
      <div className="topbar-left"><HealthBar /></div>
      <div className="top-switch">
        {!phone && (
          <span className="seg">
            <button className={`small ${view === "desk" ? "on" : ""}`} onClick={() => setView("desk")}>{t("view.desk")}</button>
            <button className={`small ${view === "research" ? "on" : ""}`} onClick={() => setView("research")}>{t("view.research")}</button>
            <button className={`small ${view === "orders" ? "on" : ""}`} onClick={() => setView("orders")}>{t("view.orders")}</button>
            <button className={`small ${view === "journal" ? "on" : ""}`} onClick={() => setView("journal")}>{t("view.journal")}</button>
            <button className={`small ${view === "settings" ? "on" : ""}`} onClick={() => setView("settings")}>{t("view.settings")}</button>
          </span>
        )}
        <button className="small" onClick={() => {
          const next = lang === "en" ? "ko" : "en";
          setLang(next);
          void settingsApi.save({ general: { language: next } }).catch(() => undefined);
        }}
          title={t("lang.note")}>{t("lang.switch")}</button>
        {auth.status?.required && (
          <button className="small" onClick={auth.logout} title={auth.status.remote ? t("login.viaPhone") : undefined}>
            {t("login.logout")}
          </button>
        )}
      </div>
      </div>
      <TopStrip pollMs={quotesMs} now={now} />
      {phone ? (
        <>
          <main className="tab-body">
            {tab === "chart" && <>{chart}{myInfo}</>}
            {tab === "pnl" && <>{pnl}{reserved}</>}
            {tab === "chat" && chat}
            {tab === "book" && <>{book}<OptionsPanel symbol={symbol} />{news}</>}
            {tab === "research" && research}
            {tab === "orders" && orders}
            {tab === "journal" && <Journal />}
            {tab === "settings" && <SettingsPage onSaved={onSettingsSaved} />}
          </main>
          <nav className="tabs">
            {TABS.map(([k, label]) => (
              <button key={k} className={tab === k ? "on" : ""} onClick={() => setTab(k)}>{t(label)}</button>
            ))}
          </nav>
        </>
      ) : view === "research" ? (
        research
      ) : view === "orders" ? (
        <main className="orders-main">{orders}</main>
      ) : view === "journal" ? (
        <main className="orders-main"><Journal /></main>
      ) : view === "settings" ? (
        <main className="orders-main"><SettingsPage onSaved={onSettingsSaved} /></main>
      ) : tablet ? (
        <main className="tablet-desk">
          {chart}
          <nav className="tablet-tabs">
            {DESK_TABS.map(([k, label]) => (
              <button key={k} className={deskTab === k ? "on" : ""} onClick={() => setDeskTab(k)}>{t(label)}</button>
            ))}
          </nav>
          {/* every panel stays mounted (a chat answer keeps streaming); only the chosen one is shown */}
          <div className="tablet-pane" hidden={deskTab !== "chat"}>{chat}</div>
          <div className="tablet-pane" hidden={deskTab !== "pnl"}>{pnl}{reserved}</div>
          <div className="tablet-pane" hidden={deskTab !== "book"}>{book}<OptionsPanel symbol={symbol} />{news}</div>
          <div className="tablet-pane" hidden={deskTab !== "info"}>{myInfo}</div>
        </main>
      ) : (
        <DeskSplit
          left={<>
            {chart}
            <div className="below-chart">
              {pnl}
              {reserved}
              {myInfo}
              <OptionsPanel symbol={symbol} />
              {book}
              {news}
            </div>
          </>}
          right={chat} />
      )}
    </div>
    </OpenResearchProvider>
  );
}
