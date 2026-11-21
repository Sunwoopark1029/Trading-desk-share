import type { TurnCost } from "../api";
import { fmtKstTime, fmtNum } from "../format";
import { useT } from "../i18n";

export function modelName(id: string | null | undefined): string {
  if (!id) return "?";
  const m = /^claude-([a-z]+)-(\d+)(?:-(\d+))?$/.exec(id);
  if (!m) return id;
  const name = m[1][0].toUpperCase() + m[1].slice(1);
  return `${name} ${m[2]}${m[3] ? "." + m[3] : ""}`;
}

/** Per message: model, tokens, cache hit rate, cost in USD and KRW (with the FX rate used). */
export default function CostLine({ cost }: { cost: TurnCost }) {
  const { t } = useT();
  const hit = cost.cache_hit_rate === null ? "—" : `${Math.round(cost.cache_hit_rate * 100)}%`;
  const usd = Number(cost.usd);
  return (
    <div className="cost-line" title={t("cost.calls", { n: cost.calls })}>
      {t("cost.line", {
        models: cost.models.map(modelName).join(" + "), inp: fmtNum(cost.input_tokens, 0),
        out: fmtNum(cost.output_tokens, 0), cw: fmtNum(cost.cache_write_tokens, 0),
        cr: fmtNum(cost.cache_read_tokens, 0), hit,
      })}
      {cost.web_search_requests > 0 && t("cost.web", { n: cost.web_search_requests })} ·{" "}
      <b>USD {usd < 0.01 ? usd.toFixed(4) : usd.toFixed(3)}</b>
      {cost.krw !== null ? ` · KRW ${fmtNum(cost.krw, 0)}` : " · KRW —"}
      {cost.fx_rate && <span className="muted">{t("cost.fx", { rate: fmtNum(cost.fx_rate, 2), t: fmtKstTime(cost.fx_time) })}</span>}
      {!cost.price_known && <span className="st-stale">{t("cost.unknownPrice")}</span>}
    </div>
  );
}
