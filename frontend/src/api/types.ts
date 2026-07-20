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

export const SYMBOLS: Symbol_[] = [
  'IR_GOLD_18K',
  'XAUUSD',
  'XAGUSD',
  'USD_IRT',
  'IR_COIN_EMAMI',
  'BRENT_OIL',
  'DXY',
  'US10Y'
]

export const SYMBOL_LABELS: Record<Symbol_, string> = {
  IR_GOLD_18K: '18k gold (gram)',
  XAUUSD: 'Global gold (XAU/USD)',
  XAGUSD: 'Silver (XAG/USD)',
  USD_IRT: 'USD / IRT',
  IR_COIN_EMAMI: 'Emami coin',
  BRENT_OIL: 'Brent oil',
  DXY: 'Dollar index',
  US10Y: 'US 10Y yield'
}

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

export interface PredictionDriver {
  name: string
  impact: number
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
