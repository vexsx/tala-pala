import type { ChartCandle, ChartDrawing, ChartDrawingPoint } from '../../api/types'
import { parseInterval, type IntervalId } from '../intervals'

/**
 * What a drawing IS, where it lands in pixel space, and what the pointer is over.
 *
 * Anchors are (time, price) — data space, never pixels. A drawing stored in
 * screen coordinates would slide off the market the moment the user zooms or
 * pans, which is the one thing an annotation may never do. Every pixel in this
 * file is derived from an anchor on the frame it is used and then thrown away;
 * nothing here ever writes a coordinate back into a drawing.
 */

// ---------- types --------------------------------------------------------

export type DrawingType =
  | 'trend_line'
  | 'horizontal_line'
  | 'vertical_line'
  | 'rectangle'
  | 'price_range'
  | 'date_range'
  | 'measure'
  | 'fib_retracement'
  | 'text'

/**
 * Anchors per type — exact, not minimum. This mirrors drawingPointCounts in
 * backend-go/internal/prices/drawings.go, which rejects a rectangle with three
 * corners; a client that can build one only discovers it as a 400.
 */
export const DRAWING_POINT_COUNTS: Record<DrawingType, number> = {
  trend_line: 2,
  horizontal_line: 1,
  vertical_line: 1,
  rectangle: 2,
  price_range: 2,
  date_range: 2,
  measure: 2,
  fib_retracement: 2,
  text: 1
}

export const DRAWING_TYPES = Object.keys(DRAWING_POINT_COUNTS) as DrawingType[]

export const DRAWING_LABELS: Record<DrawingType, string> = {
  trend_line: 'Trend line',
  horizontal_line: 'Horizontal line',
  vertical_line: 'Vertical line',
  rectangle: 'Rectangle',
  price_range: 'Price range',
  date_range: 'Date range',
  measure: 'Measure',
  fib_retracement: 'Fib retracement',
  text: 'Text'
}

/** How far a two-point line runs past its anchors. */
export type LineExtend = 'segment' | 'ray' | 'extended'

/**
 * Palette token, not a colour. Storing '#d4a017' would freeze a drawing to the
 * theme it was made in and leave it unreadable after the light/dark flip; the
 * token is resolved against the live CSS variables on every repaint instead.
 */
export type StyleColor = 'accent' | 'info' | 'pos' | 'neg' | 'purple' | 'warn' | 'text'

export interface DrawingStyle {
  color?: StyleColor
  width?: number
  dash?: 'solid' | 'dashed' | 'dotted'
  extend?: LineExtend
  /** Label content for `text`. */
  text?: string
}

/** One anchor: unix seconds (UTC) and a price in the symbol's own quote unit. */
export type DrawingPoint = ChartDrawingPoint

export interface Drawing {
  id: number
  symbol: string
  interval: IntervalId
  type: DrawingType
  points: DrawingPoint[]
  style: DrawingStyle
  locked: boolean
  visible: boolean
  created_at: string
  updated_at: string
}

/** A drawing before the server has seen it. */
export interface DrawingDraft {
  type: DrawingType
  points: DrawingPoint[]
  style: DrawingStyle
}

export const FIB_LEVELS = [0, 0.236, 0.382, 0.5, 0.618, 0.786, 1] as const

/** Matches maxDrawingTime in drawings.go: past this, `t` is milliseconds by mistake. */
export const MAX_DRAWING_TIME = 1e12

/** Style is a bounded blob server-side (4096 bytes); a label is the only part a user can grow. */
export const MAX_TEXT_LENGTH = 240

export const DEFAULT_COLORS: Record<DrawingType, StyleColor> = {
  trend_line: 'accent',
  horizontal_line: 'info',
  vertical_line: 'info',
  rectangle: 'purple',
  price_range: 'info',
  date_range: 'info',
  measure: 'accent',
  fib_retracement: 'warn',
  text: 'text'
}

export function defaultStyle(type: DrawingType): DrawingStyle {
  const style: DrawingStyle = { color: DEFAULT_COLORS[type], width: 1, dash: 'solid' }
  if (type === 'trend_line') style.extend = 'segment'
  if (type === 'text') style.text = 'Text'
  return style
}

// ---------- ids ----------------------------------------------------------

/**
 * Local ids are negative because the server's are a positive bigserial: the sign
 * alone says "this row does not exist yet", so an optimistic drawing can never
 * be mistaken for one the server would answer a PUT about.
 */
let localSeq = 0

export function nextLocalId(): number {
  localSeq -= 1
  return localSeq
}

export function isPersistedId(id: number): boolean {
  return id > 0
}

// ---------- pixel projection --------------------------------------------

export interface Pt {
  x: number
  y: number
}

export interface Viewport {
  width: number
  height: number
}

/** The four ChartHandle conversions, each of which may decline. */
export interface ProjectorSource {
  timeToX(t: number): number | null
  priceToY(price: number): number | null
  xToTime(x: number): number | null
  yToPrice(y: number): number | null
}

export interface Projector {
  x(t: number): number
  y(price: number): number
  t(x: number): number
  price(y: number): number
}

interface TimeCalibration {
  /** Bucket starts, ascending — index i is drawn at x0 + i * spacing. */
  times: number[]
  x0: number
  spacing: number
  /** Mean seconds per bucket, used only outside the loaded range. */
  step: number
}

function finite(v: number | null | undefined): v is number {
  return typeof v === 'number' && Number.isFinite(v)
}

/**
 * Fractional index of `t` among bucket starts.
 *
 * Buckets are not evenly spaced in time — a missing bucket is a real gap in this
 * data — but they ARE evenly spaced on screen, one bar slot each. Interpolating
 * on index rather than on seconds is what keeps a trend line touching the same
 * two candles across a zoom.
 */
function fractionalIndex(times: number[], t: number, step: number): number {
  const n = times.length
  if (n === 0) return 0
  if (t <= times[0]) return (t - times[0]) / step
  if (t >= times[n - 1]) return n - 1 + (t - times[n - 1]) / step
  let lo = 0
  let hi = n - 1
  while (hi - lo > 1) {
    const mid = (lo + hi) >> 1
    if (times[mid] <= t) lo = mid
    else hi = mid
  }
  const span = times[hi] - times[lo]
  return span > 0 ? lo + (t - times[lo]) / span : lo
}

function timeAtIndex(times: number[], index: number, step: number): number {
  const n = times.length
  if (n === 0) return 0
  if (index <= 0) return times[0] + index * step
  if (index >= n - 1) return times[n - 1] + (index - (n - 1)) * step
  const lo = Math.floor(index)
  const frac = index - lo
  return times[lo] + (times[lo + 1] - times[lo]) * frac
}

/**
 * Calibrate the bar grid from two real buckets.
 *
 * The chart's own timeToX answers only for times it has data at, so a ray drawn
 * into next week — or an anchor in a gap — would vanish. Two probes give the
 * pixels-per-bar the whole time axis is built from, and every other time is
 * derived from that. Returns null when the chart cannot place two distinct
 * buckets yet, in which case the source's own conversion is used unaided.
 */
function calibrateTime(src: ProjectorSource, times: number[]): TimeCalibration | null {
  const n = times.length
  if (n < 2) return null
  const xFirst = src.timeToX(times[0])
  const xLast = src.timeToX(times[n - 1])
  if (!finite(xFirst) || !finite(xLast) || xFirst === xLast) return null
  const spacing = (xLast - xFirst) / (n - 1)
  if (!Number.isFinite(spacing) || spacing === 0) return null
  const step = (times[n - 1] - times[0]) / (n - 1)
  return { times, x0: xFirst, spacing, step: step > 0 ? step : 1 }
}

/**
 * Build the frame's time/price projection.
 *
 * `times` are the loaded bucket starts, ascending. Prices go through the chart's
 * own scale untouched so a logarithmic axis keeps working; only the time axis is
 * reconstructed, because only the time axis refuses to answer off-data.
 */
export function createProjector(src: ProjectorSource, times: number[] = []): Projector {
  const cal = calibrateTime(src, times)
  return {
    x(t: number): number {
      if (cal) return cal.x0 + fractionalIndex(cal.times, t, cal.step) * cal.spacing
      const v = src.timeToX(t)
      return finite(v) ? v : NaN
    },
    y(price: number): number {
      const v = src.priceToY(price)
      return finite(v) ? v : NaN
    },
    t(x: number): number {
      if (cal) return timeAtIndex(cal.times, (x - cal.x0) / cal.spacing, cal.step)
      const v = src.xToTime(x)
      return finite(v) ? v : NaN
    },
    price(y: number): number {
      const v = src.yToPrice(y)
      return finite(v) ? v : NaN
    }
  }
}

export function isFinitePt(p: Pt): boolean {
  return Number.isFinite(p.x) && Number.isFinite(p.y)
}

/** Pixel position of one anchor. */
export function project(point: DrawingPoint, proj: Projector): Pt {
  return { x: proj.x(point.t), y: proj.y(point.price) }
}

/** Pixel position back to an anchor. */
export function unproject(p: Pt, proj: Projector): DrawingPoint {
  return { t: proj.t(p.x), price: proj.price(p.y) }
}

// ---------- geometry -----------------------------------------------------

/** How far a ray runs past its anchor. Canvas clips; the number just has to out-reach any viewport. */
const FAR_PX = 100_000

export function distanceToSegment(p: Pt, a: Pt, b: Pt): number {
  const dx = b.x - a.x
  const dy = b.y - a.y
  const lenSq = dx * dx + dy * dy
  if (lenSq === 0) return Math.hypot(p.x - a.x, p.y - a.y)
  let s = ((p.x - a.x) * dx + (p.y - a.y) * dy) / lenSq
  s = Math.max(0, Math.min(1, s))
  return Math.hypot(p.x - (a.x + s * dx), p.y - (a.y + s * dy))
}

/** The segment actually drawn for a two-point line, once its variant is applied. */
export function lineEndpoints(a: Pt, b: Pt, extend: LineExtend = 'segment'): [Pt, Pt] {
  if (extend === 'segment') return [a, b]
  const dx = b.x - a.x
  const dy = b.y - a.y
  const len = Math.hypot(dx, dy)
  if (len === 0) return [a, b]
  const ux = dx / len
  const uy = dy / len
  const forward = { x: a.x + ux * FAR_PX, y: a.y + uy * FAR_PX }
  if (extend === 'ray') return [a, forward]
  return [{ x: a.x - ux * FAR_PX, y: a.y - uy * FAR_PX }, forward]
}

export interface Box {
  left: number
  right: number
  top: number
  bottom: number
}

export function normalizedBox(a: Pt, b: Pt): Box {
  return {
    left: Math.min(a.x, b.x),
    right: Math.max(a.x, b.x),
    top: Math.min(a.y, b.y),
    bottom: Math.max(a.y, b.y)
  }
}

function nearBoxEdge(p: Pt, box: Box, tolerance: number): boolean {
  const withinX = p.x >= box.left - tolerance && p.x <= box.right + tolerance
  const withinY = p.y >= box.top - tolerance && p.y <= box.bottom + tolerance
  if (!withinX || !withinY) return false
  return (
    Math.abs(p.x - box.left) <= tolerance ||
    Math.abs(p.x - box.right) <= tolerance ||
    Math.abs(p.y - box.top) <= tolerance ||
    Math.abs(p.y - box.bottom) <= tolerance
  )
}

function insideBox(p: Pt, box: Box): boolean {
  return p.x >= box.left && p.x <= box.right && p.y >= box.top && p.y <= box.bottom
}

/** Half-size of the label box a `text` drawing occupies, for hit-testing without a canvas. */
const TEXT_HIT_WIDTH = 60
const TEXT_HIT_HEIGHT = 11

// ---------- hit testing --------------------------------------------------

/**
 * No viewport here on purpose: a ray is extended by a fixed reach that
 * out-runs any screen rather than clipped to one, so hit-testing needs the
 * projection and a tolerance and nothing else.
 */
export interface HitContext {
  proj: Projector
  /** Pixel slack, so a 1px line is still grabbable with a real pointer. */
  tolerance: number
}

/** Is the pointer over this drawing? Locked and hidden are the caller's business. */
export function hitTest(d: Drawing, p: Pt, ctx: HitContext): boolean {
  const { proj, tolerance } = ctx
  const pts = d.points.map((point) => project(point, proj))
  if (pts.some((pt) => !isFinitePt(pt))) return false
  const a = pts[0]
  const b = pts[1]

  switch (d.type) {
    case 'trend_line': {
      const [from, to] = lineEndpoints(a, b, d.style.extend ?? 'segment')
      return distanceToSegment(p, from, to) <= tolerance
    }
    case 'horizontal_line':
      return Math.abs(p.y - a.y) <= tolerance
    case 'vertical_line':
      return Math.abs(p.x - a.x) <= tolerance
    case 'rectangle':
    case 'price_range':
    case 'date_range':
    case 'measure': {
      const box = normalizedBox(a, b)
      return nearBoxEdge(p, box, tolerance) || insideBox(p, box)
    }
    case 'fib_retracement': {
      const left = Math.min(a.x, b.x)
      const right = Math.max(a.x, b.x)
      if (p.x < left - tolerance || p.x > right + tolerance) return false
      return fibLevelYs(a, b).some((y) => Math.abs(p.y - y) <= tolerance)
    }
    case 'text': {
      const box: Box = {
        left: a.x - tolerance,
        right: a.x + TEXT_HIT_WIDTH + tolerance,
        top: a.y - TEXT_HIT_HEIGHT - tolerance,
        bottom: a.y + TEXT_HIT_HEIGHT + tolerance
      }
      return insideBox(p, box)
    }
    default:
      // Exhaustive above; a row from a newer build simply cannot be grabbed.
      return false
  }
}

/** Screen y of every fib level between two anchors. */
export function fibLevelYs(a: Pt, b: Pt): number[] {
  return FIB_LEVELS.map((level) => a.y + (b.y - a.y) * level)
}

/**
 * One draggable anchor.
 *
 * timeIndex and priceIndex are separate because a rectangle's corner is not a
 * stored point: the top-right of a box takes its time from one anchor and its
 * price from the other. Naming the two sources independently makes all four
 * corners draggable without inventing anchors the server would reject.
 */
export interface HandleSpec {
  id: string
  x: number
  y: number
  timeIndex: number | null
  priceIndex: number | null
}

export function handlesFor(d: Drawing, proj: Projector): HandleSpec[] {
  const pts = d.points.map((point) => project(point, proj))
  if (pts.some((pt) => !isFinitePt(pt))) return []
  const a = pts[0]
  const b = pts[1]

  switch (d.type) {
    case 'horizontal_line':
    case 'vertical_line':
    case 'text':
      return [{ id: 'p0', x: a.x, y: a.y, timeIndex: 0, priceIndex: 0 }]
    case 'trend_line':
    case 'fib_retracement':
      return [
        { id: 'p0', x: a.x, y: a.y, timeIndex: 0, priceIndex: 0 },
        { id: 'p1', x: b.x, y: b.y, timeIndex: 1, priceIndex: 1 }
      ]
    case 'rectangle':
    case 'price_range':
    case 'date_range':
    case 'measure':
      return [
        { id: 'p0', x: a.x, y: a.y, timeIndex: 0, priceIndex: 0 },
        { id: 'p1', x: b.x, y: b.y, timeIndex: 1, priceIndex: 1 },
        { id: 'p0t-p1p', x: a.x, y: b.y, timeIndex: 0, priceIndex: 1 },
        { id: 'p1t-p0p', x: b.x, y: a.y, timeIndex: 1, priceIndex: 0 }
      ]
    default:
      return []
  }
}

export function hitHandle(d: Drawing, p: Pt, ctx: HitContext): HandleSpec | null {
  let best: HandleSpec | null = null
  let bestDist = Infinity
  for (const h of handlesFor(d, ctx.proj)) {
    const dist = Math.hypot(p.x - h.x, p.y - h.y)
    if (dist <= ctx.tolerance + 3 && dist < bestDist) {
      best = h
      bestDist = dist
    }
  }
  return best
}

// ---------- editing ------------------------------------------------------

/** Every anchor shifted by the same data-space delta — the whole-drawing drag. */
export function moveDrawing(d: Drawing, dt: number, dPrice: number): Drawing {
  return {
    ...d,
    points: d.points.map((p) => ({ t: p.t + dt, price: p.price + dPrice }))
  }
}

/** One handle dragged to a new anchor, leaving the coordinates it does not own alone. */
export function moveHandle(d: Drawing, handle: HandleSpec, to: DrawingPoint): Drawing {
  const points = d.points.map((p) => ({ ...p }))
  if (handle.timeIndex !== null && points[handle.timeIndex]) {
    points[handle.timeIndex].t = to.t
  }
  if (handle.priceIndex !== null && points[handle.priceIndex]) {
    points[handle.priceIndex].price = to.price
  }
  return { ...d, points }
}

export function offsetDrawing(d: Drawing, dt: number, dPrice = 0): Drawing {
  return moveDrawing(d, dt, dPrice)
}

// ---------- snapping -----------------------------------------------------

export type SnapMode = 'off' | 'weak' | 'strong'

export const SNAP_MODES: SnapMode[] = ['off', 'weak', 'strong']

export const SNAP_LABELS: Record<SnapMode, string> = {
  off: 'Snap off',
  weak: 'Snap weak',
  strong: 'Snap strong'
}

/** Weak is the default: it helps on the bar you meant and stays out of the way elsewhere. */
export const DEFAULT_SNAP_MODE: SnapMode = 'weak'

/** How near, in pixels, 'weak' has to be before it grabs an OHLC value. */
export const SNAP_TOLERANCE_PX = 8

/** Index of the bucket whose start is nearest `t`, or -1 when there are none. */
export function nearestCandleIndex(candles: ChartCandle[], t: number): number {
  const n = candles.length
  if (n === 0) return -1
  let lo = 0
  let hi = n - 1
  while (lo < hi) {
    const mid = (lo + hi) >> 1
    if (candles[mid].t < t) lo = mid + 1
    else hi = mid
  }
  if (lo > 0 && Math.abs(candles[lo - 1].t - t) <= Math.abs(candles[lo].t - t)) return lo - 1
  return lo
}

export interface SnapContext {
  candles: ChartCandle[]
  mode: SnapMode
  proj: Projector
  tolerancePx?: number
}

/**
 * Pull a raw anchor onto the nearest candle's open/high/low/close.
 *
 * 'strong' always takes the bar; 'weak' takes it only when the pointer is
 * already within a few pixels, so a deliberately free-hand line stays free-hand.
 * Time and price snap independently — grabbing the bar's start while leaving the
 * price alone is the common case near a wick.
 */
export function snapPoint(raw: DrawingPoint, ctx: SnapContext): DrawingPoint {
  const { candles, mode, proj } = ctx
  if (mode === 'off' || candles.length === 0) return raw
  if (!Number.isFinite(raw.t) || !Number.isFinite(raw.price)) return raw

  const index = nearestCandleIndex(candles, raw.t)
  if (index < 0) return raw
  const candle = candles[index]
  const tolerance = ctx.tolerancePx ?? SNAP_TOLERANCE_PX

  let t = raw.t
  if (mode === 'strong') {
    t = candle.t
  } else {
    const dx = Math.abs(proj.x(candle.t) - proj.x(raw.t))
    if (Number.isFinite(dx) && dx <= tolerance) t = candle.t
  }

  let price = raw.price
  const candidates = [candle.open, candle.high, candle.low, candle.close]
  let best = candidates[0]
  let bestDelta = Infinity
  for (const c of candidates) {
    const delta = Math.abs(c - raw.price)
    if (delta < bestDelta) {
      best = c
      bestDelta = delta
    }
  }
  if (mode === 'strong') {
    price = best
  } else {
    const dy = Math.abs(proj.y(best) - proj.y(raw.price))
    if (Number.isFinite(dy) && dy <= tolerance) price = best
  }

  return { t, price }
}

// ---------- validation ---------------------------------------------------

function isDrawingType(value: unknown): value is DrawingType {
  return typeof value === 'string' && value in DRAWING_POINT_COUNTS
}

function validPoint(p: unknown): p is DrawingPoint {
  if (!p || typeof p !== 'object') return false
  const { t, price } = p as { t?: unknown; price?: unknown }
  if (typeof t !== 'number' || !Number.isFinite(t) || Math.abs(t) > MAX_DRAWING_TIME) return false
  return typeof price === 'number' && Number.isFinite(price)
}

/**
 * Anchors the server would accept, and therefore anchors the canvas can trust.
 *
 * The count is exact and the coordinates must be finite: a NaN anchor poisons
 * every pixel derived from it, and one drawing with a NaN is enough to blank a
 * frame. Refusing it here is how a bad row stays a missing drawing rather than
 * an empty chart.
 */
export function validPoints(type: DrawingType, points: unknown): points is DrawingPoint[] {
  if (!Array.isArray(points)) return false
  if (points.length !== DRAWING_POINT_COUNTS[type]) return false
  return points.every(validPoint)
}

export function isValidDraft(draft: DrawingDraft): boolean {
  return isDrawingType(draft.type) && validPoints(draft.type, draft.points)
}

function parseStyle(raw: unknown): DrawingStyle {
  const style: DrawingStyle = {}
  if (!raw || typeof raw !== 'object') return style
  const src = raw as Record<string, unknown>
  const colors: StyleColor[] = ['accent', 'info', 'pos', 'neg', 'purple', 'warn', 'text']
  if (typeof src.color === 'string' && (colors as string[]).includes(src.color)) {
    style.color = src.color as StyleColor
  }
  if (typeof src.width === 'number' && Number.isFinite(src.width)) {
    style.width = Math.max(1, Math.min(6, Math.round(src.width)))
  }
  if (src.dash === 'solid' || src.dash === 'dashed' || src.dash === 'dotted') {
    style.dash = src.dash
  }
  if (src.extend === 'segment' || src.extend === 'ray' || src.extend === 'extended') {
    style.extend = src.extend
  }
  if (typeof src.text === 'string') style.text = src.text.slice(0, MAX_TEXT_LENGTH)
  return style
}

/**
 * One API row into a drawing, or null.
 *
 * Null is the point: rows are user-authored JSONB the server stores without
 * interpreting, and a type this build does not know — or an anchor count that
 * disagrees with it — must not reach the canvas.
 */
export function parseDrawing(raw: ChartDrawing | null | undefined): Drawing | null {
  if (!raw || typeof raw !== 'object') return null
  if (typeof raw.id !== 'number' || !Number.isFinite(raw.id)) return null
  if (!isDrawingType(raw.drawing_type)) return null
  const interval = parseInterval(raw.interval)
  if (!interval) return null
  if (!validPoints(raw.drawing_type, raw.points)) return null
  return {
    id: raw.id,
    symbol: String(raw.symbol ?? ''),
    interval,
    type: raw.drawing_type,
    points: raw.points.map((p) => ({ t: p.t, price: p.price })),
    style: parseStyle(raw.style),
    locked: raw.locked === true,
    visible: raw.visible !== false,
    created_at: typeof raw.created_at === 'string' ? raw.created_at : '',
    updated_at: typeof raw.updated_at === 'string' ? raw.updated_at : ''
  }
}

export function parseDrawings(rows: ChartDrawing[] | null | undefined): Drawing[] {
  if (!Array.isArray(rows)) return []
  const out: Drawing[] = []
  for (const row of rows) {
    const parsed = parseDrawing(row)
    if (parsed) out.push(parsed)
  }
  return out
}

export interface DrawingRequestBody {
  symbol: string
  interval: string
  drawing_type: DrawingType
  points: DrawingPoint[]
  style: DrawingStyle
  locked: boolean
  visible: boolean
}

/**
 * The POST/PUT body. Times are whole seconds because the column is: sending a
 * fractional `t` means the row that comes back differs from the row that was
 * sent, and the next PUT would then look like an edit nobody made.
 */
export function toRequestBody(d: Drawing): DrawingRequestBody {
  return {
    symbol: d.symbol,
    interval: d.interval,
    drawing_type: d.type,
    points: d.points.map((p) => ({ t: Math.round(p.t), price: p.price })),
    style: d.style,
    locked: d.locked,
    visible: d.visible
  }
}

// ---------- history ------------------------------------------------------

export interface DrawingHistory {
  past: Drawing[][]
  present: Drawing[]
  future: Drawing[][]
}

/** Deep enough to cover a session's drawing; bounded so a long one cannot grow without limit. */
export const HISTORY_LIMIT = 50

export function emptyHistory(present: Drawing[] = []): DrawingHistory {
  return { past: [], present, future: [] }
}

/**
 * Record a new state. Push once per gesture, not per frame: a drag that pushed
 * on every pointermove would need fifty undos to walk back one line.
 */
export function pushHistory(h: DrawingHistory, next: Drawing[]): DrawingHistory {
  if (sameDrawingSet(h.present, next)) return h
  const past = h.past.concat([h.present])
  return {
    past: past.length > HISTORY_LIMIT ? past.slice(past.length - HISTORY_LIMIT) : past,
    present: next,
    future: []
  }
}

export function canUndo(h: DrawingHistory): boolean {
  return h.past.length > 0
}

export function canRedo(h: DrawingHistory): boolean {
  return h.future.length > 0
}

export function undoHistory(h: DrawingHistory): DrawingHistory {
  if (h.past.length === 0) return h
  return {
    past: h.past.slice(0, -1),
    present: h.past[h.past.length - 1],
    future: [h.present].concat(h.future)
  }
}

export function redoHistory(h: DrawingHistory): DrawingHistory {
  if (h.future.length === 0) return h
  return {
    past: h.past.concat([h.present]),
    present: h.future[0],
    future: h.future.slice(1)
  }
}

/**
 * Rewrite an id everywhere it is remembered.
 *
 * Undoing a create deletes the row; redoing it has to INSERT again, and the
 * server hands back a new bigserial. Without this, every history entry still
 * naming the old id would issue PUTs against a row that no longer exists.
 */
export function remapHistoryId(h: DrawingHistory, from: number, to: number): DrawingHistory {
  const swap = (list: Drawing[]) =>
    list.map((d) => (d.id === from ? { ...d, id: to } : d))
  return {
    past: h.past.map(swap),
    present: swap(h.present),
    future: h.future.map(swap)
  }
}

// ---------- diffing ------------------------------------------------------

export function drawingsEqual(a: Drawing, b: Drawing): boolean {
  if (a.id !== b.id || a.type !== b.type) return false
  if (a.locked !== b.locked || a.visible !== b.visible) return false
  if (a.points.length !== b.points.length) return false
  for (let i = 0; i < a.points.length; i++) {
    if (a.points[i].t !== b.points[i].t || a.points[i].price !== b.points[i].price) return false
  }
  return JSON.stringify(a.style) === JSON.stringify(b.style)
}

export function sameDrawingSet(a: Drawing[], b: Drawing[]): boolean {
  if (a.length !== b.length) return false
  return a.every((d, i) => drawingsEqual(d, b[i]))
}

export interface DrawingDiff {
  created: Drawing[]
  updated: Drawing[]
  deleted: Drawing[]
}

/** What has to happen on the server to turn `from` into `to`. */
export function diffDrawings(from: Drawing[], to: Drawing[]): DrawingDiff {
  const before = new Map(from.map((d) => [d.id, d]))
  const after = new Map(to.map((d) => [d.id, d]))
  const diff: DrawingDiff = { created: [], updated: [], deleted: [] }
  for (const d of to) {
    const prev = before.get(d.id)
    if (!prev) diff.created.push(d)
    else if (!drawingsEqual(prev, d)) diff.updated.push(d)
  }
  for (const d of from) {
    if (!after.has(d.id)) diff.deleted.push(d)
  }
  return diff
}

// ---------- readouts -----------------------------------------------------

const MINUTE = 60
const HOUR = 3_600
const DAY = 86_400

/** Elapsed time a trader reads: "3d 4h", "45m". Never a bucket size — that is humanSeconds(). */
export function formatDuration(seconds: number): string {
  const total = Math.round(Math.abs(seconds))
  if (total < MINUTE) return `${total}s`
  if (total < HOUR) return `${Math.round(total / MINUTE)}m`
  if (total < DAY) {
    const h = Math.floor(total / HOUR)
    const m = Math.round((total % HOUR) / MINUTE)
    return m > 0 ? `${h}h ${m}m` : `${h}h`
  }
  const d = Math.floor(total / DAY)
  const h = Math.round((total % DAY) / HOUR)
  return h > 0 ? `${d}d ${h}h` : `${d}d`
}

/**
 * Buckets actually loaded between two anchors, inclusive.
 *
 * Counted from the loaded times rather than divided out of the interval: this
 * data has real gaps, and dividing would report bars that were never collected.
 */
export function countBarsBetween(times: number[], a: number, b: number): number {
  if (times.length === 0) return 0
  const lo = Math.min(a, b)
  const hi = Math.max(a, b)
  // Two binary searches rather than a scan: `times` is the whole loaded history
  // and this runs for every measurement on every repaint.
  const first = lowerBound(times, (t) => t >= lo)
  const past = lowerBound(times, (t) => t > hi)
  return Math.max(0, past - first)
}

/** First index whose value satisfies `pred`; `times.length` when none does. */
function lowerBound(times: number[], pred: (t: number) => boolean): number {
  let lo = 0
  let hi = times.length
  while (lo < hi) {
    const mid = (lo + hi) >> 1
    if (pred(times[mid])) hi = mid
    else lo = mid + 1
  }
  return lo
}

export interface RangeReadout {
  priceDelta: number
  pctDelta: number | null
  seconds: number
  bars: number
}

export function rangeReadout(d: Drawing, times: number[]): RangeReadout {
  const a = d.points[0]
  const b = d.points[d.points.length - 1]
  const priceDelta = b.price - a.price
  const pctDelta = a.price !== 0 ? (priceDelta / Math.abs(a.price)) * 100 : null
  return {
    priceDelta,
    pctDelta,
    seconds: b.t - a.t,
    bars: countBarsBetween(times, a.t, b.t)
  }
}
