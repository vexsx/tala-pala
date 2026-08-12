import { act, renderHook, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { ChartCandle, ChartDrawing } from '../api/types'

/**
 * A fake drawings API with real semantics: it assigns ids the way a bigserial
 * does, refuses to move a drawing between charts the way the UPDATE predicate
 * does, and keys its rows by (symbol, interval) the way the index does. Testing
 * the store against a permissive stub would prove nothing about the two things
 * most likely to break — id remapping and chart scoping.
 */
const srv = vi.hoisted(() => {
  class ApiError extends Error {
    status: number
    code: string
    constructor(status: number, message: string) {
      super(message)
      this.name = 'ApiError'
      this.status = status
      this.code = 'test_error'
    }
  }

  interface Row {
    id: number
    symbol: string
    interval: string
    drawing_type: string
    points: Array<{ t: number; price: number }>
    style: Record<string, unknown>
    locked: boolean
    visible: boolean
    created_at: string
    updated_at: string
  }

  const state = {
    rows: [] as Row[],
    nextId: 100,
    calls: [] as Array<{ method: string; path: string; body?: unknown }>,
    /** Paths matching this substring fail, so rollback can be exercised. */
    failOn: null as string | null,
    failStatus: 500,
    truncated: false,
    reset() {
      state.rows = []
      state.nextId = 100
      state.calls = []
      state.failOn = null
      state.failStatus = 500
      state.truncated = false
    }
  }

  const api = async (path: string, opts?: { method?: string; body?: unknown }) => {
    const method = opts?.method ?? 'GET'
    state.calls.push({ method, path, body: opts?.body })
    if (state.failOn && path.includes(state.failOn)) {
      throw new ApiError(state.failStatus, 'server said no')
    }
    if (method === 'GET') {
      const url = new URL(`http://x${path}`)
      const symbol = url.searchParams.get('symbol')
      const interval = url.searchParams.get('interval')
      const items = state.rows.filter((r) => r.symbol === symbol && r.interval === interval)
      return { items, count: items.length, limit: 250, truncated: state.truncated }
    }
    if (method === 'POST') {
      const body = opts?.body as Record<string, unknown>
      const now = new Date().toISOString()
      const row: Row = {
        id: state.nextId++,
        symbol: body.symbol as string,
        interval: body.interval as string,
        drawing_type: body.drawing_type as string,
        points: body.points as Array<{ t: number; price: number }>,
        style: (body.style ?? {}) as Record<string, unknown>,
        locked: body.locked === true,
        visible: body.visible !== false,
        created_at: now,
        updated_at: now
      }
      state.rows.push(row)
      return row
    }
    if (method === 'PUT') {
      const id = Number(path.split('/').pop())
      const body = opts?.body as Record<string, unknown>
      const row = state.rows.find((r) => r.id === id)
      if (!row) throw new ApiError(404, 'drawing not found')
      // The server refuses to move a drawing to another chart.
      if (row.symbol !== body.symbol || row.interval !== body.interval) {
        throw new ApiError(400, 'a drawing cannot be moved to another chart')
      }
      row.drawing_type = body.drawing_type as string
      row.points = body.points as Array<{ t: number; price: number }>
      row.style = (body.style ?? {}) as Record<string, unknown>
      row.locked = body.locked === true
      row.visible = body.visible !== false
      row.updated_at = new Date().toISOString()
      return row
    }
    if (method === 'DELETE') {
      const id = Number(path.split('/').pop())
      const i = state.rows.findIndex((r) => r.id === id)
      if (i < 0) throw new ApiError(404, 'drawing not found')
      state.rows.splice(i, 1)
      return null
    }
    throw new ApiError(405, `unexpected ${method}`)
  }

  return { state, api, ApiError }
})

vi.mock('../api/client', () => ({
  api: srv.api,
  ApiError: srv.ApiError,
  errorMessage: (err: unknown) => (err instanceof Error ? err.message : 'Unexpected error')
}))

import {
  countBarsBetween,
  createProjector,
  diffDrawings,
  distanceToSegment,
  emptyHistory,
  fibLevelYs,
  formatDuration,
  handlesFor,
  hitTest,
  lineEndpoints,
  moveDrawing,
  moveHandle,
  parseDrawing,
  parseDrawings,
  pushHistory,
  redoHistory,
  remapHistoryId,
  snapPoint,
  undoHistory,
  isValidDraft,
  rangeReadout,
  toRequestBody,
  FIB_LEVELS,
  HISTORY_LIMIT,
  type Drawing,
  type DrawingType,
  type Projector
} from '../chart/drawings/model'
import { useDrawings } from '../chart/drawings/useDrawings'

const DAY = 86_400
const T0 = Date.parse('2026-08-01T00:00:00Z') / 1000
const TIMES = Array.from({ length: 10 }, (_, i) => T0 + i * DAY)

function candle(i: number, close: number): ChartCandle {
  return {
    t: T0 + i * DAY,
    open: close - 20_000,
    high: close + 30_000,
    low: close - 40_000,
    close,
    ticks: 12,
    confirmed: true,
    synthetic: false
  }
}

const CANDLES = TIMES.map((_, i) => candle(i, 8_000_000 + i * 10_000))

/**
 * A linear stand-in for the chart's scales. `barPx` is the zoom: the whole point
 * of the projector is that changing it moves pixels and nothing else.
 */
function source(barPx = 10) {
  return {
    timeToX: (t: number) => ((t - T0) / DAY) * barPx,
    priceToY: (p: number) => 300 - (p - 8_000_000) / 1_000,
    xToTime: (x: number) => T0 + (x / barPx) * DAY,
    yToPrice: (y: number) => 8_000_000 + (300 - y) * 1_000
  }
}

function proj(barPx = 10): Projector {
  return createProjector(source(barPx), TIMES)
}

function drawing(type: DrawingType, points: Array<[number, number]>, over: Partial<Drawing> = {}): Drawing {
  return {
    id: 1,
    symbol: 'IR_GOLD_18K',
    interval: '1d',
    type,
    points: points.map(([t, price]) => ({ t, price })),
    style: {},
    locked: false,
    visible: true,
    created_at: '',
    updated_at: '',
    ...over
  }
}

const ctx = (p: Projector, tolerance = 6) => ({ proj: p, tolerance })

describe('drawing geometry and projection', () => {
  it('anchors in data space: a zoom moves pixels but never a point', () => {
    // Anchored to the 3rd and 7th candle.
    const line = drawing('trend_line', [
      [TIMES[3], CANDLES[3].close],
      [TIMES[7], CANDLES[7].close]
    ])
    const before = JSON.stringify(line.points)

    const near = proj(10)
    const far = proj(37)

    // The pixels genuinely change — otherwise this test proves nothing.
    expect(near.x(TIMES[3])).not.toBeCloseTo(far.x(TIMES[3]))

    // …yet at both zooms the drawing sits exactly on the candles it names.
    for (const p of [near, far]) {
      expect(p.x(line.points[0].t)).toBeCloseTo(p.x(CANDLES[3].t))
      expect(p.x(line.points[1].t)).toBeCloseTo(p.x(CANDLES[7].t))
      expect(p.y(line.points[0].price)).toBeCloseTo(p.y(CANDLES[3].close))
    }
    expect(JSON.stringify(line.points)).toBe(before)
  })

  it('places a time between two buckets on the bar grid, not on the clock', () => {
    const p = proj(10)
    expect(p.x(TIMES[0])).toBeCloseTo(0)
    expect(p.x(TIMES[5])).toBeCloseTo(50)
    expect(p.x(TIMES[5] + DAY / 2)).toBeCloseTo(55)
    // Past the loaded history the grid keeps its spacing, so a ray drawn into
    // next week still lands somewhere instead of vanishing.
    expect(p.x(TIMES[9] + 2 * DAY)).toBeCloseTo(110)
    expect(p.t(50)).toBeCloseTo(TIMES[5])
  })

  it('falls back to the chart when there are too few buckets to calibrate', () => {
    const p = createProjector(source(10), [])
    expect(p.x(TIMES[4])).toBeCloseTo(40)
  })

  it('measures distance to a segment, including past its ends', () => {
    expect(distanceToSegment({ x: 5, y: 3 }, { x: 0, y: 0 }, { x: 10, y: 0 })).toBeCloseTo(3)
    expect(distanceToSegment({ x: 20, y: 0 }, { x: 0, y: 0 }, { x: 10, y: 0 })).toBeCloseTo(10)
  })

  it('extends ray and extended variants past their anchors', () => {
    const a = { x: 0, y: 0 }
    const b = { x: 10, y: 0 }
    expect(lineEndpoints(a, b, 'segment')).toEqual([a, b])
    const [rayFrom, rayTo] = lineEndpoints(a, b, 'ray')
    expect(rayFrom).toEqual(a)
    expect(rayTo.x).toBeGreaterThan(1_000)
    const [extFrom, extTo] = lineEndpoints(a, b, 'extended')
    expect(extFrom.x).toBeLessThan(-1_000)
    expect(extTo.x).toBeGreaterThan(1_000)
  })
})

describe('hit testing', () => {
  const p = proj(10)

  it('grabs a thin trend line within tolerance and misses beyond it', () => {
    const line = drawing('trend_line', [
      [TIMES[0], 8_000_000],
      [TIMES[9], 8_000_000]
    ])
    const y = p.y(8_000_000)
    expect(hitTest(line, { x: 45, y: y + 4 }, ctx(p))).toBe(true)
    expect(hitTest(line, { x: 45, y: y + 30 }, ctx(p))).toBe(false)
  })

  it('does not grab a plain segment past its endpoints, but a ray is grabbable there', () => {
    const points: Array<[number, number]> = [
      [TIMES[0], 8_000_000],
      [TIMES[2], 8_000_000]
    ]
    const segment = drawing('trend_line', points, { style: { extend: 'segment' } })
    const ray = drawing('trend_line', points, { style: { extend: 'ray' } })
    const far = { x: 80, y: p.y(8_000_000) }
    expect(hitTest(segment, far, ctx(p))).toBe(false)
    expect(hitTest(ray, far, ctx(p))).toBe(true)
  })

  it('grabs a horizontal line anywhere across the chart and a vertical line down it', () => {
    const h = drawing('horizontal_line', [[TIMES[0], 8_050_000]])
    expect(hitTest(h, { x: 580, y: p.y(8_050_000) }, ctx(p))).toBe(true)
    const v = drawing('vertical_line', [[TIMES[4], 8_000_000]])
    expect(hitTest(v, { x: p.x(TIMES[4]), y: 12 }, ctx(p))).toBe(true)
  })

  it('grabs a rectangle by its edge and by its interior', () => {
    const rect = drawing('rectangle', [
      [TIMES[2], 8_100_000],
      [TIMES[6], 8_000_000]
    ])
    expect(hitTest(rect, { x: p.x(TIMES[2]), y: p.y(8_050_000) }, ctx(p))).toBe(true)
    expect(hitTest(rect, { x: p.x(TIMES[4]), y: p.y(8_050_000) }, ctx(p))).toBe(true)
    expect(hitTest(rect, { x: p.x(TIMES[8]), y: p.y(8_050_000) }, ctx(p))).toBe(false)
  })

  it('grabs a fib by any of its level lines but not by the gap outside the span', () => {
    const fib = drawing('fib_retracement', [
      [TIMES[2], 8_000_000],
      [TIMES[6], 8_100_000]
    ])
    const ys = fibLevelYs(
      { x: p.x(TIMES[2]), y: p.y(8_000_000) },
      { x: p.x(TIMES[6]), y: p.y(8_100_000) }
    )
    expect(hitTest(fib, { x: p.x(TIMES[4]), y: ys[4] }, ctx(p))).toBe(true)
    expect(hitTest(fib, { x: p.x(TIMES[9]), y: ys[4] }, ctx(p))).toBe(false)
  })
})

describe('editing', () => {
  const p = proj(10)

  it('moves one endpoint and leaves the other alone', () => {
    const line = drawing('trend_line', [
      [TIMES[1], 8_000_000],
      [TIMES[5], 8_050_000]
    ])
    const handles = handlesFor(line, p)
    expect(handles).toHaveLength(2)
    const moved = moveHandle(line, handles[1], { t: TIMES[8], price: 8_090_000 })
    expect(moved.points[0]).toEqual({ t: TIMES[1], price: 8_000_000 })
    expect(moved.points[1]).toEqual({ t: TIMES[8], price: 8_090_000 })
  })

  it('moves a whole drawing by one delta, preserving its shape', () => {
    const rect = drawing('rectangle', [
      [TIMES[2], 8_100_000],
      [TIMES[6], 8_000_000]
    ])
    const moved = moveDrawing(rect, 2 * DAY, 25_000)
    expect(moved.points[0]).toEqual({ t: TIMES[4], price: 8_125_000 })
    expect(moved.points[1]).toEqual({ t: TIMES[8], price: 8_025_000 })
    // Shape is unchanged: same width in time, same height in price.
    expect(moved.points[1].t - moved.points[0].t).toBe(rect.points[1].t - rect.points[0].t)
    expect(moved.points[1].price - moved.points[0].price).toBe(
      rect.points[1].price - rect.points[0].price
    )
  })

  it('resizes a rectangle from any of its four corners', () => {
    const rect = drawing('rectangle', [
      [TIMES[2], 8_100_000],
      [TIMES[6], 8_000_000]
    ])
    const handles = handlesFor(rect, p)
    expect(handles).toHaveLength(4)

    // The mixed corner takes its time from one anchor and its price from the
    // other — dragging it must move exactly those two coordinates.
    const mixed = handles.find((h) => h.timeIndex === 1 && h.priceIndex === 0)
    expect(mixed).toBeDefined()
    const resized = moveHandle(rect, mixed!, { t: TIMES[9], price: 8_200_000 })
    expect(resized.points[0]).toEqual({ t: TIMES[2], price: 8_200_000 })
    expect(resized.points[1]).toEqual({ t: TIMES[9], price: 8_000_000 })
  })

  it('gives a fib two draggable anchors and puts its levels at the seven ratios', () => {
    const fib = drawing('fib_retracement', [
      [TIMES[2], 8_000_000],
      [TIMES[6], 8_100_000]
    ])
    expect(handlesFor(fib, p)).toHaveLength(2)
    expect(FIB_LEVELS).toEqual([0, 0.236, 0.382, 0.5, 0.618, 0.786, 1])

    const a = { x: 0, y: 0 }
    const b = { x: 100, y: 200 }
    const ys = fibLevelYs(a, b)
    expect(ys[0]).toBeCloseTo(0)
    expect(ys[3]).toBeCloseTo(100)
    expect(ys[6]).toBeCloseTo(200)

    // Dragging an anchor moves every level with it.
    const dragged = moveHandle(fib, handlesFor(fib, p)[1], { t: TIMES[8], price: 8_200_000 })
    expect(dragged.points[1]).toEqual({ t: TIMES[8], price: 8_200_000 })
    expect(dragged.points[0]).toEqual(fib.points[0])
  })

  it('gives one-point drawings a single handle', () => {
    for (const type of ['horizontal_line', 'vertical_line', 'text'] as DrawingType[]) {
      expect(handlesFor(drawing(type, [[TIMES[3], 8_000_000]]), p)).toHaveLength(1)
    }
  })
})

describe('snapping', () => {
  // A wide zoom (40px per bar) so "near" and "far" in pixels are distinguishable:
  // at 10px per bar every point is within 5px of a bucket, and a weak time snap
  // would correctly grab every time.
  const p = proj(40)
  // 400 toman below the 4th candle's high: well under a pixel away on this scale.
  const nearHigh = { t: TIMES[4] + 200, price: CANDLES[4].high - 400 }
  // Half a bar (20px) and 40,000 toman (40px) from anything: far in both axes.
  const farOff = { t: TIMES[4] + DAY / 2, price: CANDLES[4].high + 40_000 }

  it('off never changes an anchor', () => {
    expect(snapPoint(nearHigh, { candles: CANDLES, mode: 'off', proj: p })).toEqual(nearHigh)
    expect(snapPoint(farOff, { candles: CANDLES, mode: 'off', proj: p })).toEqual(farOff)
  })

  it('weak grabs a value the pointer is already on, and leaves a free-hand point free', () => {
    const snapped = snapPoint(nearHigh, { candles: CANDLES, mode: 'weak', proj: p })
    expect(snapped).toEqual({ t: TIMES[4], price: CANDLES[4].high })

    const loose = snapPoint(farOff, { candles: CANDLES, mode: 'weak', proj: p })
    expect(loose).toEqual(farOff)
  })

  it('strong always takes the nearest bucket and its nearest OHLC value', () => {
    const snapped = snapPoint(farOff, { candles: CANDLES, mode: 'strong', proj: p })
    expect(snapped.t).toBe(TIMES[4])
    expect([CANDLES[4].open, CANDLES[4].high, CANDLES[4].low, CANDLES[4].close]).toContain(
      snapped.price
    )
    expect(snapped.price).toBe(CANDLES[4].high)
  })

  it('cannot snap without candles', () => {
    expect(snapPoint(farOff, { candles: [], mode: 'strong', proj: p })).toEqual(farOff)
  })
})

describe('validation', () => {
  const row = (over: Partial<ChartDrawing>): ChartDrawing => ({
    id: 7,
    symbol: 'IR_GOLD_18K',
    interval: '1d',
    drawing_type: 'trend_line',
    points: [
      { t: TIMES[0], price: 8_000_000 },
      { t: TIMES[4], price: 8_050_000 }
    ],
    style: { color: 'accent' },
    locked: false,
    visible: true,
    created_at: '2026-08-01T00:00:00Z',
    updated_at: '2026-08-01T00:00:00Z',
    ...over
  })

  it('accepts a well-formed row', () => {
    const parsed = parseDrawing(row({}))
    expect(parsed?.type).toBe('trend_line')
    expect(parsed?.style.color).toBe('accent')
    expect(parsed?.points).toHaveLength(2)
  })

  it('rejects an unknown drawing type', () => {
    expect(parseDrawing(row({ drawing_type: 'gann_fan' }))).toBeNull()
  })

  it('rejects the wrong number of anchors for the type', () => {
    expect(parseDrawing(row({ drawing_type: 'rectangle', points: [{ t: TIMES[0], price: 1 }] }))).toBeNull()
    expect(
      parseDrawing(
        row({
          drawing_type: 'horizontal_line',
          points: [
            { t: TIMES[0], price: 1 },
            { t: TIMES[1], price: 2 }
          ]
        })
      )
    ).toBeNull()
  })

  it('rejects non-finite coordinates and millisecond timestamps', () => {
    expect(
      parseDrawing(row({ points: [{ t: TIMES[0], price: NaN }, { t: TIMES[1], price: 1 }] }))
    ).toBeNull()
    expect(
      parseDrawing(
        row({ points: [{ t: TIMES[0] * 1000, price: 1 }, { t: TIMES[1], price: 1 }] })
      )
    ).toBeNull()
  })

  it('rejects an interval this build does not know', () => {
    expect(parseDrawing(row({ interval: '7s' }))).toBeNull()
  })

  it('drops only the bad rows from a list', () => {
    const parsed = parseDrawings([row({ id: 1 }), row({ id: 2, drawing_type: 'nope' }), row({ id: 3 })])
    expect(parsed.map((d) => d.id)).toEqual([1, 3])
  })

  it('refuses to create a draft with a degenerate anchor set', () => {
    expect(isValidDraft({ type: 'rectangle', points: [{ t: T0, price: 1 }], style: {} })).toBe(false)
    expect(
      isValidDraft({
        type: 'rectangle',
        points: [
          { t: T0, price: 1 },
          { t: T0 + DAY, price: 2 }
        ],
        style: {}
      })
    ).toBe(true)
  })

  it('rounds times to whole seconds on the way out, as the column stores them', () => {
    const body = toRequestBody(drawing('trend_line', [[T0 + 0.7, 1], [T0 + 1.2, 2]]))
    expect(body.points[0].t).toBe(Math.round(T0 + 0.7))
    expect(body.drawing_type).toBe('trend_line')
  })
})

describe('history', () => {
  const a = drawing('trend_line', [[TIMES[0], 1], [TIMES[1], 2]], { id: 1 })
  const b = drawing('rectangle', [[TIMES[2], 3], [TIMES[3], 4]], { id: 2 })

  it('undoes and redoes a create', () => {
    let h = emptyHistory([])
    h = pushHistory(h, [a])
    expect(h.present).toEqual([a])
    h = undoHistory(h)
    expect(h.present).toEqual([])
    h = redoHistory(h)
    expect(h.present).toEqual([a])
  })

  it('drops the redo stack once a new change is made', () => {
    let h = pushHistory(emptyHistory([]), [a])
    h = undoHistory(h)
    expect(h.future).toHaveLength(1)
    h = pushHistory(h, [b])
    expect(h.future).toHaveLength(0)
  })

  it('ignores a push that changes nothing', () => {
    const h = pushHistory(emptyHistory([a]), [{ ...a }])
    expect(h.past).toHaveLength(0)
  })

  it('bounds how far back it remembers', () => {
    let h = emptyHistory([])
    for (let i = 0; i < HISTORY_LIMIT + 20; i++) {
      h = pushHistory(h, [{ ...a, id: i + 1 }])
    }
    expect(h.past).toHaveLength(HISTORY_LIMIT)
  })

  it('rewrites an id everywhere so a redone create still points at a real row', () => {
    let h = pushHistory(emptyHistory([]), [{ ...a, id: -1 }])
    h = undoHistory(h)
    h = redoHistory(h)
    h = remapHistoryId(h, -1, 900)
    expect(h.present[0].id).toBe(900)
    expect(h.past.flat().every((d) => d.id !== -1)).toBe(true)
    expect(h.future.flat().every((d) => d.id !== -1)).toBe(true)
  })

  it('diffs two sets into creates, updates and deletes', () => {
    const moved = moveDrawing(b, DAY, 0)
    const added = drawing('text', [[TIMES[5], 9]], { id: 3 })
    const diff = diffDrawings([a, b], [moved, added])
    expect(diff.updated.map((d) => d.id)).toEqual([2])
    expect(diff.created.map((d) => d.id)).toEqual([3])
    expect(diff.deleted.map((d) => d.id)).toEqual([1])
  })
})

describe('readouts', () => {
  it('reads a range as price, percent, elapsed time and loaded bars', () => {
    const measure = drawing('measure', [
      [TIMES[2], 8_000_000],
      [TIMES[6], 8_400_000]
    ])
    const r = rangeReadout(measure, TIMES)
    expect(r.priceDelta).toBe(400_000)
    expect(r.pctDelta).toBeCloseTo(5)
    expect(r.seconds).toBe(4 * DAY)
    expect(r.bars).toBe(5)
  })

  it('counts only buckets that were actually loaded', () => {
    // A gap: nothing was collected between TIMES[3] and TIMES[7].
    const sparse = [TIMES[0], TIMES[1], TIMES[2], TIMES[3], TIMES[7], TIMES[8]]
    expect(countBarsBetween(sparse, TIMES[2], TIMES[8])).toBe(4)
    expect(countBarsBetween([], TIMES[0], TIMES[9])).toBe(0)
  })

  it('spells elapsed time the way a trader reads it', () => {
    expect(formatDuration(45)).toBe('45s')
    expect(formatDuration(2 * 3_600)).toBe('2h')
    expect(formatDuration(3 * DAY + 4 * 3_600)).toBe('3d 4h')
  })
})

// ---------- persistence ----------

const flush = () => act(async () => { await Promise.resolve() })

describe('useDrawings persistence', () => {
  beforeEach(() => {
    srv.state.reset()
  })

  const line = {
    type: 'trend_line' as DrawingType,
    points: [
      { t: TIMES[1], price: 8_000_000 },
      { t: TIMES[5], price: 8_050_000 }
    ],
    style: { color: 'accent' as const }
  }

  it('starts on the cursor with weak snapping', async () => {
    const { result } = renderHook(() => useDrawings('IR_GOLD_18K', '1d'))
    await waitFor(() => expect(result.current.loading).toBe(false))
    expect(result.current.snap).toBe('weak')
    expect(result.current.tool).toBeNull()
    expect(result.current.selectedId).toBeNull()
  })

  it('round-trips a create: optimistic first, then the server id', async () => {
    const { result } = renderHook(() => useDrawings('IR_GOLD_18K', '1d'))
    await waitFor(() => expect(result.current.loading).toBe(false))

    act(() => result.current.create(line))
    // Optimistic: on screen immediately, under a local (negative) id.
    expect(result.current.drawings).toHaveLength(1)
    expect(result.current.drawings[0].id).toBeLessThan(0)

    await waitFor(() => expect(result.current.drawings[0].id).toBe(100))
    expect(srv.state.rows).toHaveLength(1)
    expect(srv.state.rows[0].drawing_type).toBe('trend_line')
    expect(srv.state.rows[0].points).toEqual(line.points)
  })

  it('persists an edit with one PUT per burst, not one per call', async () => {
    const { result } = renderHook(() => useDrawings('IR_GOLD_18K', '1d'))
    await waitFor(() => expect(result.current.loading).toBe(false))
    act(() => result.current.create(line))
    await waitFor(() => expect(result.current.drawings[0].id).toBe(100))

    // Ten "frames" of a drag. Each delta is applied to the drawing as it was
    // when grabbed — never accumulated — which is exactly what the layer does.
    const grabbed = result.current.drawings[0]
    for (let i = 1; i <= 10; i++) {
      act(() => result.current.replace(moveDrawing(grabbed, i * 60, 0)))
    }
    await waitFor(() => {
      const puts = srv.state.calls.filter((c) => c.method === 'PUT')
      expect(puts).toHaveLength(1)
    })
    expect(srv.state.rows[0].points[0].t).toBe(TIMES[1] + 600)
  })

  it('deletes on the server and rolls back when the server refuses', async () => {
    const { result } = renderHook(() => useDrawings('IR_GOLD_18K', '1d'))
    await waitFor(() => expect(result.current.loading).toBe(false))
    act(() => result.current.create(line))
    await waitFor(() => expect(result.current.drawings[0].id).toBe(100))

    srv.state.failOn = '/chart/drawings/100'
    act(() => result.current.remove(100))
    expect(result.current.drawings).toHaveLength(0)
    await waitFor(() => expect(result.current.drawings).toHaveLength(1))
    expect(result.current.error).toBe('server said no')

    srv.state.failOn = null
    act(() => result.current.remove(100))
    await waitFor(() => expect(srv.state.rows).toHaveLength(0))
  })

  it('undo removes the row and redo writes a new one, keeping ids honest', async () => {
    const { result } = renderHook(() => useDrawings('IR_GOLD_18K', '1d'))
    await waitFor(() => expect(result.current.loading).toBe(false))
    act(() => result.current.create(line))
    await waitFor(() => expect(result.current.drawings[0].id).toBe(100))

    act(() => result.current.undo())
    expect(result.current.drawings).toHaveLength(0)
    await waitFor(() => expect(srv.state.rows).toHaveLength(0))

    act(() => result.current.redo())
    expect(result.current.drawings).toHaveLength(1)
    await waitFor(() => expect(srv.state.rows).toHaveLength(1))
    // A new bigserial, threaded back into local state — not the id that 404s.
    await waitFor(() => expect(result.current.drawings[0].id).toBe(101))
    expect(srv.state.rows[0].id).toBe(101)

    // And the re-created row is editable, which is what a stale id would break.
    act(() => result.current.replace(moveDrawing(result.current.drawings[0], DAY, 0)))
    await waitFor(() => expect(srv.state.rows[0].points[0].t).toBe(TIMES[1] + DAY))
  })

  it('rolls a failed create back off the chart', async () => {
    const { result } = renderHook(() => useDrawings('IR_GOLD_18K', '1d'))
    await waitFor(() => expect(result.current.loading).toBe(false))

    srv.state.failOn = '/chart/drawings'
    act(() => result.current.create(line))
    expect(result.current.drawings).toHaveLength(1)
    await waitFor(() => expect(result.current.drawings).toHaveLength(0))
    expect(result.current.error).toBe('server said no')
    expect(result.current.canUndo).toBe(true)
    act(() => result.current.undo())
    // Undo after a failed create must not resurrect it.
    expect(result.current.drawings).toHaveLength(0)
  })

  it('surfaces a truncated list rather than showing a partial set silently', async () => {
    srv.state.truncated = true
    const { result } = renderHook(() => useDrawings('IR_GOLD_18K', '1d'))
    await waitFor(() => expect(result.current.loading).toBe(false))
    expect(result.current.truncated).toBe(true)
    expect(result.current.limit).toBe(250)
  })

  it('drops rows it cannot read instead of blanking the chart', async () => {
    srv.state.rows.push({
      id: 5,
      symbol: 'IR_GOLD_18K',
      interval: '1d',
      drawing_type: 'starship',
      points: [{ t: TIMES[0], price: 1 }],
      style: {},
      locked: false,
      visible: true,
      created_at: '',
      updated_at: ''
    })
    srv.state.rows.push({
      id: 6,
      symbol: 'IR_GOLD_18K',
      interval: '1d',
      drawing_type: 'horizontal_line',
      points: [{ t: TIMES[0], price: 8_000_000 }],
      style: {},
      locked: false,
      visible: true,
      created_at: '',
      updated_at: ''
    })
    const { result } = renderHook(() => useDrawings('IR_GOLD_18K', '1d'))
    await waitFor(() => expect(result.current.loading).toBe(false))
    expect(result.current.drawings.map((d) => d.id)).toEqual([6])
  })

  it('keeps a separate set per timeframe and swaps it on switch', async () => {
    const { result, rerender } = renderHook(
      ({ interval }: { interval: '1d' | '4h' }) => useDrawings('IR_GOLD_18K', interval),
      { initialProps: { interval: '1d' as '1d' | '4h' } }
    )
    await waitFor(() => expect(result.current.loading).toBe(false))
    act(() => result.current.create(line))
    await waitFor(() => expect(result.current.drawings[0].id).toBe(100))

    rerender({ interval: '4h' })
    // Cleared before the new request resolves: never one chart's drawings on another.
    expect(result.current.drawings).toHaveLength(0)
    await waitFor(() => expect(result.current.loading).toBe(false))
    expect(result.current.drawings).toHaveLength(0)

    act(() => result.current.create(line))
    await waitFor(() => expect(srv.state.rows).toHaveLength(2))
    expect(srv.state.rows[1].interval).toBe('4h')

    rerender({ interval: '1d' })
    await waitFor(() => expect(result.current.drawings).toHaveLength(1))
    expect(result.current.drawings[0].interval).toBe('1d')
    expect(result.current.drawings[0].id).toBe(100)
  })

  it('does not leak one symbol’s drawings onto another', async () => {
    const { result, rerender } = renderHook(
      ({ symbol }: { symbol: string }) => useDrawings(symbol, '1d'),
      { initialProps: { symbol: 'IR_GOLD_18K' } }
    )
    await waitFor(() => expect(result.current.loading).toBe(false))
    act(() => result.current.create(line))
    await waitFor(() => expect(result.current.drawings).toHaveLength(1))

    rerender({ symbol: 'XAUUSD' })
    expect(result.current.drawings).toHaveLength(0)
    await waitFor(() => expect(result.current.loading).toBe(false))
    expect(result.current.drawings).toHaveLength(0)

    rerender({ symbol: 'IR_GOLD_18K' })
    await waitFor(() => expect(result.current.drawings).toHaveLength(1))
    expect(result.current.drawings[0].symbol).toBe('IR_GOLD_18K')
  })

  it('never asks the server to move a drawing between charts', async () => {
    const { result, rerender } = renderHook(
      ({ interval }: { interval: '1d' | '4h' }) => useDrawings('IR_GOLD_18K', interval),
      { initialProps: { interval: '1d' as '1d' | '4h' } }
    )
    await waitFor(() => expect(result.current.loading).toBe(false))
    act(() => result.current.create(line))
    await waitFor(() => expect(result.current.drawings[0].id).toBe(100))
    act(() => result.current.replace(moveDrawing(result.current.drawings[0], DAY, 0)))
    await waitFor(() => expect(srv.state.calls.some((c) => c.method === 'PUT')).toBe(true))

    rerender({ interval: '4h' })
    await flush()
    // Every write named the chart the drawing was drawn on — the 400 the server
    // raises for a move is never provoked.
    for (const call of srv.state.calls) {
      if (call.method !== 'PUT') continue
      const body = call.body as { symbol: string; interval: string }
      expect(body.interval).toBe('1d')
      expect(body.symbol).toBe('IR_GOLD_18K')
    }
  })

  it('hides and shows drawings, persisting both', async () => {
    const { result } = renderHook(() => useDrawings('IR_GOLD_18K', '1d'))
    await waitFor(() => expect(result.current.loading).toBe(false))
    act(() => result.current.create(line))
    await waitFor(() => expect(result.current.drawings[0].id).toBe(100))

    act(() => result.current.replace({ ...result.current.drawings[0], visible: false }))
    expect(result.current.hiddenCount).toBe(1)
    await waitFor(() => expect(srv.state.rows[0].visible).toBe(false))

    act(() => result.current.showAll())
    expect(result.current.hiddenCount).toBe(0)
    await waitFor(() => expect(srv.state.rows[0].visible).toBe(true))
  })

  it('duplicates a drawing onto free space', async () => {
    const { result } = renderHook(() =>
      useDrawings('IR_GOLD_18K', '1d', { intervalSeconds: DAY })
    )
    await waitFor(() => expect(result.current.loading).toBe(false))
    act(() => result.current.create(line))
    await waitFor(() => expect(result.current.drawings[0].id).toBe(100))

    act(() => result.current.duplicate(100))
    await waitFor(() => expect(srv.state.rows).toHaveLength(2))
    expect(srv.state.rows[1].points[0].t).toBe(TIMES[1] + 2 * DAY)
  })
})
