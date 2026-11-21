/** Finds listed US tickers in chat text (pure; unit tested). Only symbols on the Toss list count, minus common
 *  acronyms that are also tickers (USD, EMA, CPI, ...). */

// Uppercase words that are also listed tickers but in answers almost always mean something else
export const NOT_TICKERS = new Set([
  "USD", "KRW", "KST", "ET", "EST", "EDT", "UTC", "EMA", "SMA", "VWAP", "RSI", "MACD", "ATR", "IV", "RV", "CPI", "PPI",
  "GDP", "PCE", "FOMC", "FED", "JOLTS", "AI", "OK", "ATM", "OTM", "ITM", "OCO", "OTO", "DAY", "CLS", "LIMIT", "MARKET",
  "BUY", "SELL", "TTM", "EPS", "ETF", "ETN", "AM", "PM", "US", "KR", "NYSE", "SEC", "CEO", "CFO", "IPO", "YOY", "QOQ",
  "YTD", "PER", "PBR", "ROE", "ROA", "FCF", "EV", "EBITDA", "DRY", "RUN", "API", "PIN", "BMO", "AMC", "OPEX", "VIX",
  "DXY", "NQ", "ES", "SOX", "PHLX", "ICE", "TP", "SL", "RR", "NO", "YES", "AND", "OR", "THE", "IS", "IT", "BE", "ALL",
  "NEW", "NOW", "LOW", "HIGH", "OPEN", "CLOSE", "STOP", "LONG", "SHORT", "BEAR", "BULL", "BIG", "TOP", "KEY", "PLAN",
  "RISK", "NET", "FX", "BLS", "BEA", "FRED", "TOSS", "LLM", "UI", "OHLC", "HOD", "LOD", "POC", "VAH", "VAL", "GAP",
  "AH", "PRE", "REG", "NBBO", "IEX", "EOD", "WTI", "MAX", "MIN", "AVG", "MA", "SEP", "DEC", "NOV", "OCT", "JAN",
]);

const TOKEN = /(?<![A-Za-z0-9.$_])([A-Z]{2,5}(?:\.[A-Z])?)(?![A-Za-z0-9_])/g;

/** remark plugin for the chat's Markdown: listed tickers in plain text become `ticker:` links (not inside links / code). */
export function remarkTickers(knownSet: Set<string> | null) {
  return () => (tree: MdNode) => {
    if (!knownSet || knownSet.size === 0) return;
    const walk = (node: MdNode, inLink: boolean) => {
      if (!node.children) return;
      const out: MdNode[] = [];
      for (const ch of node.children) {
        if (ch.type === "text" && !inLink && typeof ch.value === "string") out.push(...split(ch.value, knownSet));
        else {
          walk(ch, inLink || ch.type === "link" || ch.type === "linkReference");
          out.push(ch);
        }
      }
      node.children = out;
    };
    walk(tree, false);
  };
}

export interface MdNode { type: string; value?: string; url?: string; children?: MdNode[] }

export function split(text: string, knownSet: Set<string>): MdNode[] {
  const out: MdNode[] = [];
  let last = 0;
  for (const m of text.matchAll(TOKEN)) {
    const sym = m[1];
    if (!knownSet.has(sym) || NOT_TICKERS.has(sym)) continue;
    const at = m.index ?? 0;
    if (at > last) out.push({ type: "text", value: text.slice(last, at) });
    out.push({ type: "link", url: `ticker:${sym}`, children: [{ type: "text", value: sym }] });
    last = at + sym.length;
  }
  if (last === 0) return [{ type: "text", value: text }];
  if (last < text.length) out.push({ type: "text", value: text.slice(last) });
  return out;
}
