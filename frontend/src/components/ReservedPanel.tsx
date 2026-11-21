import { accountApi } from "../api";
import { fmtKstDateTime } from "../format";
import { useT } from "../i18n";
import { condCurrency, useOrderText } from "../orders/orderText";
import { usePoll } from "../usePoll";
import { TickerLink } from "../TickerLink";
import StatusLine from "./Status";

interface Props {
  pollMs: number;
  now: number;
  onManage: () => void;
}

/** Desk: open reserved (conditional) orders + open regular orders. Read-only; changes happen on the Orders tab. */
export default function ReservedPanel({ pollMs, now, onManage }: Props) {
  const { t } = useT();
  const { side, tif, price, leg } = useOrderText();
  const acct = usePoll(() => accountApi.get(), pollMs);   // cached on the backend, no extra Toss call
  const a = acct.data;

  return (
    <section className="panel rsv-panel">
      <div className="panel-head">
        <h2>{t("rsv.title")}</h2>
        <button className="small" onClick={onManage}>{t("rsv.manage")}</button>
      </div>
      <div className="small">
        <StatusLine status={a?.status} updated={a?.updated} error={a?.error} now={now} label="Toss"
          fetchError={acct.error && !a ? acct.error : null} />
      </div>

      <h3 className="rsv-sub">{t("ord.cond")}</h3>
      {a && a.open_conditional.length === 0 && <div className="muted small">{t("ord.noCond")}</div>}
      {a?.open_conditional.map((c) => (
        <div key={c.conditional_order_id} className="rsv-row">
          <div>
            <b><TickerLink sym={c.symbol} /></b> · {c.type} · {t("ord.qty")} {c.quantity}
            <span className="muted small"> · {t("ord.expires")} {c.expire_date ?? "—"} · {c.status}</span>
          </div>
          <div className="small">{leg(c.first, condCurrency(c))}</div>
          {c.second && <div className="small">{leg(c.second, condCurrency(c))}</div>}
        </div>
      ))}

      <h3 className="rsv-sub">{t("ord.open")}</h3>
      {a && a.open_orders.length === 0 && <div className="muted small">{t("ord.noOpen")}</div>}
      {a?.open_orders.map((o) => (
        <div key={o.order_id} className="rsv-row">
          <div>
            <b><TickerLink sym={o.symbol} /></b> · <span className={o.side === "BUY" ? "up" : "down"}>{side(o.side)}</span>{" "}
            {o.quantity ?? (o.order_amount ? `USD ${o.order_amount}` : "—")} @ {price(o)}
            <span className="muted small"> · {t("ord.filled")} {o.execution?.filled_quantity ?? "0"} · {tif(o.time_in_force)} · {o.status}</span>
          </div>
          <div className="muted small">{fmtKstDateTime(o.ordered_at)}</div>
        </div>
      ))}
    </section>
  );
}
