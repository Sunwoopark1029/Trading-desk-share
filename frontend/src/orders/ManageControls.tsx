import { useState } from "react";
import { orderApi, type Issue } from "../api";
import { useT } from "../i18n";
import { issueText } from "./OrderTicket";

export interface ManageTarget {
  type: "order" | "conditional" | "simulated";
  id: string;
  conditional: boolean;
  price?: string | null;                                   // normal order: current limit price
  legs?: { trigger_price: string | null; order_price: string | null }[];
  expire_date?: string | null;
  lastPrice?: string | null;                               // for the "current price" button
}

interface Props {
  target: ManageTarget;
  onDraft: () => void;
}

/** Change price / Edit / Cancel for one open order. Creates a DRAFT; approval happens in Drafts. */
export default function ManageControls({ target, onDraft }: Props) {
  const { t } = useT();
  const [mode, setMode] = useState<"none" | "edit">("none");
  const [orderType, setOrderType] = useState<"LIMIT" | "MARKET">("LIMIT");
  const [price, setPrice] = useState(target.price ?? "");
  const [legs, setLegs] = useState((target.legs ?? []).map((l) => ({ trigger_price: l.trigger_price ?? "", order_price: l.order_price ?? "" })));
  const [expire, setExpire] = useState(target.expire_date ?? "");
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);

  const create = async (action: "modify" | "cancel") => {
    setMsg(null);
    const body: Record<string, unknown> = { action, target_type: target.type, target_id: target.id, source: "panel" };
    if (action === "modify") {
      if (target.conditional) Object.assign(body, { legs, expire_date: expire });
      else Object.assign(body, { order_type: orderType, price: orderType === "LIMIT" ? price : "" });
    }
    try {
      const d = await orderApi.createDraft(body) as { status: string; errors?: Issue[] };
      if (d.status === "invalid" && d.errors?.length) {
        setMsg({ ok: false, text: d.errors.map((i) => issueText(t, i)).join(" · ") });
      } else {
        setMsg({ ok: true, text: t("mg.drafted") });
        setMode("none");
      }
      onDraft();
    } catch (e) {
      setMsg({ ok: false, text: e instanceof Error ? e.message : String(e) });
    }
  };

  return (
    <div className="manage">
      {mode === "none" ? (
        <span className="manage-btns">
          <button className="small" onClick={() => setMode("edit")}>{target.conditional ? t("mg.edit") : t("mg.changePrice")}</button>
          <button className="small" onClick={() => void create("cancel")}>{t("mg.cancel")}</button>
        </span>
      ) : (
        <div className="manage-form">
          {!target.conditional ? (
            <>
              <span className="seg">
                <button className={`small ${orderType === "LIMIT" ? "on" : ""}`} onClick={() => setOrderType("LIMIT")}>{t("tk.limit")}</button>
                <button className={`small ${orderType === "MARKET" ? "on" : ""}`} onClick={() => setOrderType("MARKET")}>{t("tk.market")}</button>
              </span>
              {orderType === "LIMIT" && (
                <>
                  <input value={price} onChange={(e) => setPrice(e.target.value)} inputMode="decimal" placeholder="USD" />
                  {target.lastPrice && <button className="small" onClick={() => setPrice(String(target.lastPrice))}>{t("tk.current")}</button>}
                </>
              )}
            </>
          ) : (
            <>
              {legs.map((l, i) => (
                <span key={i} className="manage-leg">
                  {t("tk.trigger")} <input value={l.trigger_price} onChange={(e) => setLegs(legs.map((x, j) => j === i ? { ...x, trigger_price: e.target.value } : x))} />
                  {l.order_price !== "" || target.legs?.[i]?.order_price ? (
                    <>{t("tk.orderPrice")} <input value={l.order_price} onChange={(e) => setLegs(legs.map((x, j) => j === i ? { ...x, order_price: e.target.value } : x))} /></>
                  ) : null}
                </span>
              ))}
              {t("tk.expire")} <input type="date" value={expire} onChange={(e) => setExpire(e.target.value)} />
            </>
          )}
          <button className="small" onClick={() => void create("modify")}>{t("tk.create")}</button>
          <button className="small" onClick={() => setMode("none")}>{t("ap.cancel")}</button>
        </div>
      )}
      {msg && <div className={`small ${msg.ok ? "muted" : "st-error"}`}>{msg.text}</div>}
    </div>
  );
}
