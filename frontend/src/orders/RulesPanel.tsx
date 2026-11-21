import { useState } from "react";
import { rulesApi, type Issue, type RuleCheck, type RuleView } from "../api";
import { fmtKstDateTime, fmtKstTime, fmtPrice } from "../format";
import { useT, type Key } from "../i18n";
import { useAuth } from "../Login";
import { issueText } from "./OrderTicket";
import { useOrderText } from "./orderText";
import { usePoll } from "../usePoll";
import { TickerLink } from "../TickerLink";

interface Props {
  symbol: string;
  pollMs: number;
}

type RType = "back" | "bounce" | "close";
type Confirm = "tick" | "close_1m" | "close_5m";

/** "YYYY-MM-DDTHH:MM" in KST, `hours` from now (for the datetime-local input; the backend reads it as KST). */
function kstLocal(hours: number): string {
  return new Date(Date.now() + hours * 3600_000 + 9 * 3600_000).toISOString().slice(0, 16);
}

function issuesOf(e: unknown): Issue[] {
  const b = (e as { body?: { errors?: Issue[]; issues?: Issue[]; error?: string } }).body;
  if (b?.errors?.length) return b.errors;
  if (b?.issues?.length) return b.issues;
  return [{ code: b?.error ?? (e instanceof Error ? e.message : String(e)), params: {} }];
}

/** Desk: automatic rules. Writing a rule never arms it; only the Arm screen does (one tap, PIN on the phone). */
export default function RulesPanel({ symbol, pollMs }: Props) {
  const { t, lang } = useT();
  const rules = usePoll(() => rulesApi.list(), pollMs);
  const d = rules.data;
  const [editing, setEditing] = useState(false);
  const [arming, setArming] = useState<RuleCheck | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [showDone, setShowDone] = useState(false);
  const tx = (i: Issue) => issueText(t, i);

  const act = async (fn: () => Promise<unknown>) => {
    setMsg(null);
    try {
      await fn();
    } catch (e) {
      setMsg(issuesOf(e).map(tx).join(" · "));
    }
    rules.refresh();
  };

  const openArm = (id: string) => act(async () => setArming(await rulesApi.check(id)));

  const killAll = () => {
    if (window.confirm(t("rl.killConfirm"))) void act(() => rulesApi.disarmAll());
  };

  const open = d?.rules.filter((r) => ["proposed", "armed", "touched", "firing"].includes(r.status)) ?? [];
  const done = d?.rules.filter((r) => !open.includes(r)).slice(0, 10) ?? [];

  return (
    <section className="panel rules-panel">
      <div className="panel-head">
        <h2>{t("rl.title")}</h2>
        <span className="small muted">{d && t("rl.armedCount", { n: d.armed, max: d.max_armed })}</span>
        <button className="small" onClick={() => setEditing(!editing)}>{t("rl.new")}</button>
        {d && d.armed > 0 && <button className="small danger" onClick={killAll}>{t("rl.killAll")}</button>}
      </div>
      {d && <div className={`dry-banner ${d.dry_run ? "" : "live"} small`}>{d.dry_run ? t("rl.dry") : t("rl.real")}</div>}
      {d && !d.enabled && <div className="st-error small">{t("rl.off")}</div>}
      {d?.error && <div className="st-error small">{d.error}</div>}
      {rules.error && !d && <div className="st-error small">{rules.error}</div>}
      <div className="muted small">{t("rl.running")}</div>
      {msg && <div className="st-error small">{msg}</div>}

      {editing && (
        <RuleEditor symbol={symbol} onSaved={() => { setEditing(false); setMsg(t("rl.saved")); rules.refresh(); }} />
      )}

      {d && d.rules.length === 0 && !editing && <div className="muted small">{t("rl.none")}</div>}
      {d && d.rules.length > 0 && (
        <div className="otable-wrap">
          <table className="otable rules-table">
            <thead>
              <tr>
                <th>{t("rl.col.status")}</th><th>{t("rl.f.symbol")}</th><th>{t("rl.col.when")}</th>
                <th>{t("rl.col.then")}</th><th>{t("rl.col.ends")}</th><th>{t("rl.col.progress")}</th><th></th>
              </tr>
            </thead>
            <tbody>
              {[...open, ...(showDone ? done : [])].map((r) => (
                <RuleRow key={r.id} r={r}
                  onArm={() => void openArm(r.id)}
                  onDisarm={() => void act(() => rulesApi.disarm(r.id))}
                  onDelete={() => void act(() => rulesApi.remove(r.id))} />
              ))}
            </tbody>
          </table>
        </div>
      )}
      {done.length > 0 && (
        <button className="small" onClick={() => setShowDone(!showDone)}>
          {showDone ? t("rl.hideDone") : t("rl.showDone", { n: done.length })}
        </button>
      )}

      {arming && (
        <ArmModal check={arming} lang={lang} onClose={() => setArming(null)}
          onArmed={() => { setArming(null); rules.refresh(); }} />
      )}
    </section>
  );
}

const OPEN_STATUSES = ["proposed", "armed", "touched", "firing"];

/** "When" cell: the condition as numbered steps (touch, then trigger) or one candle-close line. */
function When({ r }: { r: RuleView }) {
  const { t } = useT();
  const cur = r.order.currency || "USD";
  const conf = t(`rl.c.${r.trigger.confirm ?? "tick"}` as Key);
  const trig = fmtPrice(r.trigger.price, cur);
  if (!r.touch) {
    return <div className="when-step">{t(r.trigger.dir === "up" ? "rl.when.closeUp" : "rl.when.closeDown", { c: conf, p: trig })}</div>;
  }
  return (
    <>
      <div className="when-step">① {t(r.touch.dir === "up" ? "rl.when.touchUp" : "rl.when.touchDown", { p: fmtPrice(r.touch.price, cur) })}</div>
      <div className="when-step">② {t(r.trigger.dir === "down" ? "rl.when.thenDown" : "rl.when.thenUp", { p: trig })}</div>
      <div className="muted small">
        {t("rl.when.check", { c: conf })}{r.window_min !== null && ` · ${t("rl.when.within", { n: r.window_min })}`}
      </div>
    </>
  );
}

function RuleRow({ r, onArm, onDisarm, onDelete }: {
  r: RuleView; onArm: () => void; onDisarm: () => void; onDelete: () => void;
}) {
  const { t } = useT();
  const { side, tif } = useOrderText();
  const live = r.status === "armed" || r.status === "touched";
  const bad = ["blocked", "rejected", "unknown"].includes(r.status);
  const cur = r.order.currency || "USD";
  const why = [
    r.source === "ai" && r.ai_fields.length > 0 ? t("rl.aiFields", { f: r.ai_fields.join(", ") }) : null,
    r.basis ? `${t("rl.basis")}: ${r.basis}` : null,
    r.note,
  ].filter(Boolean);
  const errs = r.result?.issues?.length
    ? r.result.issues.map((i) => issueText(t, i))
    : r.result?.error && r.status !== "sent" ? [r.result.error] : [];
  const progress = [
    r.status === "touched" && r.touched_at
      ? <div key="touch">{t("rl.touchedAt", { p: fmtPrice(r.touch_price, cur), t: fmtKstTime(r.touched_at), u: fmtKstTime(r.touch_until) })}</div>
      : null,
    r.paused && live ? <div key="pause" className="st-stale">{t("rl.paused")}</div> : null,
    r.fired_at ? <div key="fire">{t("rl.firedAt", { p: fmtPrice(r.fire_price, cur), t: fmtKstDateTime(r.fired_at) })}</div> : null,
    ...errs.map((e, n) => <div key={`e${n}`} className="st-error">✗ {e}</div>),
  ].filter(Boolean);
  return (
    <>
      <tr className={`st-${r.status}${why.length ? " has-why" : ""}${OPEN_STATUSES.includes(r.status) ? "" : " done"}`}>
        <td>
          <div><span className={`rule-chip ${live ? "on" : bad ? "bad" : ""}`}>{t(`rl.st.${r.status}` as Key)}</span></div>
          {r.source === "ai" && <div><span className="rule-chip ai">{t("rl.ai")}</span></div>}
          {live && r.armed_by === "ai" && <div><span className="rule-chip ai">{t("rl.armedByAi")}</span></div>}
        </td>
        <th><TickerLink sym={r.symbol} /></th>
        <td><When r={r} /></td>
        <td>
          <div className="when-step">
            <b className={r.order.side === "BUY" ? "up" : "down"}>{side(r.order.side)}</b>{" "}
            {r.order.quantity !== null ? t("rl.shares", { n: r.order.quantity }) : "—"}
          </div>
          <div className="when-step">{r.order.order_type === "MARKET" ? t("ord.market") : `@ ${fmtPrice(r.order.price, cur)}`}</div>
          <div className="muted small">{tif(r.order.time_in_force)}</div>
        </td>
        <td>
          <div className="when-step">{fmtKstDateTime(r.expires)}</div>
          {r.sessions === "regular" && <div className="muted small">{t("rl.f.regular")}</div>}
        </td>
        <td className="small">{progress.length ? progress : <span className="muted">—</span>}</td>
        <td>
          <div className="rule-btns">
            {r.status === "proposed" && <button className="small primary" onClick={onArm}>{t("rl.arm")}</button>}
            {live && <button className="small" onClick={onDisarm}>{t("rl.disarm")}</button>}
            {r.status !== "firing" && <button className="small" onClick={onDelete}>{t("rl.delete")}</button>}
          </div>
        </td>
      </tr>
      {why.length > 0 && (
        <tr className={`rule-why${OPEN_STATUSES.includes(r.status) ? "" : " done"}`}>
          <td></td>
          <td colSpan={6} className="muted small">{why.join(" · ")}</td>
        </tr>
      )}
    </>
  );
}

function ArmModal({ check, lang, onClose, onArmed }: { check: RuleCheck; lang: string; onClose: () => void; onArmed: () => void }) {
  const { t } = useT();
  const [pin, setPin] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const r = check.rule;
  const auth = useAuth().status;   // trusted browser: home-PC rules, no PIN (cloud plan)
  const remote = auth ? !!auth.remote && !auth.trusted : !["localhost", "127.0.0.1"].includes(window.location.hostname);
  const arm = async () => {
    setBusy(true);
    setErr(null);
    try {
      await rulesApi.arm(r.id, remote ? pin : undefined);
      onArmed();
    } catch (e) {
      setErr(issuesOf(e).map((i) => issueText(t, i)).join(" · "));
    }
    setBusy(false);
  };
  return (
    <div className="modal-back" onClick={(ev) => ev.target === ev.currentTarget && !busy && onClose()}>
      <div className="modal">
        <h2>{t("rl.armTitle")}</h2>
        <div className={`dry-banner ${check.dry_run ? "" : "live"}`}>{check.dry_run ? t("rl.dry") : t("rl.real")}</div>
        <p className="big-summary">{lang === "ko" ? r.summary_ko : r.summary_en}</p>
        {r.source === "ai" && (
          <div className="st-stale small">
            {t("rl.ai")}{r.ai_fields.length > 0 && ` · ${t("rl.aiFields", { f: r.ai_fields.join(", ") })}`}
            {r.basis && ` · ${t("rl.basis")}: ${r.basis}`}
          </div>
        )}
        <div className="small">{t("rl.armNote")}</div>
        <div className="muted small">{t("rl.running")}</div>
        {check.errors.length > 0 && (
          <div className="st-error small">{t("rl.armBlocked")} {check.errors.map((i) => issueText(t, i)).join(" · ")}</div>
        )}
        {check.warnings.map((i) => <div key={i.code} className="st-stale small">⚠ {issueText(t, i)}</div>)}
        {remote && (
          <label className="small">{t("rl.pin")} <input type="password" value={pin} onChange={(e) => setPin(e.target.value)} /></label>
        )}
        {err && <div className="st-error small">{err}</div>}
        <div className="modal-btns">
          <button onClick={onClose} disabled={busy}>{t("rl.cancel")}</button>
          <button className="primary" onClick={() => void arm()} disabled={busy || check.errors.length > 0}>{t("rl.armDo")}</button>
        </div>
      </div>
    </div>
  );
}

function RuleEditor({ symbol, onSaved }: { symbol: string; onSaved: () => void }) {
  const { t } = useT();
  const [sym, setSym] = useState(symbol);
  const [type, setType] = useState<RType>("back");
  const [touch, setTouch] = useState("");
  const [trigger, setTrigger] = useState("");
  const [closeUp, setCloseUp] = useState(true);
  const [windowMin, setWindowMin] = useState("15");
  const [confirm, setConfirm] = useState<Confirm>("tick");
  const [side, setSide] = useState<"BUY" | "SELL">("SELL");
  const [otype, setOtype] = useState<"LIMIT" | "MARKET">("LIMIT");
  const [price, setPrice] = useState("");
  const [qty, setQty] = useState("");
  const [tif, setTif] = useState<"DAY" | "CLS">("DAY");
  const [expires, setExpires] = useState(kstLocal(8));
  const [regular, setRegular] = useState(false);
  const [errs, setErrs] = useState<Issue[]>([]);
  const [busy, setBusy] = useState(false);

  const pickType = (v: RType) => {
    setType(v);
    setSide(v === "bounce" ? "BUY" : v === "back" ? "SELL" : side);
    if (v === "close" && confirm === "tick") setConfirm("close_5m");
  };

  const save = async () => {
    setBusy(true);
    setErrs([]);
    const body: Record<string, unknown> = {
      symbol: sym.trim().toUpperCase(),
      touch: type === "close" ? null : { dir: type === "back" ? "up" : "down", price: touch.trim() },
      trigger: { dir: type === "close" ? (closeUp ? "up" : "down") : type === "back" ? "down" : "up", price: trigger.trim(), confirm },
      window_min: type === "close" ? null : windowMin.trim(),
      order: { side, order_type: otype, ...(otype === "LIMIT" ? { price: price.trim() } : {}), quantity: qty.trim(),
               time_in_force: tif, currency: "USD" },
      sessions: regular ? "regular" : "any",
      expires,
    };
    try {
      await rulesApi.create(body);
      onSaved();
    } catch (e) {
      setErrs(issuesOf(e));
    }
    setBusy(false);
  };

  return (
    <div className="rule-editor">
      <div className="set-grid">
        <label>{t("rl.f.symbol")}</label>
        <input value={sym} onChange={(e) => setSym(e.target.value)} />
        <label>{t("rl.f.type")}</label>
        <select value={type} onChange={(e) => pickType(e.target.value as RType)}>
          <option value="back">{t("rl.type.back")}</option>
          <option value="bounce">{t("rl.type.bounce")}</option>
          <option value="close">{t("rl.type.close")}</option>
        </select>
        {type !== "close" && <>
          <label>{t("rl.f.touch")}</label>
          <input value={touch} inputMode="decimal" onChange={(e) => setTouch(e.target.value)} />
        </>}
        {type === "close" && <>
          <label>{t("rl.f.closeDir")}</label>
          <select value={closeUp ? "up" : "down"} onChange={(e) => setCloseUp(e.target.value === "up")}>
            <option value="up">{t("rl.f.above")}</option>
            <option value="down">{t("rl.f.below")}</option>
          </select>
        </>}
        <label>{type === "close" ? t("rl.f.level") : t("rl.f.trigger")}</label>
        <input value={trigger} inputMode="decimal" onChange={(e) => setTrigger(e.target.value)} />
        {type !== "close" && <>
          <label>{t("rl.f.window")}</label>
          <input value={windowMin} inputMode="numeric" onChange={(e) => setWindowMin(e.target.value)} />
        </>}
        <label title={t("rl.confirmHelp")}>{t("rl.f.confirm")}</label>
        <select value={confirm} title={t("rl.confirmHelp")} onChange={(e) => setConfirm(e.target.value as Confirm)}>
          {type !== "close" && <option value="tick">{t("rl.c.tick")}</option>}
          <option value="close_1m">{t("rl.c.close_1m")}</option>
          <option value="close_5m">{t("rl.c.close_5m")}</option>
        </select>
        <label>{t("rl.f.side")}</label>
        <select value={side} onChange={(e) => setSide(e.target.value as "BUY" | "SELL")}>
          <option value="BUY">{t("ord.side.BUY")}</option>
          <option value="SELL">{t("ord.side.SELL")}</option>
        </select>
        <label>{t("rl.f.otype")}</label>
        <select value={otype} onChange={(e) => setOtype(e.target.value as "LIMIT" | "MARKET")}>
          <option value="LIMIT">LIMIT</option>
          <option value="MARKET">{t("ord.market")}</option>
        </select>
        {otype === "LIMIT" && <>
          <label>{t("rl.f.price")}</label>
          <input value={price} inputMode="decimal" onChange={(e) => setPrice(e.target.value)} />
        </>}
        <label>{t("rl.f.qty")}</label>
        <input value={qty} inputMode="numeric" onChange={(e) => setQty(e.target.value)} />
        <label>{t("rl.f.tif")}</label>
        <select value={tif} onChange={(e) => setTif(e.target.value as "DAY" | "CLS")}>
          <option value="DAY">{t("ord.tif.DAY")}</option>
          {otype === "LIMIT" && <option value="CLS">{t("ord.tif.CLS")}</option>}
        </select>
        <label>{t("rl.f.expires")}</label>
        <input type="datetime-local" value={expires} onChange={(e) => setExpires(e.target.value)} />
        <label>{t("rl.f.regular")}</label>
        <input type="checkbox" checked={regular} onChange={() => setRegular(!regular)} />
      </div>
      <div className="muted small">{t("rl.confirmHelp")}</div>
      {errs.map((i, n) => <div key={n} className="st-error small">✗ {issueText(t, i)}</div>)}
      <button className="primary" disabled={busy} onClick={() => void save()}>{t("rl.f.save")}</button>
    </div>
  );
}
