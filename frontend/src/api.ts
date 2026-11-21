// Backend API types + fetch helpers. All calls go to /api (Vite forwards them to
// 127.0.0.1:8000). No keys ever reach the browser. Decimal values arrive as strings.

export type Num = string; // exact decimal string from the backend

export type Status = "ok" | "stale" | "error" | "pending" | "missing";

export interface SourceMeta {
  status: Status;
  updated: string | null; // KST ISO
  source: string | null;
  delay: string | null;
  error_code: string | null;
  error: string | null;
}

export interface Health {
  status: "ok" | "degraded";
  keys: Record<string, "set" | "empty">;
  optional_keys?: string[];
  toss: { configured: boolean; revoked: boolean; message: string | null };
  sources: Record<string, SourceMeta>;
  fred_attribution: string;
  deploy_role?: "live" | "workshop"; // workshop = development copy: DRY RUN forced, rules off
}

export interface BarOut {
  start: number;
  end: number;
  open: Num;
  high: Num;
  low: Num;
  close: Num;
  volume: Num;
  session: string | null;
  date?: string;
}

export interface FibLevel {
  ratio: Num;
  price: Num;
}

export interface FibOut {
  type: "fib";
  source: "indicator";
  direction: "up" | "down";
  low: Num;
  high: Num;
  points: { time: string; epoch: number; start: number; price: Num }[];
  levels: FibLevel[];
}

export interface CandlesOut {
  symbol: string;
  tf: Timeframe;
  interval_seconds: number;
  bars: BarOut[];
  ema: Record<string, (Num | null)[]>;
  vwap: (Num | null)[] | null;
  rsi?: (Num | null)[]; // RSI(14), Wilder, 0..100; null while warming up
  vwap_label: string | null;
  fib: FibOut | null;
  fib_date: string | null;
  swings: { end: number; start: number; price: Num; kind: "high" | "low" }[];
  sessions: { name: string; start: number; end: number }[];
  live_bar?: (BarOut & { provisional: boolean; quote_time: string }) | null; // forming candle from the latest quote
  meta: SourceMeta & { currency: string | null; extended_hours_note?: string; time_label: string };
}

export type Timeframe = "1m" | "5m" | "1d";

export interface StripItem {
  key: string;
  label: string;
  value: Num | number | null;
  change: Num | number | null;
  change_pct: Num | number | null;
  unit: string | null;
  source: string;
  delay: string;
  updated: string | null;
  status: Status;
  error: string | null;
  flag: string | null;
  bar_date?: string | null;
  attribution?: string;
  prev_close_date?: string | null;
}

export interface StripOut {
  items: StripItem[];
  next_event: { name: string; name_ko?: string; time: string | null; date?: string; note: string | null;
    kind?: string; source?: string; importance?: string; count?: number } | null;
}

export interface OverlayPoint {
  time: string; // ISO 8601 with offset
  price: Num;
}

export interface Overlay {
  id: string;
  type: "hline" | "trendline" | "fib" | "zone";
  points: OverlayPoint[];
  label: string;
  color: string;
  source: "user" | "indicator" | "ai";
  visible: boolean;
  note: string | null;
  symbol: string | null;
  timeframe?: Timeframe | null;   // chart timeframe the line belongs to; null = every timeframe
  derived?: boolean;
  dashed?: boolean; // order drafts (phase 4)
  group?: "orders" | "research"; // orders: open orders / reserved triggers / DRY RUN ("Orders" toggle);
                                 // research: saved thesis reference prices ("Thesis lines" toggle)
  levels?: FibLevel[];
  direction?: "up" | "down";
}

export interface Plan {
  id?: string;
  symbol: string;
  side: "buy" | "sell";
  price: Num;
  currency: "USD" | "KRW";
  qty?: Num;
  note?: string;
}

export interface FxBlock {
  rate: Num | null;
  time: string | null;
  source: string;
  pair: string;
}

export interface PlanOut extends Plan {
  price_usd?: Num | null;
  price_krw?: Num | null;
  converted?: boolean;
  fx?: { rate: Num | null; time: string | null };
  raw?: unknown;
  error?: string;
}

export interface PositionsOut {
  trades: unknown[];
  plans: PlanOut[];
  avg_override: Record<string, { price: Num; currency: string }>;
  overlays: Overlay[];
  fx: FxBlock;
  notes: string[];
}

export interface Tagged {
  value: Num | null;
  status: "confirmed" | "typed" | "est." | "not set" | "missing";
}

export interface PnlRow {
  realized_usd: Tagged;
  realized_krw: Tagged;
  unrealized_usd: Tagged;
  unrealized_krw: Tagged;
  fees_usd: Tagged;
  fees_krw: Tagged;
  fees_confirmed_usd: Tagged;
  fees_confirmed_krw: Tagged;
  fees_est_usd: Tagged;
  fees_est_krw: Tagged;
  fx_cost_usd: Tagged;
  fx_cost_krw: Tagged;
  ai_credits_usd: Tagged;
  ai_credits_krw: Tagged;
  net_usd: Tagged;
  net_krw: Tagged;
}

export interface SymbolPnl extends PnlRow {
  symbol: string;
  qty: Num;
  avg_source: "computed" | "typed" | "toss";
  avg_usd: Tagged;
  avg_krw: Tagged;
  avg_usd_computed: Tagged;
}

export interface PnlOut {
  period: "today" | "all";
  source?: "toss" | "manual";
  names?: Record<string, string>;
  currencies?: Record<string, string>;
  symbols: SymbolPnl[];
  total: PnlRow;
  notes: string[];
  fx: FxBlock;
  quotes: SourceMeta;
}

export interface TickerHit {
  symbol: string;
  name: string;
  security_type: string;
}

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(path, init);
  } catch {
    throw new ApiError(0, "backend not reachable (is start.cmd running?)");
  }
  let body: unknown = null;
  try {
    body = await res.json();
  } catch {
    /* non-JSON body */
  }
  if (!res.ok) {
    if (res.status === 401 && body && typeof body === "object" && (body as { code?: string }).code === "login_required") {
      window.dispatchEvent(new Event("desk-login"));   // show the login screen (phase 5)
    }
    const detail = body && typeof body === "object" && "detail" in body ? String((body as { detail: unknown }).detail) : "";
    const err = new ApiError(res.status, detail || `HTTP ${res.status}`);
    (err as ApiError & { body?: unknown }).body = body;
    throw err;
  }
  return body as T;
}

export interface AuthStatus {
  required: boolean;
  logged_in: boolean;
  passcode_set: boolean;
  pin_set: boolean;
  remote: boolean;          // opened from another device through Tailscale
  trusted?: boolean;        // this browser is a trusted device: home-PC order rules (cloud plan)
  remote_orders: boolean;   // config remote.orders_enabled
  block_market: boolean;    // config remote.block_market_orders
  remote_hours?: number;    // phone login length
  local_days?: number;      // home PC login length
}

export const authApi = {
  status: () => request<AuthStatus>("/api/auth/status"),
  login: (passcode: string) => sendJson<{ ok: boolean }>("POST", "/api/auth/login", { passcode }),
  logout: () => sendJson<{ ok: boolean }>("POST", "/api/auth/logout", {}),
  trust: (pin: string, name: string) => sendJson<{ ok: boolean }>("POST", "/api/auth/trust", { pin, name }),
  devices: () => request<{ devices: TrustedDevice[] }>("/api/auth/devices"),
  removeDevice: (id: string) => sendJson<{ ok: boolean }>("DELETE", `/api/auth/devices/${encodeURIComponent(id)}`, {}),
  removeAllDevices: () => sendJson<{ ok: boolean; removed: number }>("POST", "/api/auth/devices/remove-all", {}),
};

export interface TrustedDevice {
  id: string;
  name: string;
  added: number;      // epoch seconds
  last_used: number;  // epoch seconds
  current: boolean;   // this browser
}

function sendJson<T>(method: string, path: string, data: unknown): Promise<T> {
  return request<T>(path, {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
}

export type LineLook = "solid" | "dotted" | "sparse_dotted" | "dashed" | "large_dashed";

export interface ChartDefaults {
  timeframe: Timeframe;
  toggles: Record<string, boolean>;
  height_pct: number;
  dotted_style?: LineLook;   // all dotted/dashed lines
  color_scheme?: "kr" | "us";
  grid?: boolean;
  dotted_opacity?: number;   // % strength of dotted/dashed lines
  zone_fill_pct?: number;    // % tint inside zones
  solid_width?: number;      // px for solid lines
  vwap_width?: number;
}

export interface UiConfig {
  tickers: { default: string; compare: string[] };
  user?: { general: { language: "en" | "ko" }; chart: ChartDefaults; ai: { default_tier: string } };
  polling: { quotes_seconds: number; candles_seconds: number; chart_seconds: number; fx_seconds: number; yfinance_seconds: number };
}

export const api = {
  uiConfig: () => request<UiConfig>("/api/ui-config"),
  health: () => request<Health>("/api/health"),
  tossRetry: () => request<{ ok: boolean; message: string }>("/api/health/toss/retry", { method: "POST" }),
  tickers: (q: string) => request<{ items: TickerHit[]; meta: SourceMeta }>(`/api/tickers?q=${encodeURIComponent(q)}`),
  candles: (symbol: string, tf: Timeframe) =>
    request<CandlesOut>(`/api/candles?symbol=${encodeURIComponent(symbol)}&tf=${tf}`),
  strip: () => request<StripOut>("/api/strip"),
  positions: () => request<PositionsOut>("/api/positions"),
  savePlans: (plans: Plan[]) => sendJson<{ plans: Plan[] }>("PUT", "/api/positions/plans", { plans }),
  pnl: (period: "today" | "all") => request<PnlOut>(`/api/pnl?period=${period}`),
  overlays: (symbol: string, timeframe?: Timeframe) =>
    request<{ overlays: Overlay[] }>(`/api/overlays?symbol=${encodeURIComponent(symbol)}${timeframe ? `&timeframe=${timeframe}` : ""}`),
  saveOverlay: (o: Omit<Overlay, "id"> & { id?: string }) => sendJson<Overlay>("POST", "/api/overlays", o),
  deleteOverlay: (id: string) => request<{ ok: boolean }>(`/api/overlays/${encodeURIComponent(id)}`, { method: "DELETE" }),
};

// ── chat (phase 2) ──────────────────────────────────────────────────────

export type Tier = "normal" | "deep" | "very_deep";

export interface BudgetInfo {
  state: "ok" | "warn" | "over";
  day_usd: Num;
  month_usd: Num;
  daily_limit_usd: Num;
  monthly_limit_usd: Num;
  day_ratio: Num;
  month_ratio: Num;
  warn_at: Num;
}

export interface ChatStatus {
  enabled: boolean;
  key?: "set" | "empty";
  message?: string | null;
  tiers?: { name: Tier; label: string; model: string }[];
  web_search_default?: boolean;
  quick_prompt?: string;
  default_tier?: Tier | null;
  candles_mode?: string;
  budget?: BudgetInfo;
}

export interface ScenarioRowOut {
  direction: "long" | "short";
  entry: Num;
  invalidation: Num;
  target: Num;
  entry_basis: string;
  invalidation_basis: string;
  target_basis: string;
  reasoning: string;
  risk_reward: Num | null;
  gross_move_pct: Num;
  costs_pct_est: Num;
  fx_cost_known: boolean;
  spread_pct_est?: Num | null;
  net_move_pct_est: Num;
  net_per_share_usd_est: Num;
  problems: string[];
  price_warnings?: string[];   // AI price that is not a code level: allowed, user checks it
  valid: boolean;
  plan?: "entry" | "entry_exits" | "exits";   // which order buttons to offer (set by the AI; default entry)
  // AI-suggested whole shares, only when the user asked for a quantity; money figures calculated in code
  quantity?: number | null;
  quantity_source?: "ai_suggested" | null;
  position_usd?: Num | null;
  loss_at_stop_usd_est?: Num | null;
  net_at_target_usd_est?: Num | null;
  quantity_warnings?: string[];
}

export interface ScenarioTable {
  symbol: string;
  currency: string;
  rows: ScenarioRowOut[];
  note: string;
}

export interface TurnCost {
  calls: number;
  models: string[];
  input_tokens: number;
  output_tokens: number;
  cache_write_tokens: number;
  cache_read_tokens: number;
  cache_hit_rate: number | null;
  web_search_requests: number;
  usd: Num;
  krw: Num | null;
  fx_rate: Num | null;
  fx_time: string | null;
  price_known: boolean;
}

export interface Valuation {
  symbol: string;
  verdicts: Record<"own_history" | "peers" | "growth", "cheap" | "fair" | "expensive" | "unclear">;
  summary: string;
  basis: { id: string; label: string; display: string; period: string; source: string }[];
  reasoning: string;
  risks: string[];
  change_view: string[];
  reference_levels: { id: string; label: string; price: Num; display: string; currency: string | null; source: string;
    as_of: string | null; note: string | null }[];
  price: { last: Num; currency: string; time: string | null; source: string; status?: string | null } | null;
  statements_currency: string | null;
  warnings: string[];
  data_as_of: { statements?: string | null; price?: string | null };
  note: string;
}

export interface Candidates { title: string; items: { symbol: string; name: string | null; reason: string }[]; note: string }

export interface ChatItem {
  id?: number;
  active?: boolean;
  kind: "question" | "answer";
  text: string;
  tier?: Tier;
  model?: string | null;
  symbol?: string;
  timeframe?: string;
  web_search?: boolean;
  scenarios?: ScenarioTable[];
  valuations?: Valuation[];
  candidates?: Candidates[];
  warnings?: string[];
  tools?: string[];
  cost?: TurnCost;
}

export interface UsagePeriod {
  usd: Num;
  krw: Num | null;
  krw_estimated: boolean;
  calls: number;
  by_model_usd: Record<string, Num>;
  by_model_share: Record<string, Num>;
}

export interface UsageSummary {
  today: UsagePeriod;
  month: UsagePeriod;
  all: UsagePeriod;
  budget: BudgetInfo;
  fx: { rate: Num | null; time: string | null; source: string };
}

export type ChatEvent =
  | { type: "start"; turn_id: string; tier: Tier; model: string }
  | { type: "text"; text: string }
  | { type: "thinking" }
  | { type: "tool"; name: string; status: "running" | "done" | "error"; message?: string }
  | { type: "overlay_changed"; action: string; id: string }
  | { type: "scenarios"; table: ScenarioTable }
  | { type: "valuation"; card: Valuation }
  | { type: "candidates"; list: Candidates }
  | { type: "thesis_saved"; symbol: string; id: number }
  | { type: "draft_created"; id: string }
  | { type: "usage"; record: Record<string, unknown> }
  | { type: "notice"; message: string }
  | { type: "done"; display: ChatItem }
  | { type: "error"; message: string }
  | { type: "end" };

export interface ConvSummary { id: string; title: string; updated: string; symbol: string | null; kind: "desk" | "research" }

export interface SendBody {
  text: string;
  tier: Tier;
  web_search: boolean;
  confirm_over_budget?: boolean;
  screen: Record<string, unknown>;
  lang?: "en" | "ko";
}

export const chatApi = {
  status: () => request<ChatStatus>("/api/chat/status"),
  usage: () => request<UsageSummary>("/api/usage/summary"),
  list: (symbol?: string, kind?: "desk" | "research") => {
    const q = new URLSearchParams();
    if (symbol) q.set("symbol", symbol);
    if (kind) q.set("kind", kind);
    const qs = q.toString();
    return request<{ conversations: ConvSummary[] }>(`/api/chat/conversations${qs ? `?${qs}` : ""}`);
  },
  create: (symbol?: string, kind?: "desk" | "research") =>
    sendJson<{ id: string }>("POST", "/api/chat/conversations", { ...(symbol ? { symbol } : {}), ...(kind ? { kind } : {}) }),
  get: (id: string) =>
    request<{ id: string; title: string; summarized: boolean; symbol: string | null; kind: "desk" | "research";
      messages: ChatItem[] }>(`/api/chat/conversations/${id}`),
  remove: (id: string) => request<{ ok: boolean }>(`/api/chat/conversations/${id}`, { method: "DELETE" }),
};

/** POST a message and read the Server-Sent Events stream. Non-200 -> ApiError (with JSON detail). */
export async function sendChat(id: string, body: SendBody, onEvent: (e: ChatEvent) => void,
                               signal?: AbortSignal): Promise<void> {
  let res: Response;
  try {
    res = await fetch(`/api/chat/conversations/${id}/messages`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body), signal,
    });
  } catch (e) {
    if (e instanceof DOMException && e.name === "AbortError") throw e;
    throw new ApiError(0, "backend not reachable (is start.cmd running?)");
  }
  if (!res.ok || !res.body) {
    let detail: unknown = null;
    try {
      detail = await res.json();
    } catch {
      /* ignore */
    }
    const err = new ApiError(res.status, (detail as { detail?: string })?.detail ?? `HTTP ${res.status}`);
    (err as ApiError & { body?: unknown }).body = detail;
    throw err;
  }
  const reader = res.body.getReader();
  const dec = new TextDecoder();
  let buf = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let i: number;
    while ((i = buf.indexOf("\n\n")) >= 0) {
      const chunk = buf.slice(0, i);
      buf = buf.slice(i + 2);
      for (const line of chunk.split("\n")) {
        if (line.startsWith("data: ")) {
          try {
            onEvent(JSON.parse(line.slice(6)) as ChatEvent);
          } catch {
            /* ignore a broken event */
          }
        }
      }
    }
  }
}

// ── orderbook, trades, news (phase 3) ───────────────────────────────────

export interface BookLevelOut {
  price: Num;
  volume: Num;
}

export interface OrderbookView {
  symbol: string;
  currency: string;
  asks: BookLevelOut[]; // cheapest first
  bids: BookLevelOut[]; // highest first
  levels_per_side: { asks: number; bids: number };
  book_time: string | null;
  book_source: "stream" | "rest" | null;
  book_updated: string | null;
  spread: { bid: Num; ask: Num; mid: Num; abs: Num; pct: Num | null; crossed: boolean; wide: boolean } | null;
  spread_warn_pct: Num;
  trades: { price: Num; volume: Num; time: string }[]; // newest first
  session: "day" | "pre" | "regular" | "after" | null;
  stream: { status: string; message: string | null; retry_in: number | null; rejected: unknown[] };
  error: string | null;
}

export interface NewsTag {
  priority: "high" | "medium" | "low";
  direction: "helps" | "hurts" | "unclear" | "no_position";
  reason_en: string;
  reason_ko: string;
}

export interface NewsItemView {
  id: string;
  headline: string;
  source: string;
  url: string;
  published_kst: string | null;
  date_verified: boolean;
  tickers: string[];
  summary: string;
  tag: NewsTag | null;
}

export interface NewsOut {
  items: NewsItemView[];
  status: {
    provider: string;
    key: "set" | "empty";
    watching: string[];
    symbols: Record<string, { last_ok: number | null; error: string | null; count: number }>;
    tagging: string;
    tagging_error: string | null;
    max_age_hours: number;
  };
}

export const marketApi = {
  orderbook: (symbol: string) => request<OrderbookView>(`/api/orderbook?symbol=${encodeURIComponent(symbol)}`),
  streamUrl: (symbol: string) => `/api/stream/market?symbol=${encodeURIComponent(symbol)}`,
  news: (symbols: string[], priority = "") =>
    request<NewsOut>(`/api/news?symbols=${encodeURIComponent(symbols.join(","))}&priority=${priority}`),
};

// ── account (phase 4, read-only) ────────────────────────────────────────

export interface OrderOut {
  order_id: string;
  symbol: string;
  side: string;
  order_type: string;
  time_in_force: string | null;
  status: string;
  price: Num | null;
  quantity: Num | null;
  order_amount: Num | null;
  currency: string;
  ordered_at: string | null;
  canceled_at: string | null;
  execution: { filled_quantity: Num | null; average_filled_price: Num | null; commission: Num | null } | null;
}

export interface CondLegOut {
  type: string;
  status: string;
  order_side: string;
  trigger_price: Num | null;
  target_profit_rate: Num | null;
  order_price: Num | null;
  triggered_order_id: string | null;
}

export interface CondOut {
  conditional_order_id: string;
  type: "SINGLE" | "OCO" | "OTO" | string;
  status: string;
  symbol: string;
  market: "KR" | "US" | string;
  quantity: Num;
  order_type: string;
  expire_date: string | null;
  first: CondLegOut;
  second: CondLegOut | null;
  created_at: string | null;
}

export interface HoldingOut {
  symbol: string;
  name: string;
  currency: string;
  quantity: Num;
  last_price: Num | null;
  average_purchase_price: Num | null;
  market_value: { purchaseAmount?: Num; amount?: Num };
  profit_loss: { amount?: Num; rate?: Num; amountAfterCost?: Num; rateAfterCost?: Num };
}

export interface AccountView {
  dry_run: boolean;
  account: { masked: string; type: string } | null;
  note: string | null;
  buying_power: { usd: Num | null; krw: Num | null };
  holdings: { items: HoldingOut[] } | null;
  open_orders: OrderOut[];
  open_conditional: CondOut[];
  updated: string | null;
  status: "ok" | "stale" | "pending" | "error";
  error: string | null;
}

export interface OptionsBand {
  move: number; up: number; down: number; move_pct: number; iv: number; expiry: string | null;
  quality: "good" | "fair" | "thin" | "none"; days?: number;
}
export interface OptionsExpiry {
  expiry: string; days: number; quality: "good" | "fair" | "thin" | "none"; iv: number | null; move: number | null;
  straddle: { strike: number; mid: number; spread_pct: number; yahoo_iv: number | null } | null;
  call_volume: number; put_volume: number;
}
/** GET /api/options: options-implied context, calculated in code from yfinance option chains (~15 min). */
export interface OptionsView {
  symbol: string; source: string; available: boolean; reason?: string;
  price: number; price_time_kst: string | null; fetched_kst: string; market_state: string | null;
  session_live: boolean; stale: boolean; fetch_error: string | null; currency: string;
  day: OptionsBand | null; week: OptionsBand | null; month: OptionsBand | null;
  realized_20d: number | null; iv_vs_realized: number | null; term: "inverted" | "normal" | "flat" | null;
  skew: { put_strike: number; call_strike: number; put_mid: number; call_mid: number; put_call_price_ratio: number; expiry: string } | null;
  pc_volume: number | null;
  most_traded: { expiry: string; side: "call" | "put"; strike: number; volume: number }[];
  expiries: OptionsExpiry[]; warnings: string[];
}

export interface EventItem {
  id: string; kind: "macro" | "fomc" | "earnings" | "market" | "opex" | "user"; title: string; title_ko: string;
  date: string; time_known: boolean; time_kst: string | null; source: string; importance: "high" | "medium" | "low";
  symbol: string | null; note: string | null; tags: string[];
}
export interface EventsOut {
  events: EventItem[]; days: number; as_of_kst: string; watched: Record<string, string>;
  sources: { name: string; ok: boolean; error: string | null; updated_kst: string | null; count: number }[];
}
export const eventsApi = {
  get: (days = 14) => request<EventsOut>(`/api/events?days=${days}`),
};

export const optionsApi = {
  get: (symbol: string) => request<OptionsView>(`/api/options?symbol=${encodeURIComponent(symbol)}`),
};

export const accountApi = {
  get: (refresh = false) => request<AccountView>(`/api/account${refresh ? "?refresh=true" : ""}`),
};

// ── drafts (phase 4 part 2): proposals only, nothing is sent ────────────

export interface Issue {
  code: string;
  params: Record<string, string | number>;
}

export interface DraftView {
  id: string;
  status: "draft" | "invalid" | "expired" | "discarded" | "sent";
  source: string;
  dry_run: boolean;
  created: string;
  expires: string;
  seconds_left: number;
  inputs: Record<string, unknown>;
  values: Record<string, unknown>;
  errors: Issue[];
  warnings: Issue[];
  estimate: {
    price_used: Num | null; price_is_estimate: boolean; notional_usd: Num | null; fee_usd_est: Num | null;
    total_usd_est: Num | null; krw_ref: Num | null; fx_mid: Num | null;
  };
  summary_en: string;
  summary_ko: string;
  context: { current_price: Num | null; price_source: string | null; session: string | null; price_band_pct: Num };
}

export interface SubmitResult {
  ok: boolean;
  status: "simulated" | "accepted" | "rejected" | "unknown" | "error";
  error?: string | null;
  issues?: Issue[];
  dry_run?: boolean;
  order_id?: string | null;
  conditional_order_id?: string | null;
  sent_at?: string;
}

export interface SimulatedOrder {
  id: string;
  draft_id: string;
  created: string;
  status: string;
  summary_en: string;
  summary_ko: string;
  values: Record<string, unknown>;
}

export interface OrderSettingsOut {
  price_band_pct: Num;
  draft_ttl_seconds: number;
  dry_run?: boolean;
}

/** A real Toss fill (chart marker). */
export interface FillMark {
  symbol: string;
  time: string; // ISO, KST
  side: "BUY" | "SELL";
  qty: Num;
  price: Num;
  currency: string;
}

export interface JournalTrip {
  symbol: string;
  currency: "USD" | "KRW";
  status: "open" | "closed";
  entry_time: string | null;
  entry_before_records: boolean;
  exit_time: string | null;
  exit_not_in_records: boolean;
  holding_seconds: number;
  buy_qty: Num;
  avg_entry: Num | null;
  sell_qty: Num;
  avg_exit: Num | null;
  held_qty: Num;
  fills: number;
  program_fills: number;
  realized_usd: Tagged; realized_krw: Tagged;
  unrealized_usd: Tagged; unrealized_krw: Tagged;
  fees_usd: Tagged; fees_krw: Tagged;
  fx_cost_usd: Tagged; fx_cost_krw: Tagged;
  ai_credits_usd: Tagged; ai_credits_krw: Tagged;
  net_usd: Tagged; net_krw: Tagged;
}

export interface JournalOut {
  ready: boolean;
  dry_run: boolean;
  trips: JournalTrip[];
  total: (Omit<JournalTrip, "symbol" | "currency" | "status" | "entry_time" | "entry_before_records" | "exit_time"
    | "exit_not_in_records" | "holding_seconds" | "buy_qty" | "avg_entry" | "sell_qty" | "avg_exit" | "held_qty"
    | "fills" | "program_fills"> & { trades: number; closed: number }) | null;
  notes: string[];
  names: Record<string, string>;
}

// ── automatic rules: armed by the user, sent by code when the condition fires ──

export interface RuleLevel { dir: "up" | "down"; price: Num; confirm?: "tick" | "close_1m" | "close_5m" }

export interface RuleView {
  id: string;
  status: "proposed" | "armed" | "touched" | "firing" | "sent" | "rejected" | "blocked" | "expired" | "unknown";
  source: "user" | "ai";
  symbol: string;
  touch: RuleLevel | null;
  trigger: RuleLevel;
  window_min: number | null;
  order: { side: string; order_type: string; price: Num | null; quantity: Num | null; time_in_force: string; currency: string };
  sessions: "any" | "regular";
  expires: string;
  created: string;
  armed_at: string | null;
  armed_by: "user" | "ai" | null;
  touched_at: string | null;
  touch_price: Num | null;
  touch_until: string | null;
  fired_at: string | null;
  fire_price: Num | null;
  paused: string | null;
  ai_fields: string[];
  basis: string | null;
  note: string | null;
  result: { ok?: boolean; status?: string; error?: string | null; order_id?: string | null; issues?: Issue[]; dry_run?: boolean } | null;
  summary_en: string;
  summary_ko: string;
}

export interface RulesOut {
  enabled: boolean; dry_run: boolean; error: string | null; armed: number; max_armed: number;
  max_buy_pct: Num | null; rules: RuleView[];
}

export interface RuleCheck { rule: RuleView; errors: Issue[]; warnings: Issue[]; dry_run: boolean }

export const rulesApi = {
  list: () => request<RulesOut>("/api/rules"),
  create: (body: Record<string, unknown>) =>
    sendJson<{ ok: boolean; rule: RuleView | null; errors: Issue[]; warnings: Issue[] }>("POST", "/api/rules", body),
  check: (id: string) => request<RuleCheck>(`/api/rules/${id}/check`),
  arm: (id: string, pin?: string) =>
    sendJson<{ ok: boolean; rule: RuleView }>("POST", `/api/rules/${id}/arm`, { approved: true, ...(pin ? { pin } : {}) }),
  disarm: (id: string) => sendJson<{ ok: boolean }>("POST", `/api/rules/${id}/disarm`, {}),
  disarmAll: () => sendJson<{ ok: boolean; disarmed: number }>("POST", "/api/rules/disarm-all", {}),
  remove: (id: string) => request<{ ok: boolean }>(`/api/rules/${id}`, { method: "DELETE" }),
};

export const orderApi = {
  journal: () => request<JournalOut>("/api/orders/journal"),
  createDraft: (body: Record<string, unknown>) => sendJson<DraftView>("POST", "/api/orders/drafts", body),
  drafts: (symbol = "") =>
    request<{ drafts: DraftView[]; done: (DraftView & { result: SubmitResult | null })[]; overlays: Overlay[]; fills: FillMark[]; dry_run: boolean }>(
      `/api/orders/drafts?symbol=${encodeURIComponent(symbol)}`),
  submit: (draftId: string, pin?: string) =>
    sendJson<SubmitResult>("POST", "/api/orders/submit", { draft_id: draftId, approved: true, ...(pin ? { pin } : {}) }),
  simulated: () => request<{ dry_run: boolean; orders: SimulatedOrder[] }>("/api/orders/simulated"),
  discard: (id: string) => request<{ ok: boolean }>(`/api/orders/drafts/${id}`, { method: "DELETE" }),
  settings: () => request<OrderSettingsOut>("/api/orders/settings"),
  saveSettings: (s: { price_band_pct: string; draft_ttl_seconds: number }) =>
    sendJson<OrderSettingsOut>("PUT", "/api/orders/settings", s),
};

// ── settings page ───────────────────────────────────────────────────────

export interface UserSettings {
  general: { language: "en" | "ko" };
  chart: ChartDefaults;
  tickers: { default: string; watchlist: string[]; related: Record<string, string[]> };
  orderbook: { spread_warn_pct: Num };
  orders: {
    price_band_pct: Num; draft_ttl_seconds: number; max_order_usd: Num | null; max_day_usd: Num | null;
    max_orders_per_day: number | null; allow_market_outside_regular: boolean; rule_max_buy_pct: Num | null;
  };
  ai: { default_tier: Tier; web_search_default: boolean; daily_usd: Num; monthly_usd: Num; raw_candles: boolean; memory_turns: number; quick_prompt: string;
    risk_profile: "standard" | "aggressive" };
  news: { max_age_hours: number; tag_enabled: boolean };
  events: { items: { name: string; time: string; note: string | null }[] };
}

export interface SettingsView {
  settings: UserSettings;
  defaults: UserSettings;
  read_only: { dry_run: boolean; tiers: { name: Tier; label: string; model: string }[] };
}

export const settingsApi = {
  get: () => request<SettingsView>("/api/settings"),
  save: (patch: Partial<Record<keyof UserSettings, unknown>>) => sendJson<SettingsView>("PUT", "/api/settings", patch),
  reset: () => request<SettingsView>("/api/settings/reset", { method: "POST" }),
};

// ── explore + watchlist (phase 6 part A) ────────────────────────────────

export type RankingType = "MARKET_TRADING_VOLUME" | "MARKET_TRADING_AMOUNT" | "TOP_GAINERS" | "TOP_LOSERS"
  | "TOSS_SECURITIES_TRADING_AMOUNT" | "TOSS_SECURITIES_TRADING_VOLUME";
export type RankingDuration = "realtime" | "1d" | "1w" | "1mo" | "3mo" | "6mo" | "1y";

export interface RankingRow {
  rank: number;
  symbol: string;
  name: string | null;            // Korean name (Toss)
  english_name: string | null;
  security_type: string | null;
  leverage_factor: Num | null;
  currency: string;
  last_price: Num;
  base_price: Num;
  change_rate: Num | null;
  change_pct: Num | null;         // calculated in code
  trading_volume: Num;
  trading_amount: Num;
}

export interface RankingsOut {
  type: RankingType;
  duration: RankingDuration;
  market: string;
  ranked_at: string | null;
  base_price_means: string;
  volume_counts: string;
  rows: RankingRow[];
  meta: SourceMeta;
}

export interface SectorItem { sector_id: string; name: string; depth: number; parent_sector_id: string | null; children: number }
export interface SectorsOut { sectors: SectorItem[]; meta: SourceMeta }

export interface SectorRankingRow {
  rank: number; sector_id: string; name: string; change_rate: Num | null; change_pct: Num | null;
  trading_amount: Num | null; market_cap: Num | null; currency: string;
}
export interface SectorRankingsOut { type: string; duration: string; rows: SectorRankingRow[]; amounts_note: string; meta: SourceMeta }

export interface SectorPage {
  detail: { sector_id: string; name: string; depth: number; parent_sector_id: string | null; summary: string | null;
    change_rates: { duration: string; change_rate: Num | null; change_pct: Num | null }[] } | null;
  stocks: { symbol: string; name: string; market_cap: Num | null; market_cap_delay: string; volume: Num | null;
    trading_amount: Num | null; currency: string; last_price: Num | null; price_currency: string | null }[];
  etfs: { symbol: string; name: string; last_price: Num | null; price_currency: string | null }[];
  prices_for_top: number;
  notes: string[];
  meta: Record<"detail" | "stocks" | "etfs", SourceMeta> & { prices: SourceMeta | null };
}

export interface WatchFolder { id: string; name: string; pinned: boolean; symbols: string[] }
export interface WatchlistDoc { version: number; folders: WatchFolder[]; updated: string | null }
export interface WatchQuote {
  symbol: string; name: string | null; last_price: Num | null; currency: string | null; price_time: string | null;
  prev_close: Num | null; prev_close_date: string | null; change: Num | null; change_pct: Num | null;
}
export interface WatchQuotesOut { folder: string; items: WatchQuote[]; prev_close_pending: number; meta: SourceMeta | null }

export const exploreApi = {
  rankings: (type: RankingType, duration: RankingDuration, count = 50) =>
    request<RankingsOut>(`/api/explore/rankings?type=${type}&duration=${duration}&count=${count}`),
  sectors: () => request<SectorsOut>("/api/explore/sectors"),
  sectorRankings: (type: "TOP_GAINERS" | "MARKET_TRADING_AMOUNT", duration: string) =>
    request<SectorRankingsOut>(`/api/explore/sectors/rankings?type=${type}&duration=${encodeURIComponent(duration)}`),
  sector: (id: string) => request<SectorPage>(`/api/explore/sectors/${encodeURIComponent(id)}`),
};

export const watchApi = {
  get: () => request<WatchlistDoc>("/api/watchlist"),
  save: (doc: { folders: Partial<WatchFolder>[] }) => sendJson<WatchlistDoc>("PUT", "/api/watchlist", doc),
  quotes: (folder: string) => request<WatchQuotesOut>(`/api/watchlist/quotes?folder=${encodeURIComponent(folder)}`),
};

// ── research / Company view (phase 6 part B) ────────────────────────────

export interface RsMeta { status: string; source: string | null; updated: string | null; error: string | null;
  error_code?: string | null; delay?: string; note?: string; reason?: string; fallback?: boolean; cik?: number }

export interface RsOverview {
  symbol: string;
  kind: "stock" | "etf" | "depositary_receipt";
  basics: { name: string; english_name: string | null; market: string; security_type: string; status: string;
    currency: string; list_date: string | null; shares_outstanding: Num; shares_note: string | null;
    leverage_factor: Num | null } | null;
  profile: { name: string | null; country: string | null; filing_currency: string | null; exchange: string | null;
    ipo: string | null; industry: string | null; logo: string | null; weburl: string | null } | null;
  next_earnings: { date: string; hour: string | null; eps_estimate: Num | null; revenue_estimate: Num | null;
    currency: string; note: string } | null;
  price: { last: Num; currency: string; time: string | null } | null;
  sector: { sector_id: string; name: string; depth: number } | null;
  sector_index: { status: string; error: string | null; progress: number[]; built_at: string | null; sectors: number };
  meta: Record<string, RsMeta>;
}

export interface RsCell { value: Num | null; note: string | null; derived: boolean; concept: string | null }
export interface RsTable {
  currency: string; entity: string | null; taxonomy: string | null; warnings: string[]; latest_filed: string | null;
  periods: { key: string; label: string; kind: string; end: string }[];
  ttm: { label: string; cells: Record<string, RsCell> } | null;
  rows: { id: string; label: string; statement: string; measure: "money" | "per_share" | "shares";
    cells: Record<string, RsCell> }[];
}
export interface RsFinancials { symbol: string; freq: string; table: RsTable | null; meta: RsMeta }

export interface PackValue { id: string; label: string; value: Num | null; display: string; kind: string; period: string;
  source: string; as_of: string | null; currency: string | null; note: string | null }
export interface RsMetrics { symbol: string; pack: { currency: string; entity: string | null; warnings: string[];
  values: PackValue[] } | null; meta: Record<string, RsMeta | string[]> }

export interface RsEarnings {
  symbol: string;
  surprises: { period: string | null; year: number | null; quarter: number | null; actual: Num | null;
    estimate: Num | null; surprise: Num | null; surprise_pct: Num | null; currency: string }[];
  calendar: { date: string | null; hour: string | null; eps_estimate: Num | null; eps_actual: Num | null;
    revenue_estimate: Num | null; revenue_actual: Num | null; currency: string }[];
  sec_eps_history: { period: string; end: string; eps_diluted: Num | null; currency: string; note: string | null }[];
  notes: string[]; currency_note: string; meta: Record<string, RsMeta>;
}

export interface RsAnalysts {
  symbol: string;
  ratings: { period: string | null; strong_buy: number | null; buy: number | null; hold: number | null;
    sell: number | null; strong_sell: number | null }[];
  targets: { low: Num | null; median: Num | null; mean: Num | null; high: Num | null; current: Num | null;
    currency: string; source: string; currency_note: string } | null;
  price: { last: Num; currency: string } | null;
  meta: Record<string, RsMeta>;
}

export interface RsPeers {
  symbol: string; sector: { sector_id: string; name: string; depth: number } | null;
  peers: { symbol: string; name: string | null; english_name: string | null; source: string | null;
    metrics?: Record<string, Num | null>; display?: Record<string, string>; note?: string | null }[];
  medians: { metric_id: string; value: Num | null; count: number; of: number; note: string | null }[];
  note: string | null; sector_index: { status: string; progress: number[]; error: string | null };
  meta?: RsMeta;
}

export interface RsNews { symbol: string; days: number; meta: RsMeta;
  items: { id: string; headline: string; source: string; url: string; published_kst: string | null;
    tag: { priority?: string; direction?: string; reason_en?: string; reason_ko?: string } | null }[] }

export interface RsThesis {
  id: number; symbol: string; saved_at: string; saved_by: "user" | "ai"; current: boolean;
  verdict: Partial<Record<"own_history" | "peers" | "growth", "cheap" | "fair" | "expensive" | "unclear">>;
  summary: string; basis: string[];
  reference_levels: { id: string; label: string; price: Num; display: string; currency: string | null; source: string;
    as_of: string | null; show_on_chart: boolean }[];
  risks: string[]; change_view: string[]; price_at_save: Num | null; currency: string | null;
  price_time?: string | null; price_source?: string; price_status?: string | null;
  latest_filed: string | null; conversation_id: string | null; edited_by_user?: boolean; warnings?: string[];
}
export interface RsThesisView {
  symbol: string; thesis: RsThesis | null; memory_version: string;
  facts: { price_now?: { last: Num; currency: string; time: string | null; source: string; status?: string | null };
    moved_since_save?: { value: Num; display: string; stale?: boolean }; new_filing?: boolean | null;
    latest_filed?: string | null;
    next_earnings?: { date: string; hour: string | null; days_away: number } };
  history: { id: number; saved_at: string; saved_by: "user" | "ai"; current: boolean; summary: string }[];
  notes: { id: number; created: string; source: "user" | "pinned" | "code"; text: string }[];
}
export interface ThesisSave {
  conversation_id?: string; message_id?: number; card_index?: number;
  summary?: string; show_on_chart?: Record<string, boolean>;
}

const rs = (sym: string, path: string) => `/api/research/${encodeURIComponent(sym)}/${path}`;
export const researchApi = {
  overview: (s: string) => request<RsOverview>(rs(s, "overview")),
  financials: (s: string, freq: "annual" | "quarterly") => request<RsFinancials>(rs(s, `financials?freq=${freq}`)),
  metrics: (s: string) => request<RsMetrics>(rs(s, "metrics")),
  earnings: (s: string) => request<RsEarnings>(rs(s, "earnings")),
  analysts: (s: string) => request<RsAnalysts>(rs(s, "analysts")),
  peers: (s: string) => request<RsPeers>(rs(s, "peers")),
  news: (s: string, days = 30) => request<RsNews>(rs(s, `news?days=${days}`)),
  thesis: (s: string) => request<RsThesisView>(rs(s, "thesis")),
  saveThesis: (s: string, body: ThesisSave) => sendJson<RsThesisView>("PUT", rs(s, "thesis"), body),
  deleteThesis: (s: string) => request<{ ok: boolean }>(rs(s, "thesis"), { method: "DELETE" }),
  addNote: (s: string, text: string) => sendJson<{ note: unknown }>("POST", rs(s, "notes"), { text }),
  deleteNote: (s: string, id: number) => request<{ ok: boolean }>(rs(s, `notes/${id}`), { method: "DELETE" }),
};
