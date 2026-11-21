import { useEffect, useState } from "react";
import { settingsApi, type SettingsView, type UserSettings } from "../api";
import { trBackend, useT, type Key } from "../i18n";
import TrustedDevices from "./TrustedDevices";

const TOGGLES = ["ema", "vwap", "rsi", "fib", "swings", "lines", "aiLines", "sessions", "volume"] as const;

const list = (s: string) => s.split(/[,\s]+/).map((x) => x.trim().toUpperCase()).filter(Boolean);
const relatedText = (r: Record<string, string[]>) => Object.entries(r).map(([k, v]) => `${k}: ${v.join(", ")}`).join("\n");
function parseRelated(text: string): Record<string, string[]> {
  const out: Record<string, string[]> = {};
  for (const line of text.split("\n")) {
    const [k, rest] = line.split(":");
    if (k?.trim() && rest !== undefined) out[k.trim().toUpperCase()] = list(rest);
  }
  return out;
}
const numOrNull = (s: string) => (s.trim() === "" ? null : s.trim());

interface Props {
  onSaved: () => void; // reload screen defaults (watchlist buttons, chart defaults)
}

/** Settings page. Saved to user_settings.json; config.yaml stays as the defaults. */
export default function SettingsPage({ onSaved }: Props) {
  const { t, setLang } = useT();
  const [view, setView] = useState<SettingsView | null>(null);
  const [s, setS] = useState<UserSettings | null>(null);
  const [watch, setWatch] = useState("");
  const [related, setRelated] = useState("");
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [busy, setBusy] = useState(false);

  const load = (v: SettingsView) => {
    setView(v);
    setS(v.settings);
    setWatch(v.settings.tickers.watchlist.join(", "));
    setRelated(relatedText(v.settings.tickers.related));
  };
  useEffect(() => {
    settingsApi.get().then(load).catch((e) => setMsg({ ok: false, text: String(e?.message ?? e) }));
  }, []);

  if (!s || !view) return <div className="settings-page"><div className="muted">{msg?.text ?? t("app.loading")}</div></div>;

  const set = <K extends keyof UserSettings>(sec: K, patch: Partial<UserSettings[K]>) =>
    setS({ ...s, [sec]: { ...s[sec], ...patch } });

  const save = async () => {
    setBusy(true);
    setMsg(null);
    try {
      const body = { ...s, tickers: { ...s.tickers, watchlist: list(watch), related: parseRelated(related) } };
      const v = await settingsApi.save(body);
      load(v);
      setLang(v.settings.general.language);
      setMsg({ ok: true, text: t("st2.saved") });
      onSaved();
    } catch (e) {
      setMsg({ ok: false, text: trBackend(e instanceof Error ? e.message : String(e)) });
    } finally {
      setBusy(false);
    }
  };

  const reset = async () => {
    if (!window.confirm(t("st2.resetConfirm"))) return;
    const v = await settingsApi.reset();
    load(v);
    setLang(v.settings.general.language);
    onSaved();
  };

  const o = s.orders;
  return (
    <div className="settings-page">
      <h2>{t("st2.title")}</h2>

      <section className="panel"><div className="panel-head"><h2>{t("st2.general")}</h2></div>
        <div className="set-grid">
          <label>{t("st2.language")}</label>
          <select value={s.general.language} onChange={(e) => set("general", { language: e.target.value as "en" | "ko" })}>
            <option value="en">English</option><option value="ko">한국어</option>
          </select>
        </div>
      </section>

      <TrustedDevices />

      <section className="panel"><div className="panel-head"><h2>{t("st2.chart")}</h2></div>
        <div className="set-grid">
          <label>{t("st2.tf")}</label>
          <select value={s.chart.timeframe} onChange={(e) => set("chart", { timeframe: e.target.value as "1m" | "5m" | "1d" })}>
            <option value="1m">1m</option><option value="5m">5m</option><option value="1d">{t("chart.daily")}</option>
          </select>
          <label>{t("st2.toggles")}</label>
          <div className="set-toggles">
            {TOGGLES.map((k) => (
              <label key={k}><input type="checkbox" checked={!!s.chart.toggles[k]}
                onChange={() => set("chart", { toggles: { ...s.chart.toggles, [k]: !s.chart.toggles[k] } })} />
                {t(`chart.toggle.${k}` as Key)}</label>
            ))}
          </div>
          <label>{t("st2.colors")}</label>
          <select value={s.chart.color_scheme ?? "kr"}
            onChange={(e) => set("chart", { color_scheme: e.target.value as "kr" | "us" })}>
            <option value="kr">{t("st2.colorsKr")}</option><option value="us">{t("st2.colorsUs")}</option>
          </select>
          <label>{t("st2.grid")}</label>
          <input type="checkbox" checked={!!s.chart.grid} onChange={() => set("chart", { grid: !s.chart.grid })} />
          <label>{t("st2.vwapStyle")}</label>
          <select value={s.chart.dotted_style ?? "sparse_dotted"}
            onChange={(e) => set("chart", { dotted_style: e.target.value as NonNullable<UserSettings["chart"]["dotted_style"]> })}>
            {(["sparse_dotted", "dotted", "large_dashed", "dashed", "solid"] as const).map((v) => (
              <option key={v} value={v}>{t(`st2.look.${v}` as Key)}</option>
            ))}
          </select>
          <label>{t("st2.dottedOpacity")}</label>
          <input type="number" min={10} max={100} value={s.chart.dotted_opacity ?? 45}
            onChange={(e) => set("chart", { dotted_opacity: Number(e.target.value) })} />
          <label>{t("st2.zoneFill")}</label>
          <input type="number" min={0} max={30} value={s.chart.zone_fill_pct ?? 8}
            onChange={(e) => set("chart", { zone_fill_pct: Number(e.target.value) })} />
          <label>{t("st2.solidWidth")}</label>
          <input type="number" min={1} max={3} value={s.chart.solid_width ?? 1}
            onChange={(e) => set("chart", { solid_width: Number(e.target.value) })} />
          <label>{t("st2.vwapWidth")}</label>
          <input type="number" min={1} max={4} value={s.chart.vwap_width ?? 2}
            onChange={(e) => set("chart", { vwap_width: Number(e.target.value) })} />
          <label>{t("st2.height")}</label>
          <input type="number" min={20} max={80} value={s.chart.height_pct}
            onChange={(e) => set("chart", { height_pct: Number(e.target.value) })} />
        </div>
      </section>

      <section className="panel"><div className="panel-head"><h2>{t("st2.tickers")}</h2></div>
        <div className="set-grid">
          <label>{t("st2.default")}</label>
          <input value={s.tickers.default} onChange={(e) => set("tickers", { default: e.target.value.toUpperCase() })} />
          <label>{t("st2.watchlist")}</label>
          <div><input style={{ width: "100%" }} value={watch} onChange={(e) => setWatch(e.target.value)} />
            <div className="muted small">{t("st2.watchHint")}</div></div>
          <label>{t("st2.related")}</label>
          <div><textarea value={related} onChange={(e) => setRelated(e.target.value)} />
            <div className="muted small">{t("st2.relatedHint")}</div></div>
        </div>
      </section>

      <section className="panel"><div className="panel-head"><h2>{t("st2.orderbook")}</h2></div>
        <div className="set-grid">
          <label>{t("st2.spread")}</label>
          <input value={String(s.orderbook.spread_warn_pct)} onChange={(e) => set("orderbook", { spread_warn_pct: e.target.value })} />
        </div>
      </section>

      <section className="panel"><div className="panel-head"><h2>{t("st2.orders")}</h2></div>
        <div className={`dry-banner small ${view.read_only.dry_run ? "" : "live"}`}>
          {t("st2.dryRun", { state: view.read_only.dry_run ? t("st2.dryOn") : t("st2.dryOff") })}
        </div>
        <div className="set-grid">
          <label>{t("st2.band")}</label>
          <input value={String(o.price_band_pct)} onChange={(e) => set("orders", { price_band_pct: e.target.value })} />
          <label>{t("st2.ttl")}</label>
          <input type="number" min={30} max={3600} value={o.draft_ttl_seconds}
            onChange={(e) => set("orders", { draft_ttl_seconds: Number(e.target.value) })} />
          <label>{t("st2.maxOrder")}</label>
          <input value={o.max_order_usd ?? ""} onChange={(e) => set("orders", { max_order_usd: numOrNull(e.target.value) })} />
          <label>{t("st2.maxDay")}</label>
          <input value={o.max_day_usd ?? ""} onChange={(e) => set("orders", { max_day_usd: numOrNull(e.target.value) })} />
          <label>{t("st2.maxCount")}</label>
          <input value={o.max_orders_per_day ?? ""}
            onChange={(e) => set("orders", { max_orders_per_day: e.target.value.trim() === "" ? null : Number(e.target.value) })} />
          <label>{t("st2.marketOutside")}</label>
          <input type="checkbox" checked={o.allow_market_outside_regular}
            onChange={() => set("orders", { allow_market_outside_regular: !o.allow_market_outside_regular })} />
          <label title={t("st2.ruleCapNote")}>{t("st2.ruleCap")}</label>
          <input value={o.rule_max_buy_pct ?? ""} title={t("st2.ruleCapNote")}
            onChange={(e) => set("orders", { rule_max_buy_pct: numOrNull(e.target.value) })} />
        </div>
      </section>

      <section className="panel"><div className="panel-head"><h2>{t("st2.ai")}</h2></div>
        <div className="set-grid">
          <label>{t("st2.tier")}</label>
          <select value={s.ai.default_tier} onChange={(e) => set("ai", { default_tier: e.target.value as UserSettings["ai"]["default_tier"] })}>
            {view.read_only.tiers.map((tr) => <option key={tr.name} value={tr.name}>{tr.label}</option>)}
          </select>
          <label title={t("st2.riskHint")}>{t("st2.risk")}</label>
          <select value={s.ai.risk_profile} title={t("st2.riskHint")}
            onChange={(e) => set("ai", { risk_profile: e.target.value as UserSettings["ai"]["risk_profile"] })}>
            <option value="standard">{t("st2.risk.standard")}</option>
            <option value="aggressive">{t("st2.risk.aggressive")}</option>
          </select>
          <label>{t("st2.web")}</label>
          <input type="checkbox" checked={s.ai.web_search_default} onChange={() => set("ai", { web_search_default: !s.ai.web_search_default })} />
          <label>{t("st2.daily")}</label>
          <input value={String(s.ai.daily_usd)} onChange={(e) => set("ai", { daily_usd: e.target.value })} />
          <label>{t("st2.monthly")}</label>
          <input value={String(s.ai.monthly_usd)} onChange={(e) => set("ai", { monthly_usd: e.target.value })} />
          <label>{t("st2.candles")}</label>
          <input type="checkbox" checked={s.ai.raw_candles} onChange={() => set("ai", { raw_candles: !s.ai.raw_candles })} />
          <label title={t("st2.memoryNote")}>{t("st2.memory")}</label>
          <input type="number" min={1} max={20} value={s.ai.memory_turns} title={t("st2.memoryNote")}
            onChange={(e) => set("ai", { memory_turns: Number(e.target.value) })} />
          <label title={t("st2.quickNote")}>{t("st2.quick")}</label>
          <div>
            <textarea className="quick-prompt" rows={8} maxLength={4000} value={s.ai.quick_prompt}
              placeholder={t("chat.quickPrompt")} title={t("st2.quickNote")}
              onChange={(e) => set("ai", { quick_prompt: e.target.value })} />
            <div className="muted small">
              {t("st2.quickNote")}{" "}
              {s.ai.quick_prompt && <button className="small" onClick={() => set("ai", { quick_prompt: "" })}>{t("st2.quickReset")}</button>}
            </div>
          </div>
        </div>
      </section>

      <section className="panel"><div className="panel-head"><h2>{t("ev.my")}</h2></div>
        <div className="muted small">{t("ev.myNote")}</div>
        <table className="otable my-events">
          <thead><tr><th>{t("ev.name")}</th><th>{t("ev.when")}</th><th>{t("ev.note")}</th><th></th></tr></thead>
          <tbody>
            {s.events.items.map((e, i) => {
              const put = (patch: Partial<typeof e>) =>
                set("events", { items: s.events.items.map((x, j) => (j === i ? { ...x, ...patch } : x)) });
              return (
                <tr key={i}>
                  <td><input value={e.name} maxLength={80} onChange={(ev) => put({ name: ev.target.value })} /></td>
                  <td><input value={e.time} placeholder="2028-11-07 / 2026-12-10T22:30:00+09:00"
                    onChange={(ev) => put({ time: ev.target.value })} /></td>
                  <td><input value={e.note ?? ""} maxLength={200} onChange={(ev) => put({ note: ev.target.value || null })} /></td>
                  <td><button className="small" onClick={() => set("events", { items: s.events.items.filter((_, j) => j !== i) })}>
                    {t("common.delete")}</button></td>
                </tr>
              );
            })}
          </tbody>
        </table>
        <button className="small" onClick={() => set("events", { items: [...s.events.items, { name: "", time: "", note: null }] })}>
          {t("ev.add")}</button>
      </section>

      <section className="panel"><div className="panel-head"><h2>{t("st2.news")}</h2></div>
        <div className="set-grid">
          <label>{t("st2.age")}</label>
          <input type="number" min={1} max={168} value={s.news.max_age_hours}
            onChange={(e) => set("news", { max_age_hours: Number(e.target.value) })} />
          <label>{t("st2.tag")}</label>
          <input type="checkbox" checked={s.news.tag_enabled} onChange={() => set("news", { tag_enabled: !s.news.tag_enabled })} />
        </div>
      </section>

      <div className="muted small">{t("st2.notHere")}</div>
      <div className="set-actions">
        <button onClick={() => void save()} disabled={busy}>{t("st2.save")}</button>
        <button className="small" onClick={() => void reset()} disabled={busy}>{t("st2.reset")}</button>
        {msg && <span className={msg.ok ? "small up" : "small st-error"}>{msg.text}</span>}
      </div>
    </div>
  );
}
