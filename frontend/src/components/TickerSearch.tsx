import { useEffect, useRef, useState } from "react";
import { api, type TickerHit } from "../api";
import { useT } from "../i18n";

interface Props {
  symbol: string;
  quick: string[];
  onChange: (symbol: string) => void;
}

/** Quick-switch buttons + search box over the cached Toss US stock list. */
export default function TickerSearch({ symbol, quick, onChange }: Props) {
  const { t } = useT();
  const [q, setQ] = useState("");
  const [hits, setHits] = useState<TickerHit[]>([]);
  const [msg, setMsg] = useState<string | null>(null);
  const [open, setOpen] = useState(false);
  const timer = useRef<number | undefined>(undefined);

  useEffect(() => {
    window.clearTimeout(timer.current);
    const text = q.trim();
    if (!text) {
      setHits([]);
      setMsg(null);
      return;
    }
    timer.current = window.setTimeout(async () => {
      try {
        const r = await api.tickers(text);
        setHits(r.items);
        setMsg(r.items.length ? null : r.meta.status === "ok" ? t("ticker.noMatch") : t("ticker.notLoaded"));
      } catch (e) {
        setHits([]);
        setMsg(e instanceof Error ? e.message : t("ticker.fail"));
      }
    }, 250);
    return () => window.clearTimeout(timer.current);
  }, [q, t]);

  const pick = (s: string) => {
    onChange(s.toUpperCase());
    setQ("");
    setOpen(false);
  };

  return (
    <div className="ticker-search">
      {quick.map((s) => (
        <button key={s} className={s === symbol ? "on" : ""} onClick={() => pick(s)}>
          {s}
        </button>
      ))}
      <div className="search-box">
        <input
          value={q}
          placeholder={t("ticker.placeholder")}
          aria-label="Search ticker"
          onChange={(e) => {
            setQ(e.target.value);
            setOpen(true);
          }}
          onFocus={() => setOpen(true)}
          onBlur={() => window.setTimeout(() => setOpen(false), 150)}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              if (hits.length) pick(hits[0].symbol);
              else if (/^[A-Za-z0-9.\-]{1,20}$/.test(q.trim())) pick(q.trim());
            }
            if (e.key === "Escape") setOpen(false);
          }}
        />
        {open && (hits.length > 0 || msg) && (
          <ul className="search-results">
            {hits.map((h) => (
              <li key={h.symbol} onMouseDown={() => pick(h.symbol)}>
                <b>{h.symbol}</b> <span className="muted">{h.name}</span>
              </li>
            ))}
            {msg && <li className="muted">{msg}</li>}
          </ul>
        )}
      </div>
    </div>
  );
}
