import type { CandleOverlayField, CandleOverlays, ChartCandle } from '../../api/types'
import { readIndicators, writeIndicators } from '../prefs'

/**
 * The indicator catalogue: what can go on the chart, what its settings mean,
 * which of them are refused, and what each one plots.
 *
 * This module is PURE. It touches no DOM and imports nothing from
 * lightweight-charts, so the arithmetic below can be pinned by a test without
 * a canvas anywhere in sight. `series.ts` turns the plots into chart series;
 * `panes.tsx` puts the oscillators in their own panes; `ChartLegend.tsx`
 * reads the values back out.
 *
 * Two rules govern where a number comes from:
 *
 *  1. PREFER THE SERVER'S SERIES. /market/candles already returns sma_20,
 *     sma_50, bollinger_*, supertrend, psar, ichimoku_*, pivots and
 *     support/resistance for the exact buckets on screen. Recomputing those
 *     in the browser would be a second engine that drifts from the one the
 *     signals, alerts and Technical page are built on.
 *  2. WHERE THE BROWSER MUST COMPUTE, MATCH THE SERVER'S FORMULA EXACTLY.
 *     The EMA 26/48/220 preset and the RSI/MACD panes have no server series
 *     for an arbitrary timeframe, so they are computed here — as line-for-line
 *     mirrors of backend-go/internal/indicators/indicators.go, which is itself
 *     the same SMA-seeded EMA as prediction-python trend_alignment.py::ema.
 *     Pandas-style seeding (first value, not the SMA) gives different numbers,
 *     and the chart would then disagree with the trend card about the same
 *     market.
 */

// ---------------------------------------------------------------------------
// Maths — mirrors backend-go/internal/indicators/indicators.go
// ---------------------------------------------------------------------------

/** Simple moving average. Null until `period` values have been seen. */
export function smaSeries(values: number[], period: number): Array<number | null> {
  const out: Array<number | null> = new Array(values.length).fill(null)
  if (period <= 0 || values.length < period) return out
  let sum = 0
  for (let i = 0; i < values.length; i++) {
    sum += values[i]
    if (i >= period) sum -= values[i - period]
    if (i >= period - 1) out[i] = sum / period
  }
  return out
}

/**
 * Exponential moving average, SEEDED WITH THE SMA of the first `period`
 * values — never with the first value alone.
 *
 * This is the seeding both back ends use, and the trend-alignment card on
 * Overview is drawn from it. Seeding from values[0] instead (the pandas
 * `ewm(adjust=False)` default) makes the early EMA a function of one arbitrary
 * tick and leaves a visible offset for hundreds of bars afterwards — the chart
 * would then draw an EMA220 that disagrees with the EMA220 the desk is being
 * told about. src/test/indicators.test.ts pins this against hand arithmetic.
 */
export function emaSeries(values: number[], period: number): Array<number | null> {
  const out: Array<number | null> = new Array(values.length).fill(null)
  if (period <= 0 || values.length < period) return out
  let sum = 0
  for (let i = 0; i < period; i++) sum += values[i]
  let prev = sum / period
  out[period - 1] = prev
  const k = 2 / (period + 1)
  for (let i = period; i < values.length; i++) {
    prev = (values[i] - prev) * k + prev
    out[i] = prev
  }
  return out
}

/**
 * EMA over a series whose head is null — the MACD signal line. Seeded with the
 * mean of the first `period` non-null values, matching emaOverNaN in the Go
 * implementation.
 */
function emaOverNulls(values: Array<number | null>, period: number): Array<number | null> {
  const out: Array<number | null> = new Array(values.length).fill(null)
  if (period <= 0) return out
  const start = values.findIndex((v) => v !== null)
  if (start < 0 || values.length - start < period) return out
  let sum = 0
  for (let i = start; i < start + period; i++) sum += values[i] as number
  let prev = sum / period
  out[start + period - 1] = prev
  const k = 2 / (period + 1)
  for (let i = start + period; i < values.length; i++) {
    prev = ((values[i] as number) - prev) * k + prev
    out[i] = prev
  }
  return out
}

function rsiValue(avgGain: number, avgLoss: number): number {
  if (avgLoss === 0) return avgGain === 0 ? 50 : 100
  return 100 - 100 / (1 + avgGain / avgLoss)
}

/** Wilder's RSI — the smoothing the Go server uses, not a plain average. */
export function rsiSeries(values: number[], period: number): Array<number | null> {
  const out: Array<number | null> = new Array(values.length).fill(null)
  if (period <= 0 || values.length < period + 1) return out
  let gainSum = 0
  let lossSum = 0
  for (let i = 1; i <= period; i++) {
    const d = values[i] - values[i - 1]
    if (d > 0) gainSum += d
    else lossSum -= d
  }
  let avgGain = gainSum / period
  let avgLoss = lossSum / period
  out[period] = rsiValue(avgGain, avgLoss)
  for (let i = period + 1; i < values.length; i++) {
    const d = values[i] - values[i - 1]
    const gain = d > 0 ? d : 0
    const loss = d < 0 ? -d : 0
    avgGain = (avgGain * (period - 1) + gain) / period
    avgLoss = (avgLoss * (period - 1) + loss) / period
    out[i] = rsiValue(avgGain, avgLoss)
  }
  return out
}

export interface MacdSeries {
  line: Array<number | null>
  signal: Array<number | null>
  hist: Array<number | null>
}

/** MACD line, its EMA signal and the histogram, all SMA-seeded like the server. */
export function macdSeries(
  values: number[],
  fast: number,
  slow: number,
  signalPeriod: number
): MacdSeries {
  const ef = emaSeries(values, fast)
  const es = emaSeries(values, slow)
  const line: Array<number | null> = values.map((_, i) =>
    ef[i] !== null && es[i] !== null ? (ef[i] as number) - (es[i] as number) : null
  )
  const signal = emaOverNulls(line, signalPeriod)
  const hist: Array<number | null> = values.map((_, i) =>
    line[i] !== null && signal[i] !== null ? (line[i] as number) - (signal[i] as number) : null
  )
  return { line, signal, hist }
}

// ---------------------------------------------------------------------------
// The catalogue
// ---------------------------------------------------------------------------

export type IndicatorKind =
  /** A configurable moving average computed here (EMA or SMA, any period). */
  | 'ma'
  /** The server's SMA 20 and SMA 50 pair. */
  | 'sma'
  | 'bollinger'
  | 'supertrend'
  | 'psar'
  | 'ichimoku'
  | 'pivots'
  | 'sr'
  | 'rsi'
  | 'macd'

export type MaMethod = 'ema' | 'sma'

/** Only the close is offered; the API returns no other per-bucket series. */
export type MaSource = 'close'

export const MA_METHODS: MaMethod[] = ['ema', 'sma']
export const MA_SOURCES: MaSource[] = ['close']

export interface IndicatorInstance {
  /** Unique on the chart; equal to the instance's spec string. */
  id: string
  kind: IndicatorKind
  /** 'ma' and 'rsi'. */
  period?: number
  method?: MaMethod
  source?: MaSource
  /** 'macd'. */
  fast?: number
  slow?: number
  signal?: number
  /** A hidden indicator keeps its settings and its legend row. */
  visible: boolean
}

/** The three chart-wide overlays, which are toggles rather than indicators. */
export interface OverlayToggles {
  forecast: boolean
  events: boolean
  trend: boolean
}

export interface ChartIndicatorState {
  instances: IndicatorInstance[]
  overlays: OverlayToggles
}

export interface IndicatorDef {
  kind: IndicatorKind
  label: string
  /** Menu grouping. */
  group: 'overlay' | 'oscillator'
  /** True when every number comes from /market/candles. */
  server: boolean
  /** Period/method/source are editable. */
  configurable: boolean
  /** What the indicator is, in one line — shown in the menu and as a title. */
  hint: string
}

export const INDICATOR_DEFS: IndicatorDef[] = [
  {
    kind: 'ma',
    label: 'Moving average',
    group: 'overlay',
    server: false,
    configurable: true,
    hint: 'EMA or SMA of the close, any period. EMAs are SMA-seeded to match the trend card.'
  },
  {
    kind: 'sma',
    label: 'SMA 20/50',
    group: 'overlay',
    server: true,
    configurable: false,
    hint: 'The server’s own 20 and 50 bar simple moving averages.'
  },
  {
    kind: 'bollinger',
    label: 'Bollinger',
    group: 'overlay',
    server: true,
    configurable: false,
    hint: 'Server Bollinger bands around the 20 bar mean.'
  },
  {
    kind: 'supertrend',
    label: 'SuperTrend',
    group: 'overlay',
    server: true,
    configurable: false,
    hint: 'Server SuperTrend stop line.'
  },
  {
    kind: 'psar',
    label: 'PSAR',
    group: 'overlay',
    server: true,
    configurable: false,
    hint: 'Server parabolic SAR dots.'
  },
  {
    kind: 'ichimoku',
    label: 'Ichimoku',
    group: 'overlay',
    server: true,
    configurable: false,
    hint: 'Server Tenkan, Kijun and the two Senkou spans.'
  },
  {
    kind: 'pivots',
    label: 'Pivots',
    group: 'overlay',
    server: true,
    configurable: false,
    hint: 'Classic pivot levels from the server, drawn as price lines.'
  },
  {
    kind: 'sr',
    label: 'Support / resistance',
    group: 'overlay',
    server: true,
    configurable: false,
    hint: 'The server’s support and resistance levels, drawn as price lines.'
  },
  {
    kind: 'rsi',
    label: 'RSI',
    group: 'oscillator',
    server: false,
    configurable: true,
    hint: 'Wilder’s RSI in its own pane. Computed here — the API has no RSI for an arbitrary timeframe.'
  },
  {
    kind: 'macd',
    label: 'MACD',
    group: 'oscillator',
    server: false,
    configurable: true,
    hint: 'MACD line, signal and histogram in their own pane. Computed here, SMA-seeded.'
  }
]

const DEF_BY_KIND = new Map<IndicatorKind, IndicatorDef>(INDICATOR_DEFS.map((d) => [d.kind, d]))

export function indicatorDef(kind: IndicatorKind): IndicatorDef | null {
  return DEF_BY_KIND.get(kind) ?? null
}

/** Indicators that live in their own stacked pane rather than on the price. */
export function isPaneIndicator(kind: IndicatorKind): boolean {
  return kind === 'rsi' || kind === 'macd'
}

// ---------------------------------------------------------------------------
// Validation
// ---------------------------------------------------------------------------

export type Validation<T> = { ok: true; value: T } | { ok: false; message: string }

/**
 * A period is a COUNT OF BARS. 14.5 bars, 0 bars and a million bars are all
 * refusals rather than crashes: lightweight-charts will happily accept a
 * series of NaN and draw nothing, which looks like a broken chart instead of
 * a rejected setting.
 */
export const MIN_PERIOD = 2
export const MAX_PERIOD = 1000

export function validatePeriod(raw: unknown, label = 'Period'): Validation<number> {
  const value = typeof raw === 'string' ? (raw.trim() === '' ? NaN : Number(raw)) : Number(raw)
  if (!Number.isFinite(value)) return { ok: false, message: `${label} must be a number of bars.` }
  if (!Number.isInteger(value)) {
    return { ok: false, message: `${label} must be a whole number of bars.` }
  }
  if (value < MIN_PERIOD) {
    return { ok: false, message: `${label} must be at least ${MIN_PERIOD} bars.` }
  }
  if (value > MAX_PERIOD) {
    return { ok: false, message: `${label} must be ${MAX_PERIOD} bars or fewer.` }
  }
  return { ok: true, value }
}

export interface MaSettings {
  method: MaMethod
  period: number
  source: MaSource
}

export function validateMaSettings(raw: {
  method?: unknown
  period?: unknown
  source?: unknown
}): Validation<MaSettings> {
  const method = raw.method
  if (typeof method !== 'string' || !(MA_METHODS as string[]).includes(method)) {
    return { ok: false, message: 'Method must be EMA or SMA.' }
  }
  const source = raw.source ?? 'close'
  if (typeof source !== 'string' || !(MA_SOURCES as string[]).includes(source)) {
    return { ok: false, message: 'Source must be the close.' }
  }
  const period = validatePeriod(raw.period)
  if (!period.ok) return period
  return { ok: true, value: { method: method as MaMethod, period: period.value, source: 'close' } }
}

export interface MacdSettings {
  fast: number
  slow: number
  signal: number
}

export function validateMacdSettings(raw: {
  fast?: unknown
  slow?: unknown
  signal?: unknown
}): Validation<MacdSettings> {
  const fast = validatePeriod(raw.fast, 'Fast period')
  if (!fast.ok) return fast
  const slow = validatePeriod(raw.slow, 'Slow period')
  if (!slow.ok) return slow
  const signal = validatePeriod(raw.signal, 'Signal period')
  if (!signal.ok) return signal
  if (fast.value >= slow.value) {
    return { ok: false, message: 'The fast period must be shorter than the slow period.' }
  }
  return { ok: true, value: { fast: fast.value, slow: slow.value, signal: signal.value } }
}

// ---------------------------------------------------------------------------
// Instances, specs and persistence
// ---------------------------------------------------------------------------

/**
 * An instance serialises to one short string so the whole chart state fits the
 * `string[]` contract prefs.ts already stores. A leading '!' means hidden.
 *   ma:ema:26   sma   rsi:14   macd:12:26:9   overlay:forecast   !bollinger
 */
export function formatSpec(instance: IndicatorInstance): string {
  return `${instance.visible ? '' : '!'}${instance.id}`
}

function idFor(
  kind: IndicatorKind,
  parts: Partial<Pick<IndicatorInstance, 'method' | 'period' | 'fast' | 'slow' | 'signal'>>
): string {
  if (kind === 'ma') return `ma:${parts.method}:${parts.period}`
  if (kind === 'rsi') return `rsi:${parts.period}`
  if (kind === 'macd') return `macd:${parts.fast}:${parts.slow}:${parts.signal}`
  return kind
}

/** Build a validated instance, or explain the refusal. Never throws. */
export function makeInstance(
  kind: IndicatorKind,
  settings: Record<string, unknown> = {},
  visible = true
): Validation<IndicatorInstance> {
  if (!DEF_BY_KIND.has(kind)) return { ok: false, message: `Unknown indicator: ${String(kind)}` }
  if (kind === 'ma') {
    const v = validateMaSettings({
      method: settings.method ?? 'ema',
      period: settings.period ?? 26,
      source: settings.source ?? 'close'
    })
    if (!v.ok) return v
    return {
      ok: true,
      value: {
        id: idFor('ma', v.value),
        kind,
        method: v.value.method,
        period: v.value.period,
        source: v.value.source,
        visible
      }
    }
  }
  if (kind === 'rsi') {
    const v = validatePeriod(settings.period ?? 14)
    if (!v.ok) return v
    return { ok: true, value: { id: idFor('rsi', { period: v.value }), kind, period: v.value, visible } }
  }
  if (kind === 'macd') {
    const v = validateMacdSettings({
      fast: settings.fast ?? 12,
      slow: settings.slow ?? 26,
      signal: settings.signal ?? 9
    })
    if (!v.ok) return v
    return { ok: true, value: { id: idFor('macd', v.value), kind, ...v.value, visible } }
  }
  return { ok: true, value: { id: kind, kind, visible } }
}

const OVERLAY_KEYS: Array<keyof OverlayToggles> = ['forecast', 'events', 'trend']

/** Parse one persisted spec. Returns null for anything this build cannot draw. */
export function parseSpec(raw: string): IndicatorInstance | null {
  const trimmed = raw.trim()
  if (trimmed === '') return null
  const visible = !trimmed.startsWith('!')
  const body = visible ? trimmed : trimmed.slice(1)
  const [kind, ...args] = body.split(':')
  if (kind === 'ma') {
    const made = makeInstance('ma', { method: args[0], period: args[1] }, visible)
    return made.ok ? made.value : null
  }
  if (kind === 'rsi') {
    const made = makeInstance('rsi', { period: args[0] }, visible)
    return made.ok ? made.value : null
  }
  if (kind === 'macd') {
    const made = makeInstance('macd', { fast: args[0], slow: args[1], signal: args[2] }, visible)
    return made.ok ? made.value : null
  }
  if (!DEF_BY_KIND.has(kind as IndicatorKind)) return null
  const made = makeInstance(kind as IndicatorKind, {}, visible)
  return made.ok ? made.value : null
}

// ---------------------------------------------------------------------------
// State transitions — pure, so the menu and the legend cannot disagree
// ---------------------------------------------------------------------------

export function addInstance(
  state: ChartIndicatorState,
  instance: IndicatorInstance
): Validation<ChartIndicatorState> {
  if (state.instances.some((i) => i.id === instance.id)) {
    return { ok: false, message: `${instanceLabel(instance)} is already on the chart.` }
  }
  return { ok: true, value: { ...state, instances: [...state.instances, instance] } }
}

export function removeInstance(state: ChartIndicatorState, id: string): ChartIndicatorState {
  return { ...state, instances: state.instances.filter((i) => i.id !== id) }
}

/**
 * Swap an instance's settings in place. The replacement keeps its position in
 * the list so re-tuning a period does not reshuffle the legend under the
 * pointer that is still hovering it.
 */
export function replaceInstance(
  state: ChartIndicatorState,
  id: string,
  next: IndicatorInstance
): Validation<ChartIndicatorState> {
  if (next.id !== id && state.instances.some((i) => i.id === next.id)) {
    return { ok: false, message: `${instanceLabel(next)} is already on the chart.` }
  }
  return {
    ok: true,
    value: { ...state, instances: state.instances.map((i) => (i.id === id ? next : i)) }
  }
}

export function setVisibility(
  state: ChartIndicatorState,
  id: string,
  visible: boolean
): ChartIndicatorState {
  return {
    ...state,
    instances: state.instances.map((i) => (i.id === id ? { ...i, visible } : i))
  }
}

export function setOverlay(
  state: ChartIndicatorState,
  key: keyof OverlayToggles,
  on: boolean
): ChartIndicatorState {
  return { ...state, overlays: { ...state.overlays, [key]: on } }
}

/**
 * Periods a new moving average is offered, in order. Trying a ladder rather
 * than always adding EMA 26 means "add another MA" keeps working instead of
 * refusing the second click.
 */
const MA_LADDER = [26, 48, 220, 9, 12, 20, 50, 100, 200]

export function nextMovingAverage(instances: IndicatorInstance[]): IndicatorInstance | null {
  const taken = new Set(instances.map((i) => i.id))
  for (const period of MA_LADDER) {
    const made = makeInstance('ma', { method: 'ema', period })
    if (made.ok && !taken.has(made.value.id)) return made.value
  }
  return null
}

// ---------------------------------------------------------------------------
// Presets
// ---------------------------------------------------------------------------

export type PresetId = 'clean' | 'trend' | 'alignment' | 'momentum' | 'full'

export interface Preset {
  id: PresetId
  label: string
  specs: string[]
  overlays: OverlayToggles
  hint: string
}

const NO_OVERLAYS: OverlayToggles = { forecast: false, events: false, trend: false }

/**
 * The default is TREND, not everything: three lines and a pivot ladder stay
 * readable on a phone, and it is the set this desk already had on screen.
 * "Full technical" exists for the times you want the whole board, and is
 * deliberately not the thing a first-time visitor lands on.
 */
export const PRESETS: Preset[] = [
  {
    id: 'clean',
    label: 'Clean',
    specs: [],
    overlays: { ...NO_OVERLAYS },
    hint: 'Candles only.'
  },
  {
    id: 'trend',
    label: 'Trend',
    specs: ['sma', 'supertrend', 'pivots', 'sr'],
    overlays: { ...NO_OVERLAYS },
    hint: 'Server SMA 20/50, SuperTrend, the pivot ladder and support/resistance.'
  },
  {
    id: 'alignment',
    label: 'Trend alignment',
    specs: ['ma:ema:26', 'ma:ema:48', 'ma:ema:220', 'supertrend'],
    overlays: { ...NO_OVERLAYS, trend: true },
    hint: 'EMA 26/48/220 plus the server’s 1D/4H/1H alignment read.'
  },
  {
    id: 'momentum',
    label: 'Momentum',
    specs: ['rsi:14', 'macd:12:26:9'],
    overlays: { ...NO_OVERLAYS },
    hint: 'RSI and MACD in their own panes.'
  },
  {
    id: 'full',
    label: 'Full technical',
    specs: [
      'sma',
      'bollinger',
      'supertrend',
      'psar',
      'ichimoku',
      'pivots',
      'sr',
      'rsi:14',
      'macd:12:26:9'
    ],
    overlays: { ...NO_OVERLAYS },
    hint: 'Everything the server serves, plus both oscillator panes.'
  }
]

export const DEFAULT_PRESET: PresetId = 'trend'

export function preset(id: PresetId): Preset {
  return PRESETS.find((p) => p.id === id) ?? PRESETS[1]
}

export function applyPreset(id: PresetId): ChartIndicatorState {
  const p = preset(id)
  return {
    instances: p.specs.map(parseSpec).filter((i): i is IndicatorInstance => i !== null),
    overlays: { ...p.overlays }
  }
}

/** The preset the current state matches exactly, or null for a custom board. */
export function matchingPreset(state: ChartIndicatorState): PresetId | null {
  const mine = state.instances.map((i) => i.id).sort()
  for (const p of PRESETS) {
    const theirs = p.specs.slice().sort()
    if (theirs.length !== mine.length) continue
    if (theirs.every((s, i) => s === mine[i])) {
      const sameOverlays = OVERLAY_KEYS.every((k) => p.overlays[k] === state.overlays[k])
      if (sameOverlays) return p.id
    }
  }
  return null
}

// ---------------------------------------------------------------------------
// Persistence — one array of opaque strings, exactly what prefs.ts stores
// ---------------------------------------------------------------------------

/**
 * A version token so an empty board ("Clean") is distinguishable from a user
 * who has never chosen anything. Without it, clearing every indicator would
 * silently restore the default preset on the next reload.
 */
const STATE_VERSION = 'v1'

export function serializeState(state: ChartIndicatorState): string[] {
  const out = [STATE_VERSION, ...state.instances.map(formatSpec)]
  for (const key of OVERLAY_KEYS) if (state.overlays[key]) out.push(`overlay:${key}`)
  return out
}

export function deserializeState(raw: string[]): ChartIndicatorState | null {
  if (raw.length === 0 || raw[0] !== STATE_VERSION) return null
  const state: ChartIndicatorState = { instances: [], overlays: { ...NO_OVERLAYS } }
  const seen = new Set<string>()
  for (const spec of raw.slice(1)) {
    if (spec.startsWith('overlay:')) {
      const key = spec.slice('overlay:'.length) as keyof OverlayToggles
      if (OVERLAY_KEYS.includes(key)) state.overlays[key] = true
      continue
    }
    const instance = parseSpec(spec)
    // A spec this build no longer understands is dropped rather than drawn as
    // a guess: an unknown indicator is not a reason to break the chart.
    if (instance === null || seen.has(instance.id)) continue
    seen.add(instance.id)
    state.instances.push(instance)
  }
  return state
}

export function loadIndicatorState(): ChartIndicatorState {
  return deserializeState(readIndicators()) ?? applyPreset(DEFAULT_PRESET)
}

export function saveIndicatorState(state: ChartIndicatorState): void {
  writeIndicators(serializeState(state))
}

// ---------------------------------------------------------------------------
// Labels
// ---------------------------------------------------------------------------

export function instanceLabel(instance: IndicatorInstance): string {
  switch (instance.kind) {
    case 'ma':
      return `${(instance.method ?? 'ema').toUpperCase()} ${instance.period}`
    case 'rsi':
      return `RSI ${instance.period}`
    case 'macd':
      return `MACD ${instance.fast}/${instance.slow}/${instance.signal}`
    default:
      return indicatorDef(instance.kind)?.label ?? instance.kind
  }
}

// ---------------------------------------------------------------------------
// Plots
// ---------------------------------------------------------------------------

/**
 * Palette keys resolved from the theme's CSS variables by series.ts.
 *
 * 'forecast' and 'forecast-band' are reserved for overlays.ts and used by
 * nothing else, which is what keeps an estimate from ever looking like a
 * measured indicator.
 */
export type PlotColor =
  | 'info'
  | 'purple'
  | 'warn'
  | 'accent'
  | 'pos'
  | 'neg'
  | 'muted'
  | 'forecast'
  | 'forecast-band'

export type PlotStyle = 'solid' | 'dashed' | 'dotted' | 'sparse' | 'largeDashed'

export type PlotShape = 'line' | 'dots' | 'histogram'

/**
 * Who puts the plot on the chart.
 *
 * 'chart' plots are the server overlay arrays TradingChart already draws from
 * its `overlays` prop; the registry still describes them so the legend can
 * report their values and their colour swatch without a second set of series
 * being added on top of the first.
 */
export type PlotOwner = 'chart' | 'layer'

export interface PlotPoint {
  time: number
  value: number
}

export interface IndicatorPlot {
  key: string
  instanceId: string
  label: string
  color: PlotColor
  style: PlotStyle
  width: 1 | 2
  shape: PlotShape
  /** null draws on the price pane; anything else gets its own stacked pane. */
  paneKey: string | null
  format: 'price' | 'number'
  owner: PlotOwner
  /** The value the legend reports for the instance. */
  primary: boolean
  data: PlotPoint[]
  /** Horizontal guides drawn inside the plot's own pane. */
  reference?: number[]
  /** Short tag for the price axis, e.g. the forecast's EST. */
  axisLabel?: string
}

export interface PlotContext {
  candles: ChartCandle[]
  overlays: Partial<CandleOverlays> | null
  /** Bucket times the overlay arrays belong to; empty falls back to the tail. */
  overlayTimes: number[]
}

interface ServerPlotSpec {
  field: CandleOverlayField
  label: string
  color: PlotColor
  style: PlotStyle
  width: 1 | 2
  shape: PlotShape
  primary?: boolean
}

/**
 * INVARIANT: these styles mirror OVERLAY_STYLES in TradingChart.tsx, which is
 * what actually draws them. The legend's swatch would otherwise claim a colour
 * the line does not have. TradingChart owns the drawing; this table exists so
 * the legend can describe it.
 */
const SERVER_PLOTS: Record<Exclude<IndicatorKind, 'ma' | 'rsi' | 'macd' | 'pivots' | 'sr'>, ServerPlotSpec[]> =
  {
    sma: [
      { field: 'sma_20', label: 'SMA 20', color: 'info', style: 'solid', width: 1, shape: 'line', primary: true },
      { field: 'sma_50', label: 'SMA 50', color: 'purple', style: 'solid', width: 1, shape: 'line' }
    ],
    bollinger: [
      { field: 'bollinger_upper', label: 'Upper', color: 'info', style: 'dashed', width: 1, shape: 'line' },
      { field: 'bollinger_mid', label: 'Mid', color: 'info', style: 'dotted', width: 1, shape: 'line', primary: true },
      { field: 'bollinger_lower', label: 'Lower', color: 'info', style: 'dashed', width: 1, shape: 'line' }
    ],
    supertrend: [
      { field: 'supertrend', label: 'SuperTrend', color: 'warn', style: 'solid', width: 2, shape: 'line', primary: true }
    ],
    psar: [{ field: 'psar', label: 'PSAR', color: 'accent', style: 'solid', width: 1, shape: 'dots', primary: true }],
    ichimoku: [
      { field: 'ichimoku_tenkan', label: 'Tenkan', color: 'pos', style: 'solid', width: 1, shape: 'line', primary: true },
      { field: 'ichimoku_kijun', label: 'Kijun', color: 'neg', style: 'solid', width: 1, shape: 'line' },
      { field: 'ichimoku_senkou_a', label: 'Senkou A', color: 'accent', style: 'dashed', width: 1, shape: 'line' },
      { field: 'ichimoku_senkou_b', label: 'Senkou B', color: 'purple', style: 'dashed', width: 1, shape: 'line' }
    ]
  }

/** Match values to the bucket times they were computed against, never to an index. */
function pointsFromServer(ctx: PlotContext, values: Array<number | null>): PlotPoint[] {
  const times =
    ctx.overlayTimes.length > 0
      ? ctx.overlayTimes
      : ctx.candles.slice(Math.max(ctx.candles.length - values.length, 0)).map((c) => c.t)
  const out: PlotPoint[] = []
  const n = Math.min(times.length, values.length)
  for (let i = 0; i < n; i++) {
    const v = values[i]
    if (v !== null && v !== undefined && Number.isFinite(v)) out.push({ time: times[i], value: v })
  }
  return out
}

function pointsFromComputed(candles: ChartCandle[], values: Array<number | null>): PlotPoint[] {
  const out: PlotPoint[] = []
  const n = Math.min(candles.length, values.length)
  for (let i = 0; i < n; i++) {
    const v = values[i]
    if (v !== null && Number.isFinite(v)) out.push({ time: candles[i].t, value: v })
  }
  return out
}

/** Every plot the active instances put on the chart, in draw order. */
export function buildPlots(instances: IndicatorInstance[], ctx: PlotContext): IndicatorPlot[] {
  const closes = ctx.candles.map((c) => c.close)
  const out: IndicatorPlot[] = []

  for (const instance of instances) {
    if (instance.kind === 'pivots' || instance.kind === 'sr') continue

    if (instance.kind === 'ma') {
      const period = instance.period ?? 26
      const values =
        instance.method === 'sma' ? smaSeries(closes, period) : emaSeries(closes, period)
      out.push({
        key: instance.id,
        instanceId: instance.id,
        label: instanceLabel(instance),
        // Slow averages are drawn heavier and cooler so three of them on one
        // pane stay tellable apart without reading the legend.
        color: period >= 200 ? 'purple' : period >= 40 ? 'info' : 'accent',
        style: 'solid',
        width: period >= 200 ? 2 : 1,
        shape: 'line',
        paneKey: null,
        format: 'price',
        owner: 'layer',
        primary: true,
        data: pointsFromComputed(ctx.candles, values)
      })
      continue
    }

    if (instance.kind === 'rsi') {
      const values = rsiSeries(closes, instance.period ?? 14)
      out.push({
        key: `${instance.id}:rsi`,
        instanceId: instance.id,
        label: instanceLabel(instance),
        color: 'purple',
        style: 'solid',
        width: 2,
        shape: 'line',
        paneKey: instance.id,
        format: 'number',
        owner: 'layer',
        primary: true,
        data: pointsFromComputed(ctx.candles, values),
        reference: [30, 50, 70]
      })
      continue
    }

    if (instance.kind === 'macd') {
      const m = macdSeries(closes, instance.fast ?? 12, instance.slow ?? 26, instance.signal ?? 9)
      out.push(
        {
          key: `${instance.id}:hist`,
          instanceId: instance.id,
          label: 'Histogram',
          color: 'muted',
          style: 'solid',
          width: 1,
          shape: 'histogram',
          paneKey: instance.id,
          format: 'price',
          owner: 'layer',
          primary: false,
          data: pointsFromComputed(ctx.candles, m.hist),
          reference: [0]
        },
        {
          key: `${instance.id}:line`,
          instanceId: instance.id,
          label: instanceLabel(instance),
          color: 'info',
          style: 'solid',
          width: 2,
          shape: 'line',
          paneKey: instance.id,
          format: 'price',
          owner: 'layer',
          primary: true,
          data: pointsFromComputed(ctx.candles, m.line)
        },
        {
          key: `${instance.id}:signal`,
          instanceId: instance.id,
          label: 'Signal',
          color: 'warn',
          style: 'solid',
          width: 1,
          shape: 'line',
          paneKey: instance.id,
          format: 'price',
          owner: 'layer',
          primary: false,
          data: pointsFromComputed(ctx.candles, m.signal)
        }
      )
      continue
    }

    const specs = SERVER_PLOTS[instance.kind]
    for (const spec of specs) {
      const values = ctx.overlays?.[spec.field]
      if (!Array.isArray(values) || values.length === 0) continue
      out.push({
        key: `${instance.id}:${spec.field}`,
        instanceId: instance.id,
        label: spec.label,
        color: spec.color,
        style: spec.style,
        width: spec.width,
        shape: spec.shape,
        paneKey: null,
        format: 'price',
        owner: 'chart',
        primary: spec.primary === true,
        data: pointsFromServer(ctx, values)
      })
    }
  }

  return out
}

/** The overlay fields TradingChart should draw for the current instances. */
export function serverOverlayFields(instances: IndicatorInstance[]): CandleOverlayField[] {
  const fields: CandleOverlayField[] = []
  for (const instance of instances) {
    if (!instance.visible) continue
    const specs = SERVER_PLOTS[instance.kind as keyof typeof SERVER_PLOTS]
    if (!specs) continue
    for (const spec of specs) fields.push(spec.field)
  }
  return fields
}

/** The `overlays` prop for TradingChart: only the fields the user asked for. */
export function serverOverlays(
  instances: IndicatorInstance[],
  all: CandleOverlays | null
): Partial<CandleOverlays> | null {
  if (!all) return null
  const picked: Partial<CandleOverlays> = {}
  for (const field of serverOverlayFields(instances)) {
    const values = all[field]
    if (Array.isArray(values)) (picked as Record<string, unknown>)[field] = values
  }
  return picked
}

export function hasInstance(instances: IndicatorInstance[], kind: IndicatorKind): boolean {
  return instances.some((i) => i.kind === kind && i.visible)
}

/** Levels an instance contributes to the legend, e.g. "P 8,100,000". */
export interface LevelReadout {
  label: string
  value: number
}

export function levelReadouts(
  instance: IndicatorInstance,
  levels: { pivots?: { p: number } | null; support?: number | null; resistance?: number | null }
): LevelReadout[] {
  if (instance.kind === 'pivots') {
    const p = levels.pivots
    return p ? [{ label: 'P', value: p.p }] : []
  }
  if (instance.kind === 'sr') {
    const out: LevelReadout[] = []
    if (typeof levels.support === 'number') out.push({ label: 'S', value: levels.support })
    if (typeof levels.resistance === 'number') out.push({ label: 'R', value: levels.resistance })
    return out
  }
  return []
}

/**
 * The value the legend shows: the plot's value at the crosshair, or its last
 * value when the pointer is off the chart. Returns null rather than the
 * nearest number — an indicator that has not warmed up has no value, and
 * borrowing one from a different bar would be a lie.
 */
export function valueAt(plot: IndicatorPlot, time: number | null): number | null {
  if (plot.data.length === 0) return null
  if (time === null) return plot.data[plot.data.length - 1].value
  let lo = 0
  let hi = plot.data.length - 1
  while (lo <= hi) {
    const mid = (lo + hi) >> 1
    const t = plot.data[mid].time
    if (t === time) return plot.data[mid].value
    if (t < time) lo = mid + 1
    else hi = mid - 1
  }
  return null
}

/**
 * Instances that produced no points at all in this window. Reported rather
 * than left as an invisible blank: "the line is missing" and "the line is off
 * screen" look identical otherwise.
 */
export function coldInstances(
  instances: IndicatorInstance[],
  plots: IndicatorPlot[]
): IndicatorInstance[] {
  return instances.filter((instance) => {
    if (instance.kind === 'pivots' || instance.kind === 'sr') return false
    const mine = plots.filter((p) => p.instanceId === instance.id)
    if (mine.length === 0) return true
    return mine.every((p) => p.data.length === 0)
  })
}
