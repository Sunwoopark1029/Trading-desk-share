import { useState } from "react";
import { api, type Plan, type PlanOut, type PositionsOut } from "../api";
import { fmtKstDateTime, fmtNum, fmtPrice } from "../format";
import { trBackend, useT } from "../i18n";

interface Props {
  symbol: string;
  positions: PositionsOut | null;
  error: string | null;
  onSaved: () => void;
}

const PRICE_RE = /^\d+(\.\d+)?$/;

function toPlan(p: PlanOut): Plan | unknown {
  if (p.error) return p.raw; // invalid entry in the file: sent back unchanged (save will report it)
  const out: Plan = { symbol: p.symbol, side: p.side, price: p.price, currency: p.currency };
  if (p.id) out.id = p.id;
  if (p.qty) out.qty = p.qty;
  if (p.note) out.note = p.note;
  return out;
}

/** Scheduled buy/sell prices (saved to positions.json) → horizontal lines on the chart. */
export default function MyInfo({ symbol, positions, error, onSaved }: Props) {
  const { t } = useT();
  const [side, setSide] = useState<"buy" | "sell">("buy");
  const [currency, setCurrency] = useState<"" | "USD" | "KRW">("");
  const [price, setPrice] = useState("");
  const [note, setNote] = useState("");
  const [msg, setMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const plans = positions?.plans ?? [];
  const fx = positions?.fx;

  const save = async (next: unknown[], okMsg: string) => {
    setBusy(true);
    try {
      await api.savePlans(next as Plan[]);
      setMsg(okMsg);
      onSaved();
      return true;
    } catch (e) {
      setMsg(t("plans.notSaved", { msg: trBackend(e instanceof Error ? e.message : String(e)) }));
      return false;
    } finally {
      setBusy(false);
    }
  };

  const add = async () => {
    const p = price.trim();
    if (!currency) return setMsg(t("plans.needCur"));
    if (!PRICE_RE.test(p) || Number(p) <= 0) return setMsg(t("plans.badPrice"));
    const plan: Plan = { symbol, side, price: p, currency };
    if (note.trim()) plan.note = note.trim();
    if (await save([...plans.map(toPlan), plan],
      t("plans.saved", { side: t(side === "buy" ? "plans.side.buy" : "plans.side.sell"), cur: currency, price: p, sym: symbol }))) {
      setPrice("");
      setNote("");
    }
  };

  const removeAt = (i: number) => save(plans.filter((_, j) => j !== i).map(toPlan), t("plans.removed"));

  return (
    <section className="panel myinfo">
      <div className="panel-head">
        <h2>{t("plans.title")}</h2>
        <span className="muted small">{t("plans.sub")}</span>
      </div>
      {error && <div className="st-error small">positions.json: {trBackend(error)}</div>}

      <div className="plan-form">
        <span className="muted">{symbol}</span>
        <select value={side} onChange={(e) => setSide(e.target.value as "buy" | "sell")} aria-label="Side">
          <option value="buy">{t("plans.buy")}</option>
          <option value="sell">{t("plans.sell")}</option>
        </select>
        <input value={price} onChange={(e) => setPrice(e.target.value)} placeholder={t("plans.price")} inputMode="decimal"
          aria-label="Price" className="price-in" />
        <select value={currency} onChange={(e) => setCurrency(e.target.value as "" | "USD" | "KRW")} aria-label="Currency">
          <option value="">{t("plans.currency")}</option>
          <option value="USD">USD</option>
          <option value="KRW">KRW</option>
        </select>
        <input value={note} onChange={(e) => setNote(e.target.value)} placeholder={t("plans.note")} aria-label="Note" />
        <button onClick={add} disabled={busy}>{t("plans.add")}</button>
      </div>
      {msg && <div className="action-msg">{msg}</div>}

      {plans.length === 0 && <div className="muted small">{t("plans.none")}</div>}
      <ul className="plan-list">
        {plans.map((p, i) => (
          <li key={p.id ?? i} className={p.symbol === symbol ? "" : "muted"}>
            {p.error ? (
              <span className="st-error">{t("plans.invalid", { msg: p.error })}</span>
            ) : (
              <>
                <b className={p.side === "buy" ? "up" : "down"}>{t(p.side === "buy" ? "plans.side.buy" : "plans.side.sell")}</b> {p.symbol} {fmtPrice(p.price, p.currency)}
                {p.converted && (
                  <span className="muted small">
                    {" "}≈ {fmtPrice(p.price_usd ?? null, "USD")}
                    {p.fx?.rate ? t("plans.fxRef", { rate: fmtNum(p.fx.rate, 2), t: fmtKstDateTime(p.fx.time) }) : ` ${t("plans.noFx")}`}
                  </span>
                )}
                {!p.converted && p.price_krw && <span className="muted small"> ≈ {fmtPrice(p.price_krw, "KRW")}</span>}
                {p.note && <span className="muted small"> · {p.note}</span>}
              </>
            )}{" "}
            <button className="small" onClick={() => removeAt(i)} disabled={busy}>{t("common.delete")}</button>
          </li>
        ))}
      </ul>
      {fx && !fx.rate && <div className="muted small">{t("plans.needFx")}</div>}
      <div className="muted small">{t("plans.avgNote")}</div>
    </section>
  );
}
