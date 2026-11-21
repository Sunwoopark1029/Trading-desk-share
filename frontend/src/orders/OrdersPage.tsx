import { accountApi, orderApi, type ScenarioTable as Table, type DraftView, type SubmitResult } from "../api";
import Drafts from "./Drafts";
import ManageControls from "./ManageControls";
import OrderTicket, { type Prefill } from "./OrderTicket";
import RulesPanel from "./RulesPanel";
import { condCurrency, useOrderText } from "./orderText";
import ScenarioTable from "../chat/ScenarioTable";
import { fmtKstDateTime, fmtKstTime, fmtMoney, fmtNum, fmtPrice, signClass, tickerLabel } from "../format";
import { trBackend, useT } from "../i18n";
import { usePoll } from "../usePoll";
import { TickerLink } from "../TickerLink";

interface Props {
  symbol: string;
  onSymbol: (s: string) => void;
  quick: string[];
  drafts: DraftView[];
  done: (DraftView & { result: SubmitResult | null })[];
  onDraftsChanged: () => void;
  prefill: Prefill | null;
  scenarios: Table[];
  onOrder: (p: Omit<Prefill, "nonce">) => void;
}

/** Orders tab: ticket -> drafts (validated in code) + read-only account data. Sending comes later. */
export default function OrdersPage({ symbol, onSymbol, quick, drafts, done, onDraftsChanged, prefill, scenarios, onOrder }: Props) {
  const { t, lang } = useT();
  const acct = usePoll(() => accountApi.get(), 10_000);
  const sims = usePoll(() => orderApi.simulated(), 5_000);
  const a = acct.data;
  const changed = () => {
    onDraftsChanged();
    sims.refresh();
  };

  const { side, tif, price, leg } = useOrderText();

  return (
    <div className="orders-page">
      <div className={`dry-banner ${a && !a.dry_run ? "live" : ""}`}>
        {a && !a.dry_run ? t("ord.live") : t("ord.dryRun")}
      </div>

      <section className="panel from-chat">
        <div className="panel-head"><h2>{t("scen.fromChat")}</h2></div>
        {scenarios.length === 0
          ? <div className="muted small">{t("scen.noneYet")}</div>
          : scenarios.map((tb, i) => <ScenarioTable key={i} table={tb} onOrder={onOrder} />)}
      </section>
      <RulesPanel symbol={symbol} pollMs={2000} />
      <div className="orders-cols">
        <div className="orders-col">
          <OrderTicket prefill={prefill} symbol={symbol} onSymbol={onSymbol} quick={quick} onDraft={onDraftsChanged} />
          <Drafts drafts={drafts} done={done} onChanged={changed} />
          {(sims.data?.orders.length ?? 0) > 0 && (
            <section className="panel">
              <div className="panel-head"><h2>{t("mg.simTitle")}</h2><span className="tag">DRY RUN</span></div>
              {sims.data!.orders.map((s) => {
                const v = s.values as Record<string, unknown>;
                const legs = (v.legs as { trigger_price: string | null; order_price: string | null }[] | undefined) ?? [];
                return (
                  <div key={s.id} className={`draft ${s.status === "OPEN" ? "" : "sent"}`}>
                    <div>{lang === "ko" ? s.summary_ko : s.summary_en}</div>
                    <div className="small muted">{s.id} · {s.status}</div>
                    {s.status === "OPEN" && (
                      <ManageControls target={{ type: "simulated", id: s.id, conditional: !!v.conditional,
                        price: (v.price as string | null) ?? null, legs, expire_date: (v.expire_date as string | null) ?? null }}
                        onDraft={changed} />
                    )}
                  </div>
                );
              })}
            </section>
          )}
        </div>
        <div className="orders-col">
      <section className="panel">
        <div className="panel-head">
          <h2>{t("ord.account")} {a?.account ? `· ${a.account.masked}` : ""}</h2>
          <span className="small muted">
            {a?.updated && t("ord.updated", { t: fmtKstTime(a.updated) })}{" "}
            <button className="small" onClick={() => void accountApi.get(true).then(acct.refresh)}>{t("ord.refresh")}</button>
          </span>
        </div>
        {(a?.error || acct.error) && <div className="st-error small">{trBackend(a?.error ?? acct.error)}</div>}
        {a?.note && <div className="st-stale small">{a.note}</div>}
        <div className="bp">
          <span>{t("ord.bp")}</span>
          <b>{fmtMoney(a?.buying_power.usd ?? null, "USD")}</b>
          <span className="muted">{fmtMoney(a?.buying_power.krw ?? null, "KRW")}</span>
        </div>
        <div className="muted small">{t("ord.bpNote")}</div>
      </section>

      <section className="panel">
        <div className="panel-head"><h2>{t("ord.holdings")}</h2></div>
        {a?.holdings && a.holdings.items.length === 0 && <div className="muted small">{t("ord.noHoldings")}</div>}
        {a?.holdings && a.holdings.items.length > 0 && (
          <table className="otable">
            <thead><tr><th></th><th className="num">{t("ord.qty")}</th><th className="num">{t("ord.avg")}</th>
              <th className="num">{t("ord.last")}</th><th className="num">{t("ord.pl")}</th></tr></thead>
            <tbody>
              {a.holdings.items.map((h) => (
                <tr key={h.symbol}>
                  <th title={`${h.name} (${h.symbol})`}><TickerLink sym={h.symbol}>{tickerLabel(h.symbol, h.name)}</TickerLink></th>
                  <td className="num">{fmtNum(h.quantity, Number(h.quantity) % 1 ? 6 : 0)}</td>
                  <td className="num">{fmtPrice(h.average_purchase_price, h.currency)}</td>
                  <td className="num">{fmtPrice(h.last_price, h.currency)}</td>
                  <td className={`num ${signClass(h.profit_loss.amount ?? null)}`}>
                    {h.profit_loss.amount !== undefined ? fmtMoney(h.profit_loss.amount, h.currency === "KRW" ? "KRW" : "USD", true) : "—"}
                    {h.profit_loss.amount !== undefined && Number(h.market_value?.purchaseAmount) > 0 && (
                      // % from two documented amounts (Toss's own rate field has an undocumented unit)
                      <div className="small">{fmtNum(Number(h.profit_loss.amount) / Number(h.market_value.purchaseAmount) * 100, 2)}%</div>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      <section className="panel">
        <div className="panel-head"><h2>{t("ord.open")}</h2></div>
        {a && a.open_orders.length === 0 && <div className="muted small">{t("ord.noOpen")}</div>}
        {a && a.open_orders.length > 0 && (
          <table className="otable">
            <thead><tr><th>{t("ord.time")}</th><th></th><th>{t("ord.side")}</th><th>{t("ord.type")}</th>
              <th className="num">{t("ord.price")}</th><th className="num">{t("ord.qty")}</th>
              <th className="num">{t("ord.filled")}</th><th>{t("ord.tif")}</th><th>{t("ord.status")}</th><th></th></tr></thead>
            <tbody>
              {a.open_orders.map((o) => (
                <tr key={o.order_id}>
                  <td className="muted">{fmtKstDateTime(o.ordered_at)}</td>
                  <th><TickerLink sym={o.symbol} /></th>
                  <td className={o.side === "BUY" ? "up" : "down"}>{side(o.side)}</td>
                  <td>{o.order_type}</td>
                  <td className="num">{price(o)}</td>
                  <td className="num">{o.quantity ?? (o.order_amount ? `USD ${o.order_amount}` : "—")}</td>
                  <td className="num">{o.execution?.filled_quantity ?? "0"}</td>
                  <td>{tif(o.time_in_force)}</td>
                  <td>{o.status}</td>
                  <td><ManageControls target={{ type: "order", id: o.order_id, conditional: false, price: o.price }} onDraft={changed} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      <section className="panel">
        <div className="panel-head"><h2>{t("ord.cond")}</h2></div>
        {a && a.open_conditional.length === 0 && <div className="muted small">{t("ord.noCond")}</div>}
        {a && a.open_conditional.length > 0 && (
          <table className="otable">
            <thead><tr><th></th><th>{t("ord.type")}</th><th className="num">{t("ord.qty")}</th>
              <th>{t("ord.trigger")}</th><th>{t("ord.expires")}</th><th>{t("ord.status")}</th><th></th></tr></thead>
            <tbody>
              {a.open_conditional.map((c) => (
                <tr key={c.conditional_order_id}>
                  <th><TickerLink sym={c.symbol} /></th>
                  <td>{c.type} · {c.order_type}</td>
                  <td className="num">{c.quantity}</td>
                  <td>{leg(c.first, condCurrency(c))}{c.second && <div>{leg(c.second, condCurrency(c))}</div>}</td>
                  <td>{c.expire_date ?? "—"}</td>
                  <td>{c.status}</td>
                  <td>{(c.status === "WATCHING" || c.status === "PAUSED") && (
                    <ManageControls target={{ type: "conditional", id: c.conditional_order_id, conditional: true,
                      legs: [c.first, ...(c.second ? [c.second] : [])].map((l) => ({ trigger_price: l.trigger_price, order_price: l.order_price })),
                      expire_date: c.expire_date }} onDraft={changed} />)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
        </div>
      </div>
    </div>
  );
}
