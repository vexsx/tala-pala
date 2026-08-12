import { formatDateTime, formatPct, type CalendarMode, type DisplayUnit } from '../../lib/format'
import { formatChartPrice } from '../OhlcHeader'
import {
  FIB_LEVELS,
  fibLevelYs,
  formatDuration,
  handlesFor,
  isFinitePt,
  lineEndpoints,
  normalizedBox,
  project,
  rangeReadout,
  type Drawing,
  type DrawingStyle,
  type Projector,
  type Pt,
  type StyleColor,
  type Viewport
} from './model'

/**
 * Painting, and nothing else. Every function here reads a drawing's data-space
 * anchors, projects them for this one frame, and draws; none of them mutate a
 * drawing or remember a pixel.
 *
 * Colours come from the live CSS variables on every repaint rather than from
 * the stored style, so a drawing made in the dark theme stays readable after the
 * flip to light. That is also why DrawingStyle stores a palette token.
 */

export interface DrawingPalette {
  accent: string
  info: string
  pos: string
  neg: string
  purple: string
  warn: string
  text: string
  muted: string
  border: string
  panel: string
  mono: string
}

/** Resolve the theme's variables to concrete colours — mirrors chartColors() in TradingChart. */
export function resolveDrawingPalette(): DrawingPalette {
  const css = getComputedStyle(document.documentElement)
  const v = (name: string, fallback: string) => css.getPropertyValue(name).trim() || fallback
  return {
    accent: v('--accent', '#d4a017'),
    info: v('--info', '#58a6ff'),
    pos: v('--pos', '#2ea36b'),
    neg: v('--neg', '#e5534b'),
    purple: v('--purple', '#a371f7'),
    warn: v('--warn', '#d29922'),
    text: v('--text', '#e6edf3'),
    muted: v('--muted', '#8b98a5'),
    border: v('--border', '#22303f'),
    panel: v('--panel', '#131b24'),
    mono: v('--mono', 'ui-monospace, SFMono-Regular, Menlo, monospace')
  }
}

export interface PaintOptions {
  ctx: CanvasRenderingContext2D
  proj: Projector
  view: Viewport
  palette: DrawingPalette
  symbol: string
  unit: DisplayUnit
  calendar: CalendarMode
  /** Loaded bucket starts — the honest source for a bar count. */
  times: number[]
  selected: boolean
  hovered: boolean
}

const LABEL_FONT_PX = 11
const HANDLE_RADIUS = 4
const FILL_ALPHA = 0.08
const BAND_ALPHA = 0.06

function colorOf(style: DrawingStyle, palette: DrawingPalette, fallback: StyleColor): string {
  const token = style.color ?? fallback
  return palette[token] ?? palette.accent
}

function applyStroke(o: PaintOptions, color: string, style: DrawingStyle): void {
  const { ctx } = o
  ctx.strokeStyle = color
  // Selection thickens the line as well as adding handles: a colour shift alone
  // would be the only cue for anyone who cannot see the shift.
  const width = (style.width ?? 1) + (o.selected ? 1 : 0) + (o.hovered && !o.selected ? 0.5 : 0)
  ctx.lineWidth = width
  if (style.dash === 'dashed') ctx.setLineDash([6, 4])
  else if (style.dash === 'dotted') ctx.setLineDash([2, 3])
  else ctx.setLineDash([])
}

function strokeLine(ctx: CanvasRenderingContext2D, a: Pt, b: Pt): void {
  ctx.beginPath()
  ctx.moveTo(a.x, a.y)
  ctx.lineTo(b.x, b.y)
  ctx.stroke()
}

/** A small filled arrowhead at `to`, pointing away from `from`. */
function arrowHead(ctx: CanvasRenderingContext2D, from: Pt, to: Pt, color: string): void {
  const angle = Math.atan2(to.y - from.y, to.x - from.x)
  const size = 6
  ctx.save()
  ctx.setLineDash([])
  ctx.fillStyle = color
  ctx.beginPath()
  ctx.moveTo(to.x, to.y)
  ctx.lineTo(to.x - size * Math.cos(angle - Math.PI / 6), to.y - size * Math.sin(angle - Math.PI / 6))
  ctx.lineTo(to.x - size * Math.cos(angle + Math.PI / 6), to.y - size * Math.sin(angle + Math.PI / 6))
  ctx.closePath()
  ctx.fill()
  ctx.restore()
}

/**
 * A readout chip. Drawn on the panel colour with a border so the numbers stay
 * legible over candles, grid lines and other drawings in either theme.
 */
function drawLabel(o: PaintOptions, x: number, y: number, lines: string[], accent: string): void {
  const { ctx, palette } = o
  if (lines.length === 0) return
  ctx.save()
  ctx.setLineDash([])
  ctx.font = `${LABEL_FONT_PX}px ${palette.mono}`
  ctx.textBaseline = 'top'
  const padding = 4
  const lineHeight = LABEL_FONT_PX + 3
  let width = 0
  for (const line of lines) width = Math.max(width, ctx.measureText(line).width)
  const boxW = width + padding * 2
  const boxH = lines.length * lineHeight + padding * 2 - 3

  // Keep the chip on screen: a measurement whose numbers sit past the edge is
  // exactly the measurement the user zoomed in to read.
  const left = Math.max(2, Math.min(x, o.view.width - boxW - 2))
  const top = Math.max(2, Math.min(y, o.view.height - boxH - 2))

  ctx.fillStyle = palette.panel
  ctx.globalAlpha = 0.92
  ctx.fillRect(left, top, boxW, boxH)
  ctx.globalAlpha = 1
  ctx.strokeStyle = accent
  ctx.lineWidth = 1
  ctx.strokeRect(left + 0.5, top + 0.5, boxW - 1, boxH - 1)
  ctx.fillStyle = palette.text
  lines.forEach((line, i) => {
    ctx.fillText(line, left + padding, top + padding + i * lineHeight)
  })
  ctx.restore()
}

function fillRegion(o: PaintOptions, color: string, draw: () => void, alpha = FILL_ALPHA): void {
  const { ctx } = o
  ctx.save()
  ctx.setLineDash([])
  ctx.globalAlpha = alpha
  ctx.fillStyle = color
  draw()
  ctx.restore()
}

/** Anchor handles for the selected drawing. Hollow when locked — the shape says "not draggable". */
function paintHandles(d: Drawing, o: PaintOptions, color: string): void {
  const { ctx } = o
  ctx.save()
  ctx.setLineDash([])
  ctx.lineWidth = 1.5
  for (const h of handlesFor(d, o.proj)) {
    if (!Number.isFinite(h.x) || !Number.isFinite(h.y)) continue
    ctx.beginPath()
    ctx.arc(h.x, h.y, HANDLE_RADIUS, 0, Math.PI * 2)
    if (d.locked) {
      ctx.strokeStyle = o.palette.muted
      ctx.stroke()
    } else {
      ctx.fillStyle = o.palette.panel
      ctx.fill()
      ctx.strokeStyle = color
      ctx.stroke()
    }
  }
  ctx.restore()
}

function priceText(value: number, o: PaintOptions): string {
  return formatChartPrice(value, o.symbol, o.unit)
}

/** Paint one drawing, plus its handles when it is the selection. */
export function paintDrawing(d: Drawing, o: PaintOptions): void {
  const pts = d.points.map((p) => project(p, o.proj))
  if (pts.length === 0 || pts.some((p) => !isFinitePt(p))) return

  const color = colorOf(d.style, o.palette, 'accent')
  const { ctx } = o
  ctx.save()
  applyStroke(o, color, d.style)

  switch (d.type) {
    case 'trend_line':
      paintTrendLine(pts[0], pts[1], d.style, o)
      break
    case 'horizontal_line':
      paintHorizontalLine(d, pts[0], o, color)
      break
    case 'vertical_line':
      paintVerticalLine(d, pts[0], o, color)
      break
    case 'rectangle':
      paintRectangle(pts[0], pts[1], o, color)
      break
    case 'price_range':
      paintPriceRange(d, pts[0], pts[1], o, color)
      break
    case 'date_range':
      paintDateRange(d, pts[0], pts[1], o, color)
      break
    case 'measure':
      paintMeasure(d, pts[0], pts[1], o, color)
      break
    case 'fib_retracement':
      paintFib(d, pts[0], pts[1], o, color)
      break
    case 'text':
      paintText(d, pts[0], o, color)
      break
  }

  ctx.restore()
  if (o.selected) paintHandles(d, o, color)
}

// The stroke colour is already on the context from applyStroke; only the types
// that draw a label or a fill need the colour itself.
function paintTrendLine(a: Pt, b: Pt, style: DrawingStyle, o: PaintOptions): void {
  const [from, to] = lineEndpoints(a, b, style.extend ?? 'segment')
  strokeLine(o.ctx, from, to)
}

function paintHorizontalLine(d: Drawing, a: Pt, o: PaintOptions, color: string): void {
  strokeLine(o.ctx, { x: 0, y: a.y }, { x: o.view.width, y: a.y })
  drawLabel(o, o.view.width - 90, a.y - LABEL_FONT_PX - 6, [priceText(d.points[0].price, o)], color)
}

function paintVerticalLine(d: Drawing, a: Pt, o: PaintOptions, color: string): void {
  strokeLine(o.ctx, { x: a.x, y: 0 }, { x: a.x, y: o.view.height })
  const when = formatDateTime(new Date(d.points[0].t * 1000), o.calendar)
  drawLabel(o, a.x + 4, 4, [when], color)
}

function paintRectangle(a: Pt, b: Pt, o: PaintOptions, color: string): void {
  const box = normalizedBox(a, b)
  const w = box.right - box.left
  const h = box.bottom - box.top
  fillRegion(o, color, () => o.ctx.fillRect(box.left, box.top, w, h))
  o.ctx.strokeRect(box.left, box.top, w, h)
}

/**
 * Absolute move and percent move between the two anchor prices.
 *
 * The percent is a price change, not a claim about anything: the sign is spelled
 * out in the number so the direction never rests on the colour alone.
 */
function paintPriceRange(d: Drawing, a: Pt, b: Pt, o: PaintOptions, color: string): void {
  const box = normalizedBox(a, b)
  const readout = rangeReadout(d, o.times)
  const tone = readout.priceDelta >= 0 ? o.palette.pos : o.palette.neg
  fillRegion(o, tone, () =>
    o.ctx.fillRect(box.left, box.top, box.right - box.left, box.bottom - box.top)
  )
  o.ctx.strokeRect(box.left, box.top, box.right - box.left, box.bottom - box.top)

  const midX = (box.left + box.right) / 2
  strokeLine(o.ctx, { x: midX, y: a.y }, { x: midX, y: b.y })
  arrowHead(o.ctx, { x: midX, y: a.y }, { x: midX, y: b.y }, tone)

  const sign = readout.priceDelta >= 0 ? '+' : '−'
  drawLabel(
    o,
    midX + 8,
    Math.min(a.y, b.y) + (Math.abs(b.y - a.y) - LABEL_FONT_PX) / 2,
    [
      `${sign}${priceText(Math.abs(readout.priceDelta), o)}`,
      formatPct(readout.pctDelta)
    ],
    color
  )
}

/** Elapsed time and how many loaded buckets the span actually covers. */
function paintDateRange(d: Drawing, a: Pt, b: Pt, o: PaintOptions, color: string): void {
  const box = normalizedBox(a, b)
  const readout = rangeReadout(d, o.times)
  fillRegion(o, color, () =>
    o.ctx.fillRect(box.left, box.top, box.right - box.left, box.bottom - box.top)
  )
  o.ctx.strokeRect(box.left, box.top, box.right - box.left, box.bottom - box.top)

  const midY = (box.top + box.bottom) / 2
  strokeLine(o.ctx, { x: a.x, y: midY }, { x: b.x, y: midY })
  arrowHead(o.ctx, { x: a.x, y: midY }, { x: b.x, y: midY }, color)

  drawLabel(
    o,
    (box.left + box.right) / 2 - 30,
    midY + 8,
    [formatDuration(readout.seconds), `${readout.bars} bars`],
    color
  )
}

/** Everything price_range and date_range say, about one drag. */
function paintMeasure(d: Drawing, a: Pt, b: Pt, o: PaintOptions, color: string): void {
  const box = normalizedBox(a, b)
  const readout = rangeReadout(d, o.times)
  const tone = readout.priceDelta >= 0 ? o.palette.pos : o.palette.neg
  fillRegion(o, tone, () =>
    o.ctx.fillRect(box.left, box.top, box.right - box.left, box.bottom - box.top)
  )
  o.ctx.strokeRect(box.left, box.top, box.right - box.left, box.bottom - box.top)
  strokeLine(o.ctx, a, b)
  arrowHead(o.ctx, a, b, tone)

  const sign = readout.priceDelta >= 0 ? '+' : '−'
  drawLabel(
    o,
    b.x + 8,
    b.y - LABEL_FONT_PX * 2,
    [
      `${sign}${priceText(Math.abs(readout.priceDelta), o)}  ${formatPct(readout.pctDelta)}`,
      `${formatDuration(readout.seconds)}  ·  ${readout.bars} bars`
    ],
    color
  )
}

/**
 * The seven retracement levels between two anchors.
 *
 * Geometry only. A level is a horizontal line at a fraction of the anchor span
 * and its label is the price it sits at — the engine derives no signal from a
 * fib level and displays no suggestion next to one.
 */
function paintFib(d: Drawing, a: Pt, b: Pt, o: PaintOptions, color: string): void {
  const left = Math.min(a.x, b.x)
  const right = Math.max(a.x, b.x)
  const ys = fibLevelYs(a, b)
  const priceA = d.points[0].price
  const priceB = d.points[1].price

  for (let i = 0; i < ys.length - 1; i++) {
    fillRegion(
      o,
      color,
      () => o.ctx.fillRect(left, Math.min(ys[i], ys[i + 1]), right - left, Math.abs(ys[i + 1] - ys[i])),
      i % 2 === 0 ? BAND_ALPHA : BAND_ALPHA / 2
    )
  }

  FIB_LEVELS.forEach((level, i) => {
    const y = ys[i]
    strokeLine(o.ctx, { x: left, y }, { x: right, y })
    const price = priceA + (priceB - priceA) * level
    drawLabel(o, right + 6, y - LABEL_FONT_PX, [`${level}  ${priceText(price, o)}`], color)
  })
}

function paintText(d: Drawing, a: Pt, o: PaintOptions, color: string): void {
  const label = d.style.text?.trim()
  drawLabel(o, a.x, a.y - LABEL_FONT_PX - 4, [label && label.length > 0 ? label : 'Text'], color)
}

/**
 * The drawing being dragged out right now.
 *
 * It is painted from the same functions as a stored one — a preview that looked
 * different from the result is a preview that lied — but always dashed and
 * unselected, so "not committed yet" is visible without relying on colour.
 */
export function paintDraft(d: Drawing, o: PaintOptions): void {
  paintDrawing({ ...d, style: { ...d.style, dash: 'dashed' } }, { ...o, selected: false })
}
