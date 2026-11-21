import type { UsageSummary } from "../api";
import { fmtNum } from "../format";
import { trBackend, useT } from "../i18n";
import { modelName } from "./CostLine";

function usd(v: string | number): string {
  return `USD ${Number(v).toFixed(2)}`;
}

/** Today's and this month's AI spend, share by model, and the budget state. */
export default function CreditHeader({ summary, error }: { summary: UsageSummary | null; error: string | null }) {
  const { t } = useT();
  if (!summary) {
    return <div className="credits muted small">{error ? t("credits.error", { msg: trBackend(error) }) : t("credits.loading")}</div>;
  }
  const b = summary.budget;
  const pct = (r: string) => Math.min(100, Math.round(Number(r) * 100));
  const share = Object.entries(summary.month.by_model_share)
    .sort((a, z) => Number(z[1]) - Number(a[1]))
    .map(([m, s]) => `${modelName(m)} ${Math.round(Number(s) * 100)}%`)
    .join(" · ");
  const krw = (p: UsageSummary["today"]) =>
    p.krw === null ? "" : ` · KRW ${fmtNum(p.krw, 0)}${p.krw_estimated ? t("credits.est") : ""}`;
  return (
    <div className={`credits budget-${b.state}`}>
      <span>
        {t("credits.today")} <b>{usd(summary.today.usd)}</b>{krw(summary.today)} / {t("credits.limit")} {usd(b.daily_limit_usd)} ({pct(b.day_ratio)}%)
      </span>
      <span>
        {t("credits.month")} <b>{usd(summary.month.usd)}</b>{krw(summary.month)} / {t("credits.limit")} {usd(b.monthly_limit_usd)} ({pct(b.month_ratio)}%)
      </span>
      {share && <span className="muted">{share}</span>}
      {b.state === "warn" && <span className="st-stale">{t("credits.warn", { pct: Math.round(Number(b.warn_at) * 100) })}</span>}
      {b.state === "over" && <span className="st-error">{t("credits.over")}</span>}
    </div>
  );
}
