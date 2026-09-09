// TypeScript mirrors of the Go API responses defined in docs/CONTRACTS.md.

// ---------- Shared ----------

export type Horizon = '1h' | '4h' | 'eod' | '1d' | '3d' | '7d' | '30d'

export const HORIZONS: Horizon[] = ['1h', '4h', 'eod', '1d', '3d', '7d', '30d']

export const HORIZON_LABELS: Record<Horizon, string> = {
  '1h': '1 hour',
  '4h': '4 hours',
  eod: 'End of day',
  '1d': '1 day',
  '3d': '3 days',
  '7d': '7 days',
  '30d': '30 days'
}

export type Symbol_ =
  | 'IR_GOLD_18K'
  | 'XAUUSD'
  | 'XAGUSD'
  | 'USD_IRT'
  | 'IR_COIN_EMAMI'
  | 'BRENT_OIL'
  | 'DXY'
  | 'US10Y'
  | 'IR_GOLD_FUND_AYAR'
  | 'IR_GOLD_FUND_TALA'
  | 'IR_GOLD_FUND_KAHRABA'
  | 'IR_GOLD_FUND_FLOW'

export const SYMBOLS: Symbol_[] = [
  'IR_GOLD_18K',
  'XAUUSD',
  'XAGUSD',
  'USD_IRT',
  'IR_COIN_EMAMI',
  'BRENT_OIL',
  'DXY',
  'US10Y',
  'IR_GOLD_FUND_AYAR',
  'IR_GOLD_FUND_TALA',
  'IR_GOLD_FUND_KAHRABA',
  'IR_GOLD_FUND_FLOW'
]

export const SYMBOL_LABELS: Record<Symbol_, string> = {
  IR_GOLD_18K: '18k gold (gram)',
  XAUUSD: 'Global gold (XAU/USD)',
  XAGUSD: 'Silver (XAG/USD)',
  USD_IRT: 'USD / IRT',
  IR_COIN_EMAMI: 'Emami coin',
  BRENT_OIL: 'Brent oil',
  DXY: 'Dollar index',
  US10Y: 'US 10Y yield',
  IR_GOLD_FUND_AYAR: 'Ayar fund (عیار)',
  IR_GOLD_FUND_TALA: 'Lotus gold fund (طلا)',
  IR_GOLD_FUND_KAHRABA: 'Kahroba fund (کهربا)',
  IR_GOLD_FUND_FLOW: 'Funds retail net flow'
}

/** TSE gold-fund price symbols shown in the Trade panel funds card. */
export const GOLD_FUND_SYMBOLS: Symbol_[] = [
  'IR_GOLD_FUND_AYAR',
  'IR_GOLD_FUND_TALA',
  'IR_GOLD_FUND_KAHRABA'
]

export interface ApiErrorEnvelope {
  error: {
    code: string
    message: string
    details?: Record<string, unknown>
  }
}

// ---------- Auth ----------

export interface User {
  id: string
  email: string
  role: 'admin' | 'user' | string
}

export interface LoginResponse {
  token: string
  expires_at: string
  user: User
}

// ---------- Prices ----------

/** Addendum 1: market-hours awareness. Optional so older payloads still parse. */
export type MarketState = 'open' | 'closed'

export interface CurrentPrice {
  value: number
  currency: string
  unit: string
  source: string
  observed_at: string
  stale: boolean
  /** 'closed' means the last observation is a last-session price, not stale data. */
  market_state?: MarketState
  change_24h_pct: number | null
}

export interface CurrentPricesResponse {
  prices: Partial<Record<Symbol_, CurrentPrice>>
  as_of: string
}

export interface PriceHistoryItem {
  observed_at: string
  value: number
  source: string
}

export interface PriceHistoryResponse {
  items: PriceHistoryItem[]
  page: number
  page_size: number
  total: number
}

// ---------- Market ----------

export interface ProviderHealth {
  code: string
  name: string
  category: string
  enabled: boolean
  priority: number
  healthy: boolean
  last_success_at: string | null
  consecutive_failures: number
  last_error: string | null
}

export type SignalLevel = 'strong_buy' | 'buy' | 'hold' | 'sell' | 'strong_sell'

export interface SignalSummary {
  signal: SignalLevel
  score: number
  confidence: number
  explanation?: string
  created_at?: string
  /** The live API emits generated_at; created_at kept for older payloads. */
  generated_at?: string
  components?: Record<string, number>
  /** Plain-language factors supporting the signal. */
  supporting?: string[]
  /** Plain-language factors conflicting with the signal. */
  conflicting?: string[]
  /** Main risks attached to the signal. */
  risks?: string[]
  /** Informational notes, e.g. "prices from last session (market closed)". */
  notes?: string[]
  /** "This view is wrong if …" condition. */
  invalidation?: string
  review_at?: string | null
  data_fresh?: boolean
  /**
   * Provenance for `confidence` and for the explanation's model claim.
   *
   * Only gold reaches this endpoint, and gold normally has trained models — but
   * when it has none, `confidence` falls back to a fixed structural constant
   * (0.3) that stands in for a measurement nobody took. Rendered as a bare
   * gauge it is indistinguishable from a measured value, which is the
   * invented-precision failure this codebase has already fixed twice
   * elsewhere. These fields let the card say which it is.
   */
  evidence_basis?: string
  confidence_basis?: string
  confidence_reason?: string
  /** null when no model ran — distinct from a model measuring zero. */
  model_confidence?: number | null
}

export interface MarketSummary {
  /** Full price objects (same shape as /prices/current entries), null when no data. */
  current_18k: CurrentPrice | null
  xau_usd: CurrentPrice | null
  usd_irt: CurrentPrice | null
  theoretical_18k: number | null
  premium_pct: number | null
  premium_avg_30d: number | null
  /**
   * Live round-trip trading cost in percent: the primary dealer's observed
   * buy/sell spread (Hamrah Gold). Null when not observed recently.
   */
  trading_cost_pct?: number | null
  last_update: string | null
  providers: ProviderHealth[]
  signal: SignalSummary | null
}

export interface PremiumPoint {
  date: string
  observed_18k: number | null
  theoretical_18k: number | null
  premium_pct: number | null
}

export interface MacdValue {
  line: number
  signal: number
  hist: number
}

export interface BollingerValue {
  upper: number
  mid: number
  lower: number
}

/** Addendum 2: 20-day high/low breakout channel. */
export interface DonchianValue {
  upper: number
  lower: number
}

/** Addendum 2: EMA20 ± 2×ATR volatility channel. */
export interface KeltnerValue {
  upper: number
  mid: number
  lower: number
}

export interface IndicatorPoint {
  date: string
  close: number
  sma_20: number | null
  sma_50: number | null
  ema_12: number | null
  ema_26: number | null
  rsi_14: number | null
  macd: MacdValue | null
  bollinger: BollingerValue | null
  atr_14: number | null
  momentum_10: number | null
  roc_10: number | null
  volatility_20: number | null
  // Addendum 2 series additions (optional so older payloads still parse).
  adx_14?: number | null
  stoch_k?: number | null
  stoch_d?: number | null
}

export interface IndicatorsResponse {
  items: IndicatorPoint[]
  support: number | null
  resistance: number | null
  // Addendum 2 scalar additions (latest values; optional for older payloads).
  adx_14?: number | null
  stoch_k?: number | null
  stoch_d?: number | null
  williams_r_14?: number | null
  cci_20?: number | null
  donchian?: DonchianValue | null
  keltner?: KeltnerValue | null
  /** Rolling 20-day correlation of daily log-returns, 18k vs XAUUSD. */
  corr_xau_20?: number | null
  /** Percent distance below the 90-day high (≤ 0 or 0). */
  drawdown_pct?: number | null
}

// ---------- Predictions & signals ----------

export type Direction = 'up' | 'down' | 'flat'

/**
 * The prediction service emits drivers as {factor, importance} (feature
 * attributions, importance 0..1) or {factor, note} (heuristic drivers, e.g.
 * "momentum_10: +1.2% over 10 steps"). Older payloads used {name, impact}.
 * All fields are optional — render defensively.
 */
export interface PredictionDriver {
  factor?: string
  importance?: number
  note?: string
  name?: string
  impact?: number
  description?: string
}

/**
 * The live API emits {point_forecast, predicted_at} and omits
 * {base_value, predicted_value, created_at}; older payloads had the reverse.
 * Run rows through normalizePrediction (src/lib/forecastChart.ts) to fill the
 * legacy fields before relying on them.
 */
export interface Prediction {
  id: number
  horizon: Horizon
  /** Older payloads; the live API emits predicted_at instead. */
  created_at?: string
  predicted_at?: string
  target_time: string
  /** Older payloads; derivable as point_forecast / (1 + expected_change_pct/100). */
  base_value?: number
  /** Older payloads; the live API emits point_forecast instead. */
  predicted_value?: number
  point_forecast?: number
  lower_bound: number
  upper_bound: number
  expected_change_pct: number
  direction: Direction
  confidence: number
  model_name: string
  model_version?: string
  drivers?: PredictionDriver[]
  warnings?: string[]
  /** False when the row was computed from stale inputs; absent means fresh. */
  data_fresh?: boolean
  actual_value: number | null
}

export interface Signal {
  id: number
  created_at: string
  /** The live API emits generated_at; created_at kept for older payloads. */
  generated_at?: string
  signal: SignalLevel
  score: number
  confidence: number
  explanation: string
  components?: Record<string, number>
  supporting?: string[]
  conflicting?: string[]
  risks?: string[]
  /** Informational notes, e.g. "prices from last session (market closed)". */
  notes?: string[]
  invalidation?: string
  review_at?: string | null
  data_fresh?: boolean
}

/** Bootstrap Monte Carlo outcome odds attached to a custom forecast. */
export interface MonteCarloOdds {
  p_up: number
  p_gain_over_cost: number
  p_loss_over_cost: number
  sim_p05_pct: number
  sim_median_pct: number
  sim_p95_pct: number
  n_paths: number
}

/** On-demand forecast for an arbitrary N-day horizon (GET /predictions/custom?days=N). */
export interface CustomForecast {
  symbol: string
  horizon_days: number
  model_name: string
  beats_naive: boolean
  point_forecast: number
  lower_bound: number
  upper_bound: number
  last_price: number
  expected_change_pct: number
  direction: Direction
  confidence: number
  regime: string
  metrics?: ModelMetrics
  drivers?: PredictionDriver[]
  decision_lean: 'buy' | 'hold' | 'sell'
  decision_note: string
  monte_carlo?: MonteCarloOdds | null
  round_trip_cost_pct: number
  provider_gap_pct: number | null
  warnings: string[]
  /** True — computed live, never stored. */
  ephemeral?: boolean
}

// ---------- Models ----------

export interface ModelMetrics {
  smape?: number
  mae?: number
  rmse?: number
  directional_accuracy?: number
}

export interface ModelVersion {
  id: number
  symbol?: string
  horizon: Horizon
  model_name: string
  version: string
  /** Older payloads; the live API emits is_active. */
  active?: boolean
  is_active?: boolean
  trained_at: string
  metrics?: ModelMetrics
  baseline_metrics?: ModelMetrics
}

export interface LiveAccuracy {
  n: number
  directional_accuracy?: number
  mae?: number
  smape?: number
  mape_pct?: number
  interval_coverage?: number
}

export interface HorizonPerformance {
  horizon: Horizon
  symbol?: string
  model_name: string
  version?: string
  metrics?: ModelMetrics
  baseline?: ModelMetrics
  live_accuracy?: LiveAccuracy | null
  degraded?: boolean
  warnings?: string[]
}

export interface TrainingRun {
  id?: number
  started_at?: string
  finished_at?: string
  status?: string
}

// ---------- Portfolio ----------

export type TxType = 'buy' | 'sell'
export type TxCurrency = 'IRT' | 'IRR'

export interface Transaction {
  id: number
  tx_type: TxType
  grams: number
  karat: number
  price_per_gram: number
  currency: TxCurrency
  fees: number
  tx_date: string
  notes: string | null
  created_at?: string
}

export interface Scenario {
  change_pct: number
  value: number
  pnl: number
}

export interface PortfolioSummary {
  total_grams_18k_equivalent: number
  invested: number
  current_value: number
  unrealized_pnl: number
  pnl_pct: number
  avg_price: number
  break_even_price: number
  scenarios: Scenario[]
  target_price_for_profit_pct: number
}

export interface PortfolioResponse extends PortfolioSummary {
  holdings: Transaction[]
}

// ---------- Admin: user management ----------

export interface AdminUser {
  id: string
  email: string
  role: 'admin' | 'user' | string
  created_at: string
  updated_at: string
  /** Portfolio transaction count — shown as a warning before deletion. */
  transactions: number
}

// ---------- Candles (trading panel) ----------

export interface Candle {
  /** Unix seconds, bucket start (UTC). */
  t: number
  open: number
  high: number
  low: number
  close: number
}

/** Index-aligned overlay arrays (null during indicator warm-up). */
export interface CandleOverlays {
  sma_20: Array<number | null>
  sma_50: Array<number | null>
  bollinger_upper: Array<number | null>
  bollinger_mid: Array<number | null>
  bollinger_lower: Array<number | null>
  supertrend: Array<number | null>
  supertrend_dir: number[]
  psar: Array<number | null>
  ichimoku_tenkan: Array<number | null>
  ichimoku_kijun: Array<number | null>
  ichimoku_senkou_a: Array<number | null>
  ichimoku_senkou_b: Array<number | null>
}

export interface PivotLevels {
  p: number
  r1: number
  r2: number
  r3: number
  s1: number
  s2: number
  s3: number
}

/** GET /market/candles — OHLC + chart-ready overlays for the trading panel. */
export interface CandlesResponse {
  symbol: string
  interval: 'daily' | 'hourly'
  candles: Candle[]
  overlays: CandleOverlays
  pivots?: PivotLevels
  support: number | null
  resistance: number | null
  as_of: string
}

// ---------- TSE gold funds ----------

export interface FundSnapshot {
  symbol: string
  ticker: string
  price: number
  change_24h_pct: number | null
  observed_at: string
  volume: number
  value: number
  retail_buy_pct: number | null
  retail_sell_pct: number | null
  /** Per-capita retail buy vs sell volume (قدرت خریدار حقیقی); >1 = buyers more eager. */
  buyer_power: number | null
  today_avg_retail_buy_pct: number | null
  today_avg_retail_sell_pct: number | null
  snapshots_today: number
}

/** GET /market/funds — the gold-fund stats panel. */
export interface FundsResponse {
  funds: FundSnapshot[]
  flow_pct: number | null
  flow_history: Array<{ date: string; flow_pct: number }>
  market_state: MarketState
  as_of: string
}

// ---------- Provider gap ----------

export interface ProviderGapQuote {
  provider: string
  value: number
  observed_at: string
}

export interface ProviderGapHistoryPoint {
  date: string
  gap_abs: number
  gap_pct: number
  mid: number
  n_providers: number
}

/** GET /market/provider-gap — dispersion between providers quoting the same symbol. */
export interface ProviderGapResponse {
  symbol: string
  window_minutes: number
  providers: ProviderGapQuote[]
  gap_abs: number | null
  gap_pct: number | null
  mid: number | null
  history?: ProviderGapHistoryPoint[]
  as_of: string
}

// ---------- Issues ----------

export type IssueService = 'api' | 'prediction' | 'frontend'
export type IssueLevel = 'warning' | 'error'

export interface AppIssue {
  id: number
  occurred_at: string
  service: IssueService
  level: IssueLevel
  source: string
  message: string
  details: Record<string, unknown> | null
}

export interface IssuesResponse {
  items: AppIssue[]
  as_of: string
}

// ---------- Alerts ----------

export type AlertType =
  | 'price_above'
  | 'price_below'
  | 'signal_change'
  | 'confidence_above'
  | 'volatility_spike'
  | 'premium_above'
  | 'stale_data'
  | 'provider_failure'
  | 'model_degradation'

export const ALERT_TYPES: AlertType[] = [
  'price_above',
  'price_below',
  'signal_change',
  'confidence_above',
  'volatility_spike',
  'premium_above',
  'stale_data',
  'provider_failure',
  'model_degradation'
]

export interface AlertCondition {
  symbol?: string
  threshold?: number
  horizon?: Horizon
  minutes?: number
  provider?: string
}

export interface Alert {
  id: number
  alert_type: AlertType
  condition: AlertCondition
  enabled: boolean
  created_at?: string
  last_triggered_at?: string | null
}

export interface AlertEvent {
  id: number
  alert_id: number
  alert_type?: AlertType
  message: string
  /** The live API emits triggered_at; created_at kept for older payloads. */
  triggered_at?: string
  created_at?: string
  acknowledged?: boolean
}

// ---------- OSINT news ----------

/** Urgent = the article is linked to a high-severity event; everything else is normal. */
export type NewsUrgency = 'urgent' | 'normal'

/**
 * One row of GET /intelligence/news. The API deliberately withholds raw
 * payloads, article bodies, content hashes, classifier confidences and rule
 * ids — this is a read-only headline view, never model input.
 */
export interface NewsItem {
  id: number
  source_code: string
  source_name: string
  /** Plain text — render as text, never as HTML. */
  title: string
  /** Canonical URL when known, else the collected URL; '' when the source gave none. */
  url: string
  /** null when the source published no timestamp. */
  published_at: string | null
  published_at_estimated: boolean
  /** When the collector stored the article — never null. */
  available_at: string
  urgency: NewsUrgency
  /** Persisted classification categories of the linked events. */
  tags: string[]
  /** Entity display names. */
  entities: string[]
  independent_source_count: number
  duplicate_count: number
}

/** GET /intelligence/news — urgent first, then available_at DESC, then id DESC. */
export interface NewsFeedResponse {
  items: NewsItem[]
  count: number
  urgent_count: number
  /** Mirrors NEWS_COLLECTION_ENABLED so the UI can explain an empty feed. */
  collection_enabled: boolean
  /** available_at of the newest item; null when the feed is empty. */
  newest_available_at: string | null
  as_of: string
}

// ---------- Trend alignment (1D / 4H / 1H moving-average read) ----------

/** How one timeframe reads: price against its three moving averages. */
export type TrendState = 'bullish' | 'bearish' | 'neutral' | 'unavailable'

/** Whether all three timeframes agree. */
export type TrendAlignmentState = 'full_bullish' | 'full_bearish' | 'not_aligned'

export type TrendMaType = 'ema' | 'sma'

/** The timeframes the engine evaluates, slowest first. */
export type TrendTimeframeKey = '1d' | '4h' | '1h'

/** Symbols the endpoint accepts — anything else is a 400. */
export type TrendSymbol = 'IR_GOLD_18K' | 'XAUUSD'

/**
 * One timeframe row — mirrors TimeframeResult.as_dict() in
 * prediction-python/app/models/trend_alignment.py. Every moving average here is
 * computed server-side; the UI only ever displays what this carries.
 */
export interface TrendTimeframe {
  timeframe: string
  trend: TrendState
  price: number | null
  ma26: number | null
  ma48: number | null
  ma220: number | null
  candle_open_time: string | null
  candle_close_time: string | null
  /** The candle these numbers were read from is closed, not still forming. */
  confirmed: boolean
  data_fresh: boolean
  ma_type: TrendMaType
  history_points: number
  /** Why a timeframe is unavailable; '' when it read cleanly. */
  reason: string
}

/**
 * GET /market/trend-alignment?symbol=… — a read-only technical indicator.
 *
 * It feeds no MODEL input, model selection, confidence or interval: the 4H and
 * 1H legs cannot be reconstructed before intraday collection began, so there is
 * no honest historical series to train on. It DOES contribute one weighted
 * factor to the rule-based buy/sell score (Addendum 21) — read live, never
 * fitted. `inputs.trend_alignment_points` on GET /signals/current is exactly
 * how many points it moved the last score.
 */
export interface TrendAlignmentResponse {
  symbol: string
  alignment: TrendAlignmentState
  previous_alignment: TrendAlignmentState | null
  timeframes: Partial<Record<TrendTimeframeKey, TrendTimeframe>>
  ma_type: TrendMaType
  /** Moving-average periods the server used — the UI labels columns from these. */
  periods: { fast: number; mid: number; slow: number }
  data_fresh: boolean
  calculated_at: string | null
  last_transition_at: string | null
  last_alert_at: string | null
  /** 'never_evaluated' when the symbol has no stored state yet. */
  note?: string | null
}

// ---------- Candles v2 (trading chart) ----------

/**
 * One time bucket of the tick stream in `prices`. These are NOT exchange OHLC
 * bars: the API buckets observations by time, so a bucket that saw a single
 * observation has open == high == low == close and no traded range at all.
 * `ticks` and `synthetic` are how the chart tells the two apart honestly.
 */
export interface ChartCandle {
  /** Unix seconds, bucket start (UTC). */
  t: number
  open_time?: string
  close_time?: string
  open: number
  high: number
  low: number
  close: number
  /** Always null — this data source carries no volume. Never invent one. */
  volume?: number | null
  /** Observations that fell in the bucket. */
  ticks?: number
  /** The bucket has ended; a false value is a still-forming bar. */
  confirmed?: boolean
  /** ticks <= 1: the high/low "range" is an artefact of a single observation. */
  synthetic?: boolean
}

/**
 * What the server can actually serve for this symbol. The chart offers only
 * the timeframes this allows rather than letting the user pick one that will
 * 400, and explains any timeframe it withholds.
 */
export interface CandleCoverage {
  /** Finest real spacing between observations; null when unknown. */
  base_granularity_seconds: number | null
  /** First instant with sub-daily observations; null means daily-only history. */
  intraday_from: string | null
  history_from: string | null
  supported_intervals: string[]
  note: string
}

/** GET /market/candles — paginated buckets plus chart-ready overlays. */
export interface ChartCandlesResponse {
  symbol: string
  interval: string
  interval_seconds: number
  timezone: string
  candles: ChartCandle[]
  has_more: boolean
  /** Pagination cursor: pass back verbatim as `before` to fetch older buckets. */
  next_before: string | null
  coverage?: CandleCoverage
  overlays?: CandleOverlays
  pivots?: PivotLevels
  support: number | null
  resistance: number | null
  as_of: string
}

// ---------- Chart drawings ----------

/**
 * One anchor of a drawing: unix seconds (UTC) and a price in the symbol's own
 * quote unit. The server stores `t` as whole seconds — see drawings.go — so a
 * fractional value comes back changed.
 */
export interface ChartDrawingPoint {
  t: number
  price: number
}

/**
 * A chart_drawings row exactly as GET/POST/PUT hand it back.
 *
 * `drawing_type` stays a plain string and `style` an untyped object on purpose:
 * both are user-authored JSONB the API stores without interpreting, so widening
 * them into the engine's own unions is validation work (model.ts's parseDrawing)
 * rather than something a type assertion may assume.
 */
export interface ChartDrawing {
  id: number
  symbol: string
  interval: string
  drawing_type: string
  points: ChartDrawingPoint[]
  style: Record<string, unknown>
  locked: boolean
  visible: boolean
  created_at: string
  updated_at: string
}

/**
 * GET /chart/drawings?symbol=&interval=.
 *
 * `truncated` means this chart holds more drawings than one response carries and
 * the client is looking at a prefix in id order. It is surfaced, never swallowed:
 * silently drawing a partial set is how a user deletes work they cannot see.
 */
export interface ChartDrawingsResponse {
  items: ChartDrawing[]
  count: number
  limit: number
  truncated: boolean
}

// ---------- Trend alignment track record (Addendum 22) ----------

/**
 * Which measurement a track-record row actually is.
 *
 * `prices` holds ticks, and until 2026-07-20 there was one tick per day, so the
 * 4H and 1H legs have weeks of history where the 1D leg has years. A row is
 * therefore never "the alignment" in the abstract:
 *
 *   'full_mtf'   — the real 1D+4H+1H alignment, replayed at every 1H close.
 *   'daily_only' — the 1D leg alone, price against its three moving averages on
 *                  daily candles. NOT the multi-timeframe alignment.
 *
 * The two are different measurements and the UI must never let a reader average
 * them together, which is why the field travels on every row.
 */
export type TrendPerformanceBasis = 'full_mtf' | 'daily_only'

/**
 * One replayed window — mirrors trendPerformanceItem in
 * backend-go/internal/prices/trendalignment.go and the columns of migration
 * 0021.
 *
 * The counts are NOT NULL because a count of zero IS the measurement. Every
 * statistic is nullable because a rate over zero bars is not a measurement at
 * all: null means "never happened in this window", which is a different fact
 * from a measured 0.0% and must never be rendered as one.
 *
 * `fwd_return_*_pct` are already percentages; `hit_rate_*` are fractions in
 * [0,1]. Both are computed by the backtest job — nothing here is derived in the
 * browser.
 */
export interface TrendPerformanceItem {
  window_days: number
  basis: TrendPerformanceBasis
  /** Bars whose forward window was fully covered — the denominator of the rates. */
  samples: number
  /** Contiguous runs of a state: N bars are not N independent trades. */
  bullish_episodes: number
  bearish_episodes: number
  bullish_bars: number
  bearish_bars: number
  unaligned_bars: number
  /** Mean forward 1-day return while bullish, in percent. */
  fwd_return_bullish_pct: number | null
  fwd_return_bearish_pct: number | null
  /** Every bar in the window whatever the state — what holding would have paid. */
  fwd_return_baseline_pct: number | null
  /** Fraction in [0,1]: bullish hits on a positive forward return, bearish on a negative one. */
  hit_rate_bullish: number | null
  hit_rate_bearish: number | null
  /** Bounds of the bar closes actually replayed; null when the window held no usable bar. */
  evaluated_from: string | null
  evaluated_to: string | null
  computed_at: string
  /** Plain-language limitation for THIS row: which basis, why, and how much of the window holds. */
  note: string
}

/**
 * GET /market/trend-alignment/performance?symbol=… — the indicator's measured
 * track record, longest window first (90, 60, 30, 14).
 *
 * A replay, not a realised trading record: no position was ever taken. A symbol
 * whose windows have not been computed yet is a 200 with an empty list.
 */
export interface TrendPerformanceResponse {
  symbol: string
  items: TrendPerformanceItem[]
  count: number
}

// ---------- Chart indicators ----------

/**
 * Overlay arrays that carry a PRICE series, i.e. everything the chart can plot
 * against the price scale. `supertrend_dir` is excluded deliberately: it is a
 * +1/-1 direction flag, and plotting it as a price would draw a line at ±1
 * toman under every candle.
 */
export type CandleOverlayField = Exclude<keyof CandleOverlays, 'supertrend_dir'>

// ---------- P1: numéraire & purchasing power ----------

/**
 * Window presets accepted by GET /markets/performance and /relative-value.
 * An explicit `from`/`to` pair overrides the preset server-side.
 */
export type MarketPeriod = '1m' | '3m' | '6m' | '1y' | '3y' | '5y' | '10y' | 'max'

export const MARKET_PERIODS: MarketPeriod[] = ['1m', '3m', '6m', '1y', '3y', '5y', '10y', 'max']

export const MARKET_PERIOD_LABELS: Record<MarketPeriod, string> = {
  '1m': '1 month',
  '3m': '3 months',
  '6m': '6 months',
  '1y': '1 year',
  '3y': '3 years',
  '5y': '5 years',
  '10y': '10 years',
  max: 'All history'
}

/**
 * The unit a return is expressed in. Never hard-code the offered set: the
 * deployment answers GET /markets/numeraires with the ones it can actually
 * back, and offering one it cannot would fill the table with nulls.
 *
 * Go's vocabulary is closed — IRT, USD, GOLD — and a request naming anything
 * else earns a 400, so the string here is the server's `key` and nothing else.
 */
export type Numeraire = string

/**
 * One row of GET /markets/numeraires, and the `numeraire_series` block echoed
 * on GET /markets/performance.
 *
 * Copied from Go: `relvalue.numeraireItem`, which embeds `relvalue.numeraireBlock`
 * — backend-go/internal/relvalue/numeraires.go and
 * backend-go/internal/relvalue/performance.go. Marshalled by `buildNumeraireList`.
 *
 * THE IDENTIFIER IS `key`. This interface used to declare `code`, `label`,
 * `series_code` and a scalar `note`, none of which Go emits; the page matched
 * nothing, fell back to `undefined` and asked for `numeraire=undefined` for the
 * rest of the session. Those four names are gone rather than deprecated, so the
 * compiler finds any reader still using them.
 */
export interface NumeraireOption {
  key: Numeraire
  /** What one converted value MEANS: 'toman', 'USD', 'grams of 18k gold'. */
  unit?: string | null
  /**
   * The display names. Go's numeraireBlock marshals `name_en`/`name_fa`; the
   * P1 contract note for this page spells the same two fields
   * `label_en`/`label_fa`. Both are read (see `numeraireLabel`) because the
   * two sides disagree today and a page that reads only one of them breaks
   * the moment the other wins.
   */
  name_en?: string | null
  name_fa?: string | null
  label_en?: string | null
  label_fa?: string | null
  /**
   * The backing instrument whose daily close performs the conversion, or null
   * for the identity numéraire (IRT converts an IRT-quoted asset by doing
   * nothing to it). NOT `series_code`.
   */
  series?: string | null
  /** Provenance of that backing series — USD_IRT is a documented proxy. */
  quality_tier?: string | null
  is_proxy?: boolean | null
  /** YYYY-MM-DD, not a timestamp: Go formats these with dateLayout. */
  coverage_from?: string | null
  coverage_to?: string | null
  observations?: number | null
  /** An ARRAY. Go emits `notes []string`; there is no scalar `note`. */
  notes?: string[] | null
  /** Item-only. Gate the menu on this: false means the request earns a 400. */
  available?: boolean
  /**
   * Item-only. Names the missing series when `available` is false. Go emits
   * null; the contract note writes it as "". Both are falsy — test for content,
   * never for `=== null`.
   */
  unavailable_reason?: string | null
  /** Item-only. True for IRT, the hub, which needs no backing series. */
  is_identity?: boolean
}

/** GET /markets/numeraires — `relvalue.numeraireListResponse`. */
export interface NumerairesResponse {
  items: NumeraireOption[]
  count?: number
  /** Whole-registry caveats, e.g. USD_IRT missing makes IRT a partial answer. */
  warnings?: string[] | null
  as_of?: string | null
  /**
   * The key to select when the client has no valid choice of its own. It is
   * the one ADDITION this page asked Go for, and it may be absent (or name a
   * numéraire that is not available) on a deployment that has not shipped it —
   * so it is a preference, not a guarantee. See `resolveNumeraire`.
   */
  default?: Numeraire | null
}

/**
 * One instrument's performance over the requested window.
 *
 * Copied from Go: `relvalue.performanceItem`, which embeds
 * `relvalue.instrumentRef` — backend-go/internal/relvalue/performance.go and
 * backend-go/internal/relvalue/instruments.go.
 *
 * EVERY metric is nullable and a null is NOT a zero: "no CPI vintage covers
 * this window" and "the price did not move" are different facts and the UI
 * must never render them the same way. `notes` carries the per-item reason a
 * field is null, which is why the reason can be shown next to the dash instead
 * of the field silently disappearing.
 *
 * `start_value` and `end_value` are in the SELECTED NUMÉRAIRE, not in
 * `quote_currency`: Go takes them from the converted series, and an instrument
 * whose quote currency has no conversion chain into the numéraire never
 * reaches `items` at all (it is named in `warnings` instead). Formatting these
 * two with `quote_currency` prints toman digits under a dollar heading.
 */
export interface MarketPerformanceItem {
  code: string
  name_en: string
  name_fa: string
  domain: string
  /** The instrument's own quote currency. Says NOTHING about start/end_value. */
  quote_currency: string
  unit: string
  /** official | official_mirror | commercial | proxy | estimate | experimental */
  quality_tier: string
  is_proxy: boolean
  is_derived?: boolean
  /** In the selected numéraire. */
  start_value: number | null
  /** In the selected numéraire. */
  end_value: number | null
  /** Return in the selected numéraire, undeflated. */
  nominal_return_pct: number | null
  /** The value re-expressed in USD before the return is taken. */
  usd_return_pct: number | null
  /** Its OWN window: shorter than the item's whenever USD_IRT quoted later. */
  usd_return_from?: string | null
  usd_return_to?: string | null
  usd_return_observations?: number | null
  /** The value re-expressed in grams of 18k gold before the return is taken. */
  gold_return_pct: number | null
  gold_return_from?: string | null
  gold_return_to?: string | null
  gold_return_observations?: number | null
  /** CPI-deflated. Its own window (below) is often NARROWER than the request. */
  real_return_pct: number | null
  real_return_from: string | null
  real_return_to: string | null
  /**
   * Sample stdev of log returns between CONSECUTIVE OBSERVATIONS — these
   * symbols do not quote every day. NOT annualised: do not scale it in the UI.
   *
   * Go renamed this from `daily_volatility_pct`, which claimed a daily step it
   * did not have. Both spellings are declared and both are read, so the column
   * shows a number on either side of that rename instead of a page of dashes.
   */
  observation_volatility_pct?: number | null
  /** @deprecated Go's former name for observation_volatility_pct. */
  daily_volatility_pct?: number | null
  max_drawdown_pct: number | null
  /** Go marshals a plain int: 0 observations is a measured zero, not a null. */
  observations: number | null
  /** YYYY-MM-DD. */
  coverage_from: string | null
  coverage_to: string | null
  /** Includes the registry's own caveat about the instrument, first. */
  notes: string[] | null
}

/**
 * GET /markets/performance?period=&numeraire=[&from=&to=] —
 * `relvalue.performanceResponse`.
 */
export interface MarketPerformanceResponse {
  period: MarketPeriod | string | null
  /** The numéraire actually served. This, not the request, dates the numbers. */
  numeraire: Numeraire
  /** Provenance of the unit of account itself, including its `unit` string. */
  numeraire_series?: NumeraireOption | null
  from: string | null
  to: string | null
  /** Which CPI series deflates `real_return_pct`, and how far it reaches. */
  cpi_series: string | null
  cpi_coverage_to: string | null
  /**
   * The deflator's full identity. Emitted by Go's cpiProvenanceBlock. A real
   * return is uninterpretable without at least base_period and quality_tier:
   * WB_CPI_IRN is the World Bank's 2010=100 rebase, NOT the Statistical Centre
   * of Iran's own 1400=100 index, and the two are not comparable level-for-level.
   */
  cpi_provenance: {
    code: string
    name_en: string
    name_fa: string
    measure: string
    unit: string
    frequency: string
    calendar: string
    base_period: string
    provider_code: string
    provider_series_id: string
    quality_tier: string
    splice_policy: string
    seasonal_adjustment: string
    notes: string
    coverage_to: string | null
  } | null
  as_of: string | null
  items: MarketPerformanceItem[]
  count?: number
  /** Names every instrument left OUT of `items`, and why. Never null. */
  warnings: string[] | null
}

/** The provenance half of a relative-value side. */
export interface RelativeValueLeg {
  code: string
  name_en: string
  name_fa: string
  quality_tier: string
  is_proxy: boolean
  unit: string
}

export interface RelativeValuePoint {
  /** Unix seconds. */
  t: number
  a_indexed: number | null
  b_indexed: number | null
  ratio: number | null
  gap_pct: number | null
}

/**
 * Why a percentile is (or is not) reportable.
 *
 * Rolling windows over one price history overlap, so 880 of them are not 880
 * observations. `independent_windows` is the honest denominator and the gate
 * is on that number; when `sufficient` is false the percentile is null and the
 * UI states the shortfall in words rather than hiding the field.
 */
export interface PercentileBasis {
  window_days: number | null
  overlapping_windows: number | null
  independent_windows: number | null
  min_independent_windows: number | null
  sufficient: boolean
  note?: string | null
}

/** GET /relative-value?a=&b=[&period=|&from=&to=][&points=] */
export interface RelativeValueResponse {
  a: RelativeValueLeg
  b: RelativeValueLeg
  /** The date both series are indexed to 100 at. */
  base_date: string | null
  from: string | null
  to: string | null
  as_of: string | null
  series: RelativeValuePoint[]
  a_growth_pct: number | null
  b_growth_pct: number | null
  /** (1+a_growth)/(1+b_growth)-1, in percent. Negative = A lagged B. */
  gap_pct: number | null
  /** Null unless percentile_basis.sufficient is true. */
  gap_percentile: number | null
  percentile_basis: PercentileBasis | null
  ratio_drawdown_pct: number | null
  warnings: string[] | null
}

/** GET /instruments — the symbol vocabulary the selectors are drawn from. */
export interface InstrumentItem {
  code: string
  kind: string
  name_en: string
  name_fa: string
  domain: string
  quote_currency: string
  unit: string
  decimals: number
  calendar_class: string
  quality_tier: string
  is_proxy: boolean
  is_derived: boolean
  enabled: boolean
  notes: string
}

// ---------------------------------------------------------------------------
// GET /api/v1/signals/overview — the advisory board contract.
//
// Derived field-for-field from backend-go/internal/signalsvc/overview.go
// (OverviewResponse, OverviewItem, CostBasis, OmittedFactor, UnavailableEntry)
// and checked against src/test/fixtures/signals-overview.json, which the Go
// side produced by marshalling those structs. Every `| null` below is a Go
// pointer field: if it is nullable here, the server can and does send null.
// ---------------------------------------------------------------------------

/**
 * Whether a trained model stood behind a reading. The single most important
 * fact on the board: "hold" from a model and "hold" from an RSI are not the
 * same claim, and only IR_GOLD_18K and XAUUSD carry active models.
 * `unknown` means the stored inputs could not be read at all — Go refuses to
 * assert "no model evidence" about a row it could not parse.
 */
export type EvidenceBasis = 'model_backed' | 'technical_only' | 'unknown'

/**
 * Provenance of the round-trip cost hurdle. `assumed` is a conservative
 * fee-plus-spread guess, `observed_spread` is a real two-sided dealer quote,
 * and `unrecorded` means the number is real but its origin was not written
 * down. A UI that cannot tell the first two apart presents a guess with the
 * authority of a quote.
 */
export type CostBasisKind = 'observed_spread' | 'assumed' | 'unrecorded'

/** One factor the scorer did not weigh, and why it was skipped. */
export interface OmittedFactor {
  factor: string
  reason: string
}

export interface SignalCostBasis {
  /** CostBasisKind in practice; widened because the server owns this vocabulary. */
  basis: CostBasisKind | string
  /** Provider code when observed; null when the figure was assumed. */
  source: string | null
  /** When the quote behind an observed spread was taken (UTC, RFC3339). */
  observed_at: string | null
  /** The generator's own sentence explaining the choice. */
  reason: string | null
}

/** One eligible symbol's latest reading. */
export interface SignalOverviewItem {
  symbol: string
  /** Display metadata joined from `instruments`. */
  name_en: string
  name_fa: string
  quote_currency: string
  unit: string
  quality_tier: string
  is_proxy: boolean
  /** The reading itself, passed through verbatim from the stored row. */
  signal: SignalLevel | string
  score: number
  /** 0..1 from the generator; run it through confidencePct() before display. */
  confidence: number
  evidence_basis: EvidenceBasis | string
  /**
   * About the INPUTS the scorer was handed: false means the prices feeding
   * this reading were stale when it was computed. It says nothing about how
   * old the READING is — `reading_stale` answers that, and the two are
   * independent.
   */
  data_fresh: boolean
  generated_at: string
  /** How long ago this row was written, at request time. */
  reading_age_hours?: number
  /**
   * True once the reading has outlived the window the engine wrote it for
   * (`stale_after_hours` on the envelope). Still shown, and flagged: an old
   * reading with its age attached is information; an old reading presented as
   * current is not. Optional so an older API does not blank the board.
   */
  reading_stale?: boolean
  /**
   * The scorer's own one-line call, from `inputs.headline` — the model-free
   * first sentence, not the blended `explanation`, which always closes with
   * "This is an uncertain, model-based assessment...". Rows written before
   * that field existed still fall back to the explanation server-side, which
   * is why AdvisoryBoard filters this against the row's own evidence basis.
   */
  headline: string
  /** First entry of the stored supporting/conflicting lists, in factor order. */
  top_supporting: string | null
  top_conflicting: string | null
  /** Never null. An empty list means every factor this asset can carry was computed. */
  omitted_factors: OmittedFactor[]
  cost_pct: number | null
  cost_basis: SignalCostBasis | null
  stale_reason: string | null
}

/**
 * Which KIND of absence an `unavailable` entry describes. The four are not
 * interchangeable, and a single sentence written over the whole list is
 * guaranteed to contradict some of the entries under it: "this system holds no
 * data for it" is true of cars and false of DXY, which is collected daily and
 * refused for being an index level rather than a price.
 *
 *   not_collected      — no series exists here at all (cars, housing).
 *   not_scored         — collected and registered, deliberately not scored.
 *   no_current_reading — signal-eligible, nothing published inside the
 *                        server's max age: never scored, or no longer scored.
 *   not_registered     — signal-eligible but absent from `instruments`, so it
 *                        cannot even be named. An operational fault.
 */
export type SignalUnavailableCategory =
  | 'not_collected'
  | 'not_scored'
  | 'no_current_reading'
  | 'not_registered'

/**
 * Something a reader might reasonably expect on the board, and why it is
 * absent, with the API's own reason attached.
 */
export interface SignalUnavailableEntry {
  symbol_or_class: string
  /**
   * Optional for one reason only: a static frontend can be served against a
   * backend that predates the field (nginx holds the built bundle, the API
   * rolls separately). The board renders an uncategorised entry under its own
   * heading rather than guessing which of the four it is.
   */
  category?: SignalUnavailableCategory | string
  reason: string
}

export interface SignalsOverviewResponse {
  /**
   * NEWEST generated_at among the items; null when nothing has been published.
   * A maximum, not a common timestamp — see `oldest_reading_at`, and never
   * stamp it over a row (each item carries its own `generated_at`).
   */
  as_of: string | null
  /** Oldest generated_at among the items; equal to `as_of` on a normal pass. */
  oldest_reading_at?: string | null
  /**
   * The server's own threshold behind each item's `reading_stale`, in hours,
   * published so a client renders the server's rule rather than inventing one.
   */
  stale_after_hours?: number
  items: SignalOverviewItem[]
  unavailable: SignalUnavailableEntry[]
}
