import { useState } from "react";
import { marketApi, type NewsItemView } from "../api";
import { fmtKstDateTime } from "../format";
import { trBackend, useT, type Key } from "../i18n";
import { usePoll } from "../usePoll";

interface Props {
  symbol: string;
}

type Prio = "" | "high" | "medium" | "low";

/** News fetched by code (Finnhub), with AI tags: priority, effect on holdings, one-line reason. */
export default function NewsPanel({ symbol }: Props) {
  const { t, lang } = useT();
  const [scope, setScope] = useState<"all" | "ticker">("all");
  const [prio, setPrio] = useState<Prio>("");
  const news = usePoll(() => marketApi.news(scope === "ticker" ? [symbol] : [], prio), 60_000, [scope, prio, symbol]);
  const d = news.data;

  const badge = (it: NewsItemView) => {
    const p = it.tag?.priority;
    return <span className={`prio prio-${p ?? "none"}`}>{t((p ? `news.prio.${p}` : "news.prio.none") as Key)}</span>;
  };

  return (
    <section className="panel news">
      <div className="panel-head">
        <h2>{t("news.title")}</h2>
        <div className="seg small">
          <button className={scope === "all" ? "on" : ""} onClick={() => setScope("all")}>{t("news.all")}</button>
          <button className={scope === "ticker" ? "on" : ""} onClick={() => setScope("ticker")}>
            {t("news.thisTicker", { sym: symbol })}
          </button>
        </div>
      </div>
      <div className="seg small news-prio">
        {(["", "high", "medium", "low"] as Prio[]).map((p) => (
          <button key={p || "any"} className={prio === p ? "on" : ""} onClick={() => setPrio(p)}>
            {p ? t(`news.prio.${p}` as Key) : t("news.all")}
          </button>
        ))}
      </div>
      {news.error && <div className="st-error small">{trBackend(news.error)}</div>}
      {d?.status.key === "empty" && <div className="st-stale small">{t("news.noKey")}</div>}
      {d && d.items.length === 0 && d.status.key === "set" && (
        <div className="muted small">{t("news.none", { h: d.status.max_age_hours })}</div>
      )}
      <ul className="news-list">
        {(d?.items ?? []).map((it) => (
          <li key={it.id} className={`news-item prio-row-${it.tag?.priority ?? "none"}`}>
            <div className="news-top">
              {badge(it)}
              {it.tag && it.tag.direction !== "no_position" && (
                <span className={`dir dir-${it.tag.direction}`}>{t(`news.dir.${it.tag.direction}` as Key)}</span>
              )}
              <a href={it.url} target="_blank" rel="noopener noreferrer">{it.headline}</a>
            </div>
            <div className="muted small">
              {it.source} · {it.date_verified ? fmtKstDateTime(it.published_kst) : <span className="st-stale">{t("news.unverified")}</span>}
              {" · "}{it.tickers.join(", ")}
            </div>
            {it.tag && <div className="small news-reason">{lang === "ko" ? it.tag.reason_ko : it.tag.reason_en}</div>}
          </li>
        ))}
      </ul>
      {d && (
        <div className="muted small">
          Finnhub · {t("news.tagging", { s: trBackend(d.status.tagging) })}
          {Object.entries(d.status.symbols).filter(([, s]) => s.error).slice(0, 3)
            .map(([sym, s]) => ` · ${t("news.err", { sym, err: trBackend(s.error) })}`)}
          <div title={d.status.watching.join(", ")}>{t("news.watching", { list: d.status.watching.slice(0, 8).join(", ") + (d.status.watching.length > 8 ? "…" : "") })}</div>
        </div>
      )}
    </section>
  );
}
