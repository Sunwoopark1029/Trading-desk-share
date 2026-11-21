import assert from "node:assert/strict";
import test from "node:test";
import { split } from "./tickerText.ts";

const known = new Set(["NVDA", "SOXS", "SOXX", "BRK.B", "USD", "AI", "ON", "A"]);
const links = (text: string) => split(text, known).filter((n) => n.type === "link").map((n) => n.url);

test("only listed tickers become links", () => {
  assert.deepEqual(links("NVDA와 SOXS 비교, SOXX는 얇음"), ["ticker:NVDA", "ticker:SOXS", "ticker:SOXX"]);
  assert.deepEqual(links("BRK.B rose"), ["ticker:BRK.B"]);
});

test("acronyms, single letters, level ids and lower case stay text", () => {
  assert.deepEqual(links("USD 4.25 at AI news, A plan, vwap_5m, ema20_5m, Nvda"), []);
  assert.deepEqual(links("XYZQ is not listed"), []);
  assert.deepEqual(links("$NVDA or NVDA2"), []);
});

test("text around a link is kept", () => {
  const out = split("Buy NVDA now", known);
  assert.deepEqual(out.map((n) => n.value ?? n.url), ["Buy ", "ticker:NVDA", " now"]);
  assert.equal(split("no tickers here", known).length, 1);
});
