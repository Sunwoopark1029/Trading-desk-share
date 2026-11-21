// Research view (phase 6): Explore rail on the left, the Company view of the active ticker in the middle and the
// research chat (part C) on the right (below on narrower screens; a Company / Chat switch on the phone).
import { useState } from "react";
import ChatPanel from "../chat/ChatPanel";
import ThesisCard from "../components/ThesisCard";
import ExploreRail, { Star, type WatchState } from "./ExploreRail";
import CompanyView from "./CompanyView";
import TickerSearch from "../components/TickerSearch";
import { useT } from "../i18n";

interface Props {
  symbol: string;
  onSymbol: (s: string) => void;
  onOpenDesk: () => void;
  watch: WatchState;
  now: number;
  phone?: boolean;
}

export default function ResearchPage({ symbol, onSymbol, onOpenDesk, watch, now, phone = false }: Props) {
  const { t } = useT();
  const [pane, setPane] = useState<"explore" | "company" | "chat">("company");   // phone only
  const [thesisTick, setThesisTick] = useState(0);  // a thesis was saved / edited: reload the thesis card
  const bumpThesis = () => setThesisTick((n) => n + 1);
  const pickSymbol = (s: string) => {               // on the phone, picking a ticker opens its company page
    onSymbol(s);
    if (phone) setPane("company");
  };
  const chat = (
    <ChatPanel symbol={symbol} kind="research" getScreen={() => ({ symbol })} onOverlayChanged={() => undefined}
      onOpenDesk={onOpenDesk} onThesisChanged={bumpThesis} onPickSymbol={pickSymbol} />
  );
  return (
    <main className="research with-chat">
      <div className={`research-rail${phone && pane !== "explore" ? " hidden" : ""}`}>
        <ExploreRail symbol={symbol} onSymbol={pickSymbol} watch={watch} now={now} />
      </div>
      {phone && (
        <span className="seg research-pane">
          <button className={pane === "explore" ? "on" : ""} onClick={() => setPane("explore")}>{t("co.paneExplore")}</button>
          <button className={pane === "company" ? "on" : ""} onClick={() => setPane("company")}>{t("co.paneCompany")}</button>
          <button className={pane === "chat" ? "on" : ""} onClick={() => setPane("chat")}>{t("co.paneChat")}</button>
        </span>
      )}
      <div className={`research-chat${phone && pane !== "chat" ? " hidden" : ""}`}>{chat}</div>
      <section className={`panel research-main${phone && pane !== "company" ? " hidden" : ""}`}>
        <div className="panel-head">
          <h2>{symbol} <Star sym={symbol} watch={watch} /></h2>
          <span className="research-actions">
            {/* any US ticker: opens its Company view without adding it to a folder */}
            <span title={t("co.searchHint")}><TickerSearch symbol={symbol} quick={[]} onChange={pickSymbol} /></span>
            <button onClick={onOpenDesk}>{t("ex.openDesk")}</button>
          </span>
        </div>
        <ThesisCard key={symbol} symbol={symbol} tick={thesisTick} />
        <CompanyView key={symbol} symbol={symbol} now={now} />
      </section>
    </main>
  );
}
