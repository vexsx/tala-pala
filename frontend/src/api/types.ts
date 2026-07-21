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
  IR_GOLD_FUND_TALA: 'Tala fund (طلا)',
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
}

export interface MarketSummary {
  /** Full price objects (same shape as /prices/current entries), null when no data. */
  current_18k: CurrentPrice | null
  xau_usd: CurrentPrice | null
  usd_irt: CurrentPrice | null
  theoretical_18k: number | null
  premium_pct: number | null
  premium_avg_30d: number | null
  last_update: string | null
  providers: ProviderHealth[]
  signal: SignalSummary | null
}

export interface PremiumPoint {
  date: string
  observed_18k: number
  theoretical_18k: number
  premium_pct: number
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

export interface Prediction {
  id: number
  horizon: Horizon
  created_at: string
  /** The live API emits predicted_at; created_at kept for older payloads. */
  predicted_at?: string
  target_time: string
  base_value: number
  predicted_value: number
  /** The live API name for the point estimate (mirrors predicted_value). */
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
  horizon: Horizon
  model_name: string
  version: string
  active: boolean
  trained_at: string
  metrics?: ModelMetrics
  baseline_metrics?: ModelMetrics
}

export interface LiveAccuracy {
  n: number
  directional_accuracy?: number
  mae?: number
  smape?: number
}

export interface HorizonPerformance {
  horizon: Horizon
  model_name: string
  version?: string
  metrics?: ModelMetrics
  baseline?: ModelMetrics
  live_accuracy?: LiveAccuracy
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
  created_at: string
  acknowledged?: boolean
  acked_at?: string | null
}
