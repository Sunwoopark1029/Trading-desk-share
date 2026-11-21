import { useEffect, useRef, useState } from "react";
import { marketApi, orderApi, type DraftView, type Issue } from "../api";
import TickerSearch from "../components/TickerSearch";
import { fmtNum, fmtPrice, num, priceDp } from "../format";
import { useT, type Key } from "../i18n";
import { usePoll } from "../usePoll";

export type Kind = "BUY" | "SELL" | "COND_BUY" | "COND_SELL" | "OCO" | "OTO";
const KINDS: Kind[] = ["BUY", "SELL", "COND_BUY", "COND_SELL", "OCO", "OTO"];

export function issueText(t: (k: Key, v?: Record<string, string | number>) => string, i: Issue): string {
  const key = `verr.${i.code}` as Key;
  const s = t(key, i.params);
  return s === key ? i.code : s;
}

function inMonth(): string {
  const d = new Date(Date.now() + 30 * 86400_000 + 9 * 3600_000);
  return d.toISOString().slice(0, 10);
}

/** Values from a chat scenario row. Only fills the ticket; the user checks the size (adds it if empty), the expiry,
 *  and creates the draft. quantity = AI-suggested whole shares, only when the user asked the chat for one. */
export interface Prefill {
  nonce: number;
  symbol: string;
  kind: Kind;
  order_type: "LIMIT" | "MARKET";
  price?: string;
  legs?: { trigger_price: string; order_price: string }[];
  quantity?: string;
  from: string;
}

interface Props {
  prefill?: Prefill | null;
  symbol: string;
  onSymbol: (s: string) => void;
  quick: string[];
  onDraft: (d: DraftView) => void;
}

/** Order ticket: the user's exact values go to the backend as typed (strings). Creates a DRAFT only. */
export default function OrderTicket({ prefill, symbol, onSymbol, quick, onDraft }: Props) {
  const { t } = useT();
  const [kind, setKind] = useState<Kind>("BUY");
  const [orderType, setOrderType] = useState<"LIMIT" | "MARKET">("LIMIT");
  const [price, setPrice] = useState("");
  const [sizeMode, setSizeMode] = useState<"shares" | "percent" | "amount">("shares");
  const [qty, setQty] = useState("");
  const [pct, setPct] = useState("");
  const [amount, setAmount] = useState("");
  const [tif, setTif] = useState<"DAY" | "CLS">("DAY");
  const [legs, setLegs] = useState([{ trigger_price: "", order_price: "" }, { trigger_price: "", order_price: "" }]);
  const [expire, setExpire] = useState(inMonth());
  const [highValue, setHighValue] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const book = usePoll(() => marketApi.orderbook(symbol), 2000, [symbol]);
  const [note, setNote] = useState<{ from: string; cond: boolean; qty: string | null } | null>(null);
  const box = useRef<HTMLElement>(null);

  useEffect(() => {
    if (!prefill) return;
    const cond = prefill.kind !== "BUY" && prefill.kind !== "SELL";
    setKind(prefill.kind);
    setOrderType(prefill.order_type);
    setPrice(prefill.price ?? "");
    if (prefill.legs) setLegs([...prefill.legs, { trigger_price: "", order_price: "" }].slice(0, 2));
    setSizeMode("shares");
    setQty(prefill.quantity ?? "");               // AI-suggested (user asked) or empty; the user checks / edits it
    setPct("");
    setAmount("");
    if (cond) setExpire("");                       // the user picks the expiry themselves
    setErr(null);
    setNote({ from: prefill.from, cond, qty: prefill.quantity ?? null });
    box.current?.scrollIntoView({ block: "start", behavior: "smooth" });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [prefill?.nonce]);

  const conditional = kind !== "BUY" && kind !== "SELL";
  const twoLegs = kind === "OCO" || kind === "OTO";
  useEffect(() => {
    if (twoLegs) setOrderType("LIMIT");
    if (conditional && sizeMode === "amount") setSizeMode("shares");
  }, [twoLegs, conditional, sizeMode]);

  const b = book.data;
  const last = b?.trades[0]?.price ?? null;
  const dp = priceDp(num(last ?? b?.spread?.mid ?? null) ?? 1);
  const side = kind === "BUY" || kind === "COND_BUY" || kind === "OTO" ? "BUY" : "SELL";

  const useCurrent = () => {
    if (last) setPrice(String(last)); // the latest trade price, exactly as Toss sent it
  };

  const create = async () => {
    setBusy(true);
    setErr(null);
    const body: Record<string, unknown> = {
      kind, symbol, currency: "USD", order_type: orderType, size_mode: sizeMode, source: "panel",
      quantity: qty, percent: pct, amount, confirm_high_value: highValue,
    };
    if (conditional) {
      body.legs = twoLegs ? legs : [legs[0]];
      body.expire_date = expire;
    } else {
      body.price = orderType === "LIMIT" ? price : "";
      body.time_in_force = tif;
    }
    try {
      onDraft(await orderApi.createDraft(body));
      setNote(null);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const setLeg = (i: number, field: "trigger_price" | "order_price", v: string) =>
    setLegs((ls) => ls.map((l, j) => (j === i ? { ...l, [field]: v } : l)));
  const legLabel = (i: number) =>
    kind === "OCO" ? t(i === 0 ? "tk.leg1" : "tk.leg2") : kind === "OTO" ? t(i === 0 ? "tk.otoBuy" : "tk.otoSell") : "";

  return (
    <section className="panel ticket" ref={box}>
      <div className="panel-head"><h2>{t("tk.title")}</h2></div>
      {note && (
        <div className="prefill-note small">
          {t("tk.prefilled", { from: note.from })}{note.qty && t("tk.prefilledQty", { q: note.qty })}{note.cond && t("tk.prefilledExp")}
          <button className="small" onClick={() => setNote(null)}>✕</button>
        </div>
      )}
      <div className="tk-row">
        <TickerSearch symbol={symbol} quick={quick} onChange={onSymbol} />
        {b?.spread && (
          <span className="small">
            {t("tk.bidAsk", { b: fmtNum(b.spread.bid, dp), a: fmtNum(b.spread.ask, dp), s: `${fmtNum(b.spread.abs, dp)} (${fmtNum(b.spread.pct, 3)}%)` })}
          </span>
        )}
        {last && <span className="small muted">{t("tk.last", { p: fmtPrice(last, "USD") })}</span>}
      </div>

      <div className="seg tk-kinds">
        {KINDS.map((k) => (
          <button key={k} className={`${kind === k ? "on" : ""} ${k.includes("BUY") || k === "OTO" ? "buyish" : "sellish"}`}
            onClick={() => setKind(k)}>{t(`tk.k.${k}` as Key)}</button>
        ))}
      </div>
      {kind === "OCO" && <div className="muted small">{t("tk.oco")}</div>}
      {kind === "OTO" && <div className="muted small">{t("tk.oto")}</div>}
      {(kind === "COND_BUY" || kind === "COND_SELL") && <div className="muted small">{t("tk.reserved")}</div>}

      <div className="tk-grid">
        <label>{t("ord.type")}</label>
        <div className="seg">
          <button className={orderType === "LIMIT" ? "on" : ""} onClick={() => setOrderType("LIMIT")}>{t("tk.limit")}</button>
          <button className={orderType === "MARKET" ? "on" : ""} disabled={twoLegs} onClick={() => setOrderType("MARKET")}>{t("tk.market")}</button>
        </div>

        {!conditional && orderType === "LIMIT" && (
          <>
            <label>{t("tk.price")}</label>
            <div className="tk-inline">
              <input value={price} onChange={(e) => setPrice(e.target.value)} inputMode="decimal" placeholder="USD" />
              <button className="small" onClick={useCurrent} disabled={!last} title={t("tk.currentTitle")}>{t("tk.current")}</button>
            </div>
          </>
        )}

        {conditional && (twoLegs ? [0, 1] : [0]).map((i) => (
          <div key={i} className="tk-leg">
            {legLabel(i) && <div className="small"><b>{legLabel(i)}</b></div>}
            <label>{t("tk.trigger")}</label>
            <input value={legs[i].trigger_price} onChange={(e) => setLeg(i, "trigger_price", e.target.value)} inputMode="decimal" />
            {orderType === "LIMIT" && (
              <>
                <label>{t("tk.orderPrice")}</label>
                <input value={legs[i].order_price} onChange={(e) => setLeg(i, "order_price", e.target.value)} inputMode="decimal" />
              </>
            )}
          </div>
        ))}

        <label>{t("tk.size")}</label>
        <div className="tk-inline">
          <select value={sizeMode} onChange={(e) => setSizeMode(e.target.value as typeof sizeMode)}>
            <option value="shares">{t("tk.shares")}</option>
            <option value="percent">{side === "BUY" ? t("tk.percent") : t("tk.percentSell")}</option>
            {!conditional && <option value="amount">{t("tk.amount")}</option>}
          </select>
          {sizeMode === "shares" && <input value={qty} onChange={(e) => setQty(e.target.value)} inputMode="decimal" placeholder={t("tk.qty")} />}
          {sizeMode === "percent" && <input value={pct} onChange={(e) => setPct(e.target.value)} inputMode="decimal" placeholder={t("tk.pct")} />}
          {sizeMode === "amount" && <input value={amount} onChange={(e) => setAmount(e.target.value)} inputMode="decimal" placeholder={t("tk.usd")} />}
        </div>

        {!conditional ? (
          <>
            <label>{t("tk.tif")}</label>
            <div className="seg">
              <button className={tif === "DAY" ? "on" : ""} onClick={() => setTif("DAY")}>{t("tk.day")}</button>
              <button className={tif === "CLS" ? "on" : ""} onClick={() => setTif("CLS")}>{t("tk.cls")}</button>
            </div>
          </>
        ) : (
          <>
            <label>{t("tk.expire")}</label>
            <input type="date" value={expire} onChange={(e) => setExpire(e.target.value)} />
          </>
        )}
      </div>

      <label className="small tk-hv">
        <input type="checkbox" checked={highValue} onChange={() => setHighValue(!highValue)} /> {t("tk.highValue")}
      </label>
      <div className="tk-actions">
        <button className={`tk-create ${side === "BUY" ? "buy" : "sell"}`} onClick={() => void create()} disabled={busy}>
          {t("tk.create")}
        </button>
        {err && <span className="st-error small">{err}</span>}
      </div>
    </section>
  );
}
