// Chat threads per ticker (phase 6 part C, D5): each ticker has its own chats on each page (desk / research).
// No imports, so Node's test runner can load it.

export type ChatKind = "desk" | "research";

/** Browser key remembering the open chat of one ticker on one page. */
export function threadKey(kind: ChatKind, symbol: string): string {
  return `desk.chat.cur.${kind}.${symbol.toUpperCase()}`;
}

/** The chat to open for a ticker: the remembered one if it still exists, else that ticker's newest, else none. */
export function chooseThread(list: { id: string }[], saved: string | null): string | null {
  if (saved && list.some((c) => c.id === saved)) return saved;
  return list.length ? list[0].id : null;
}
