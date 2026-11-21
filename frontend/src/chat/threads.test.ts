// Run: npm test
import assert from "node:assert/strict";
import { test } from "node:test";
import { chooseThread, threadKey } from "./threads.ts";

test("one key per page and ticker", () => {
  assert.equal(threadKey("desk", "soxs"), "desk.chat.cur.desk.SOXS");
  assert.notEqual(threadKey("desk", "NVDA"), threadKey("research", "NVDA"));
});

test("remembered chat wins while it exists, else the newest, else none", () => {
  const list = [{ id: "new" }, { id: "old" }];   // the server sends newest first
  assert.equal(chooseThread(list, "old"), "old");
  assert.equal(chooseThread(list, "gone"), "new");
  assert.equal(chooseThread(list, null), "new");
  assert.equal(chooseThread([], "old"), null);
});
