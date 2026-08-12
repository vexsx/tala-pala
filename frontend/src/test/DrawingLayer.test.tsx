import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { ChartCandle } from '../api/types'

/**
 * The layer under a real pointer, against a fake server.
 *
 * jsdom has no canvas, so painting is recorded rather than rasterised — which is
 * what the geometry assertions actually need: "this trend line was stroked from
 * the 3rd candle to the 7th" is a claim about coordinates, not about pixels.
 */
const srv = vi.hoisted(() => {
  class ApiError extends Error {
    status: number
    constructor(status: number, message: string) {
      super(message)
      this.name = 'ApiError'
      this.status = status
    }
  }
  const state = {
    rows: [] as Array<Record<string, unknown>>,
    nextId: 500,
    calls: [] as Array<{ method: string; path: string; body?: Record<string, unknown> }>,
    truncated: false,
    reset() {
      state.rows = []
      state.nextId = 500
      state.calls = []
      state.truncated = false
    },
    bodies(method: string) {
      return state.calls.filter((c) => c.method === method).map((c) => c.body)
    }
  }
  const api = async (path: string, opts?: { method?: string; body?: unknown }) => {
    const method = opts?.method ?? 'GET'
    state.calls.push({ method, path, body: opts?.body as Record<string, unknown> })
    if (method === 'GET') {
      return { items: [], count: 0, limit: 250, truncated: state.truncated }
    }
    if (method === 'POST') {
      const body = opts?.body as Record<string, unknown>
      const row = {
        ...body,
        id: state.nextId++,
        created_at: '2026-08-01T00:00:00Z',
        updated_at: '2026-08-01T00:00:00Z'
      }
      state.rows.push(row)
      return row
    }
    if (method === 'PUT') {
      const body = opts?.body as Record<string, unknown>
      const id = Number(path.split('/').pop())
      const row = { ...body, id, created_at: '', updated_at: '' }
      const i = state.rows.findIndex((r) => r.id === id)
      if (i >= 0) state.rows[i] = row
      return row
    }
    if (method === 'DELETE') {
      const id = Number(path.split('/').pop())
      state.rows = state.rows.filter((r) => r.id !== id)
      return null
    }
    throw new ApiError(405, 'unexpected')
  }
  return { state, api, ApiError }
})

vi.mock('../api/client', () => ({
  api: srv.api,
  ApiError: srv.ApiError,
  errorMessage: (err: unknown) => (err instanceof Error ? err.message : 'Unexpected error')
}))

import { DrawingLayer } from '../chart/drawings/DrawingLayer'
import { DrawingToolbar } from '../chart/DrawingToolbar'
import { useDrawings, type DrawingEngine } from '../chart/drawings/useDrawings'
import type { DrawingType } from '../chart/drawings/model'
import type { ChartHandle } from '../chart/TradingChart'
import { SettingsProvider } from '../lib/settings'

const DAY = 86_400
const T0 = Date.parse('2026-08-01T00:00:00Z') / 1000
const WIDTH = 600
const HEIGHT = 400

const CANDLES: ChartCandle[] = Array.from({ length: 10 }, (_, i) => {
  const close = 8_000_000 + i * 10_000
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
})

/** x = 10px per day at this zoom; y = 8,000,000 at 300, 1,000 toman per pixel. */
const zoom = { barPx: 10 }
const xOf = (t: number) => ((t - T0) / DAY) * zoom.barPx
const yOf = (price: number) => 300 - (price - 8_000_000) / 1_000

// ---- canvas recorder ----------------------------------------------------

interface Op {
  op: string
  args: number[]
}
let ops: Op[] = []

function fakeContext(): CanvasRenderingContext2D {
  const push = (op: string, ...args: number[]) => {
    ops.push({ op, args })
  }
  const noop = () => undefined
  const ctx = {
    save: noop,
    restore: noop,
    beginPath: noop,
    closePath: noop,
    stroke: noop,
    fill: noop,
    setTransform: noop,
    clearRect: noop,
    setLineDash: noop,
    moveTo: (x: number, y: number) => push('moveTo', x, y),
    lineTo: (x: number, y: number) => push('lineTo', x, y),
    fillRect: (x: number, y: number, w: number, h: number) => push('fillRect', x, y, w, h),
    strokeRect: (x: number, y: number, w: number, h: number) => push('strokeRect', x, y, w, h),
    arc: (x: number, y: number, r: number) => push('arc', x, y, r),
    fillText: (_t: string, x: number, y: number) => push('fillText', x, y),
    measureText: () => ({ width: 40 }),
    strokeStyle: '',
    fillStyle: '',
    lineWidth: 1,
    globalAlpha: 1,
    font: '',
    textBaseline: 'top'
  }
  return ctx as unknown as CanvasRenderingContext2D
}

/** Stroked segments, paired from the moveTo/lineTo stream. */
function segments(): Array<{ x1: number; y1: number; x2: number; y2: number }> {
  const out: Array<{ x1: number; y1: number; x2: number; y2: number }> = []
  for (let i = 0; i < ops.length - 1; i++) {
    if (ops[i].op === 'moveTo' && ops[i + 1].op === 'lineTo') {
      out.push({
        x1: ops[i].args[0],
        y1: ops[i].args[1],
        x2: ops[i + 1].args[0],
        y2: ops[i + 1].args[1]
      })
    }
  }
  return out
}

// ---- harness ------------------------------------------------------------

let container: HTMLDivElement
let chartSurface: HTMLDivElement
let engine: DrawingEngine
let viewportListeners: Set<() => void>

function makeHandle(): ChartHandle {
  viewportListeners = new Set()
  return {
    chart: {} as ChartHandle['chart'],
    candleSeries: {} as ChartHandle['candleSeries'],
    container,
    timeToX: (t: number) => xOf(t),
    priceToY: (price: number) => yOf(price),
    xToTime: (x: number) => T0 + (x / zoom.barPx) * DAY,
    yToPrice: (y: number) => 8_000_000 + (300 - y) * 1_000,
    onViewportChange: (cb: () => void) => {
      viewportListeners.add(cb)
      return () => {
        viewportListeners.delete(cb)
      }
    }
  }
}

function Harness({ handle, toolbar }: { handle: ChartHandle; toolbar?: boolean }) {
  const eng = useDrawings('IR_GOLD_18K', '1d', { intervalSeconds: DAY })
  engine = eng
  return (
    <>
      {/* The toolbar must share this tree, not live in its own root: it reads
          the same engine, and a second root would never re-render when the
          engine's state changed. */}
      {toolbar && <DrawingToolbar engine={eng} />}
      <DrawingLayer
        handle={handle}
        engine={eng}
        symbol="IR_GOLD_18K"
        interval="1d"
        unit="IRT"
        candles={CANDLES}
      />
    </>
  )
}

/** A pointer event jsdom will actually construct; only the fields the layer reads. */
function pointer(type: string, x: number, y: number, target: Element = chartSurface): MouseEvent {
  const e = new MouseEvent(type, {
    bubbles: true,
    cancelable: true,
    clientX: x,
    clientY: y,
    button: 0
  })
  Object.defineProperty(e, 'pointerId', { value: 1 })
  Object.defineProperty(e, 'pointerType', { value: 'mouse' })
  target.dispatchEvent(e)
  return e
}

function drag(from: [number, number], to: [number, number], target?: Element) {
  act(() => {
    pointer('pointerdown', from[0], from[1], target)
  })
  act(() => {
    pointer('pointermove', to[0], to[1], target)
  })
  act(() => {
    pointer('pointerup', to[0], to[1], target)
  })
}

function key(k: string, init: KeyboardEventInit = {}, target: Element | Document = document) {
  act(() => {
    target.dispatchEvent(new KeyboardEvent('keydown', { key: k, bubbles: true, ...init }))
  })
}

async function setup(opts: { toolbar?: boolean } = {}) {
  container = document.createElement('div')
  container.getBoundingClientRect = () =>
    ({ left: 0, top: 0, right: WIDTH, bottom: HEIGHT, width: WIDTH, height: HEIGHT, x: 0, y: 0 }) as DOMRect
  document.body.appendChild(container)

  const handle = makeHandle()
  render(
    <SettingsProvider>
      <Harness handle={handle} toolbar={opts.toolbar} />
    </SettingsProvider>,
    { container }
  )

  // Stands in for the lightweight-charts canvas: events start here, and the
  // layer's capture listener on `container` sees them first. Appended after the
  // render because createRoot clears whatever it finds in the container.
  chartSurface = document.createElement('div')
  container.appendChild(chartSurface)

  await waitFor(() => expect(engine.loading).toBe(false))
  act(() => engine.setSnap('off'))
  ops = []
}

async function createWith(type: DrawingType, from: [number, number], to: [number, number]) {
  act(() => engine.setTool({ type }))
  drag(from, to)
  await waitFor(() => expect(engine.drawings.length).toBeGreaterThan(0))
  await waitFor(() => expect(engine.drawings[engine.drawings.length - 1].id).toBeGreaterThan(0))
}

beforeEach(() => {
  srv.state.reset()
  ops = []
  zoom.barPx = 10
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockImplementation(() => fakeContext())
  // Paint synchronously so a gesture's effect on the canvas is observable
  // immediately instead of a frame later.
  vi.spyOn(window, 'requestAnimationFrame').mockImplementation((cb: FrameRequestCallback) => {
    cb(0)
    return 1
  })
  vi.spyOn(window, 'cancelAnimationFrame').mockImplementation(() => undefined)
})

afterEach(() => {
  vi.restoreAllMocks()
  container?.remove()
})

describe('DrawingLayer creation', () => {
  beforeEach(setup)

  it('creates every tool with exactly the anchors its type demands', async () => {
    const expected: Array<[DrawingType, number]> = [
      ['trend_line', 2],
      ['horizontal_line', 1],
      ['vertical_line', 1],
      ['rectangle', 2],
      ['price_range', 2],
      ['date_range', 2],
      ['measure', 2],
      ['fib_retracement', 2],
      ['text', 1]
    ]
    for (const [type] of expected) {
      await createWith(type, [20, 250], [60, 200])
    }
    const posted = srv.state.bodies('POST')
    expect(posted).toHaveLength(expected.length)
    expected.forEach(([type, count], i) => {
      expect(posted[i]?.drawing_type).toBe(type)
      expect((posted[i]?.points as unknown[]).length).toBe(count)
    })
  })

  it('anchors a new drawing in data space, not in pixels', async () => {
    await createWith('trend_line', [30, yOf(8_030_000)], [70, yOf(8_070_000)])
    const body = srv.state.bodies('POST')[0]
    const points = body?.points as Array<{ t: number; price: number }>
    expect(points[0].t).toBe(T0 + 3 * DAY)
    expect(points[0].price).toBeCloseTo(8_030_000, 0)
    expect(points[1].t).toBe(T0 + 7 * DAY)
    expect(points[1].price).toBeCloseTo(8_070_000, 0)
  })

  it('carries the trend-line variant into the stored style', async () => {
    act(() => engine.setTool({ type: 'trend_line', extend: 'ray' }))
    drag([20, 250], [60, 220])
    await waitFor(() => expect(srv.state.bodies('POST')).toHaveLength(1))
    expect((srv.state.bodies('POST')[0]?.style as Record<string, unknown>).extend).toBe('ray')
  })

  it('refuses a two-point drawing made by a click with no drag', async () => {
    act(() => engine.setTool({ type: 'rectangle' }))
    act(() => {
      pointer('pointerdown', 40, 200)
    })
    act(() => {
      pointer('pointerup', 40, 200)
    })
    expect(engine.drawings).toHaveLength(0)
    expect(srv.state.bodies('POST')).toHaveLength(0)
  })

  it('still creates a one-point drawing from a plain click', async () => {
    act(() => engine.setTool({ type: 'horizontal_line' }))
    act(() => {
      pointer('pointerdown', 40, 200)
    })
    act(() => {
      pointer('pointerup', 40, 200)
    })
    await waitFor(() => expect(srv.state.bodies('POST')).toHaveLength(1))
  })

  it('returns to the cursor once a drawing is made', async () => {
    await createWith('rectangle', [20, 250], [60, 200])
    expect(engine.tool).toBeNull()
  })

  it('Escape cancels the drawing in progress and writes nothing', async () => {
    act(() => engine.setTool({ type: 'trend_line' }))
    act(() => {
      pointer('pointerdown', 20, 250)
    })
    act(() => {
      pointer('pointermove', 60, 200)
    })
    key('Escape')
    act(() => {
      pointer('pointerup', 60, 200)
    })
    expect(engine.drawings).toHaveLength(0)
    expect(srv.state.bodies('POST')).toHaveLength(0)
  })

  it('Escape with no drawing in progress disarms the tool', async () => {
    act(() => engine.setTool({ type: 'rectangle' }))
    key('Escape')
    expect(engine.tool).toBeNull()
  })

  it('snaps a new anchor to a candle when snapping is on', async () => {
    act(() => engine.setSnap('strong'))
    act(() => engine.setTool({ type: 'trend_line' }))
    // Deliberately between buckets and off every OHLC value.
    drag([34, yOf(8_037_000)], [72, yOf(8_061_000)])
    await waitFor(() => expect(srv.state.bodies('POST')).toHaveLength(1))
    const points = srv.state.bodies('POST')[0]?.points as Array<{ t: number; price: number }>
    expect(points[0].t).toBe(CANDLES[3].t)
    expect([CANDLES[3].open, CANDLES[3].high, CANDLES[3].low, CANDLES[3].close]).toContain(
      points[0].price
    )
  })
})

describe('DrawingLayer selection and editing', () => {
  beforeEach(setup)

  it('selects on click and clears the selection on empty space', async () => {
    await createWith('trend_line', [20, 250], [80, 250])
    act(() => engine.select(null))
    expect(engine.selectedId).toBeNull()

    act(() => {
      pointer('pointerdown', 50, 250)
    })
    act(() => {
      pointer('pointerup', 50, 250)
    })
    expect(engine.selectedId).toBe(engine.drawings[0].id)
    expect(screen.getByRole('group', { name: 'Selected drawing' })).toBeInTheDocument()

    act(() => {
      pointer('pointerdown', 300, 60)
    })
    act(() => {
      pointer('pointerup', 300, 60)
    })
    expect(engine.selectedId).toBeNull()
  })

  it('drags a whole drawing and persists it once', async () => {
    await createWith('trend_line', [20, 250], [60, 250])
    const before = engine.drawings[0]
    srv.state.calls = []

    // Grab the middle of the line and move it 20px right, 30px up.
    drag([40, 250], [60, 220])

    await waitFor(() => expect(srv.state.bodies('PUT')).toHaveLength(1))
    const points = srv.state.bodies('PUT')[0]?.points as Array<{ t: number; price: number }>
    expect(points[0].t).toBe(before.points[0].t + 2 * DAY)
    expect(points[1].t).toBe(before.points[1].t + 2 * DAY)
    expect(points[0].price).toBeCloseTo(before.points[0].price + 30_000, 0)
    // Shape preserved: the drag moved it, it did not stretch it.
    expect(points[1].t - points[0].t).toBe(before.points[1].t - before.points[0].t)
  })

  it('drags one endpoint and leaves the other where it was', async () => {
    await createWith('trend_line', [20, 250], [60, 250])
    const before = engine.drawings[0]
    srv.state.calls = []

    // Grab the second anchor exactly and drag it.
    drag([60, 250], [90, 200])

    await waitFor(() => expect(srv.state.bodies('PUT')).toHaveLength(1))
    const points = srv.state.bodies('PUT')[0]?.points as Array<{ t: number; price: number }>
    expect(points[0]).toEqual(before.points[0])
    expect(points[1].t).toBe(T0 + 9 * DAY)
    expect(points[1].price).toBeCloseTo(8_100_000, 0)
  })

  it('resizes a rectangle from a corner without moving the opposite one', async () => {
    await createWith('rectangle', [20, 250], [60, 200])
    const before = engine.drawings[0]
    srv.state.calls = []

    // The corner that takes its time from anchor 1 and its price from anchor 0.
    drag([60, 250], [100, 260])

    await waitFor(() => expect(srv.state.bodies('PUT')).toHaveLength(1))
    const points = srv.state.bodies('PUT')[0]?.points as Array<{ t: number; price: number }>
    expect(points[0].t).toBe(before.points[0].t)
    expect(points[1].price).toBe(before.points[1].price)
    expect(points[1].t).toBe(T0 + 10 * DAY)
    expect(points[0].price).toBeCloseTo(8_040_000, 0)
  })

  it('drags a fib anchor, moving every level with it', async () => {
    await createWith('fib_retracement', [20, 250], [60, 150])
    ops = []
    act(() => {
      viewportListeners.forEach((cb) => cb())
    })
    const levelsBefore = segments().filter((s) => s.y1 === s.y2).length
    expect(levelsBefore).toBeGreaterThanOrEqual(7)

    srv.state.calls = []
    drag([60, 150], [60, 100])
    await waitFor(() => expect(srv.state.bodies('PUT')).toHaveLength(1))
    const points = srv.state.bodies('PUT')[0]?.points as Array<{ t: number; price: number }>
    expect(points[0].price).toBeCloseTo(8_050_000, 0)
    expect(points[1].price).toBeCloseTo(8_200_000, 0)

    // The 1.0 level now sits where the dragged anchor does.
    ops = []
    act(() => {
      viewportListeners.forEach((cb) => cb())
    })
    const ys = segments()
      .filter((s) => s.y1 === s.y2)
      .map((s) => s.y1)
    expect(Math.min(...ys)).toBeCloseTo(100, 0)
  })

  it('selects a locked drawing but refuses to move it', async () => {
    await createWith('trend_line', [20, 250], [80, 250])
    act(() => engine.replace({ ...engine.drawings[0], locked: true }))
    await waitFor(() => expect(srv.state.bodies('PUT')).toHaveLength(1))
    const anchors = engine.drawings[0].points.map((p) => ({ ...p }))
    srv.state.calls = []

    act(() => engine.select(null))
    drag([50, 250], [50, 180])

    expect(engine.selectedId).toBe(engine.drawings[0].id)
    expect(engine.drawings[0].points).toEqual(anchors)
    expect(srv.state.bodies('PUT')).toHaveLength(0)
  })

  it('a hidden drawing is neither painted nor grabbable', async () => {
    await createWith('trend_line', [20, 250], [80, 250])
    const id = engine.drawings[0].id
    act(() => engine.replace({ ...engine.drawings[0], visible: false }))
    act(() => engine.select(null))

    ops = []
    act(() => {
      viewportListeners.forEach((cb) => cb())
    })
    expect(segments()).toHaveLength(0)

    act(() => {
      pointer('pointerdown', 50, 250)
    })
    act(() => {
      pointer('pointerup', 50, 250)
    })
    expect(engine.selectedId).toBeNull()
    expect(engine.hiddenCount).toBe(1)
    expect(engine.drawings.find((d) => d.id === id)?.visible).toBe(false)
  })
})

describe('DrawingLayer keyboard', () => {
  beforeEach(setup)

  it('Delete removes the selection', async () => {
    await createWith('trend_line', [20, 250], [60, 250])
    expect(engine.drawings).toHaveLength(1)
    key('Delete')
    expect(engine.drawings).toHaveLength(0)
    await waitFor(() => expect(srv.state.rows).toHaveLength(0))
  })

  it('Backspace removes the selection too', async () => {
    await createWith('rectangle', [20, 250], [60, 200])
    key('Backspace')
    expect(engine.drawings).toHaveLength(0)
  })

  it('refuses to delete a locked drawing', async () => {
    await createWith('trend_line', [20, 250], [60, 250])
    act(() => engine.replace({ ...engine.drawings[0], locked: true }))
    key('Delete')
    expect(engine.drawings).toHaveLength(1)
  })

  it('leaves Delete alone while the label field has focus', async () => {
    await createWith('text', [40, 200], [40, 200])
    const input = screen.getByLabelText('Drawing label text')
    key('Delete', {}, input)
    expect(engine.drawings).toHaveLength(1)
  })

  it('undoes and redoes with Ctrl/Cmd+Z', async () => {
    await createWith('trend_line', [20, 250], [60, 250])
    expect(engine.drawings).toHaveLength(1)

    key('z', { ctrlKey: true })
    expect(engine.drawings).toHaveLength(0)
    await waitFor(() => expect(srv.state.rows).toHaveLength(0))

    key('z', { ctrlKey: true, shiftKey: true })
    expect(engine.drawings).toHaveLength(1)
    await waitFor(() => expect(srv.state.rows).toHaveLength(1))

    key('z', { metaKey: true })
    expect(engine.drawings).toHaveLength(0)
  })
})

describe('DrawingLayer painting', () => {
  beforeEach(setup)

  it('keeps drawings on their candles across a zoom', async () => {
    await createWith('trend_line', [30, yOf(8_030_000)], [70, yOf(8_070_000)])

    ops = []
    act(() => {
      viewportListeners.forEach((cb) => cb())
    })
    const before = segments().find((s) => Math.abs(s.x1 - 30) < 1)
    expect(before).toBeDefined()
    expect(before?.x2).toBeCloseTo(70, 0)

    // Zoom in: the same anchors, a wider bar.
    zoom.barPx = 25
    ops = []
    act(() => {
      viewportListeners.forEach((cb) => cb())
    })
    const after = segments().find((s) => Math.abs(s.x1 - xOf(T0 + 3 * DAY)) < 1)

    // The line moved on screen, exactly as far as its candles did.
    expect(after).toBeDefined()
    expect(after?.x1).toBeCloseTo(xOf(CANDLES[3].t), 0)
    expect(after?.x2).toBeCloseTo(xOf(CANDLES[7].t), 0)
    expect(after?.x1).not.toBeCloseTo(before!.x1, 0)

    // And the anchors themselves never moved.
    expect(engine.drawings[0].points[0].t).toBe(CANDLES[3].t)
    expect(engine.drawings[0].points[1].t).toBe(CANDLES[7].t)
  })

  it('draws a horizontal line clear across the chart at its price', async () => {
    await createWith('horizontal_line', [40, yOf(8_055_000)], [40, yOf(8_055_000)])
    ops = []
    act(() => {
      viewportListeners.forEach((cb) => cb())
    })
    const span = segments().find((s) => s.x1 === 0 && s.x2 === WIDTH)
    expect(span).toBeDefined()
    expect(span?.y1).toBeCloseTo(yOf(8_055_000), 0)
  })
})

describe('DrawingLayer and the chart underneath', () => {
  beforeEach(setup)

  it('lets a drag that hits nothing reach the chart, so panning still works', async () => {
    const seen: string[] = []
    chartSurface.addEventListener('pointerdown', () => seen.push('chart'))

    act(() => {
      pointer('pointerdown', 400, 80)
    })
    expect(seen).toEqual(['chart'])
  })

  it('claims the gesture when a tool is armed, so drawing does not pan the chart', async () => {
    const seen: string[] = []
    chartSurface.addEventListener('pointerdown', () => seen.push('chart'))

    act(() => engine.setTool({ type: 'trend_line' }))
    act(() => {
      pointer('pointerdown', 40, 200)
    })
    expect(seen).toEqual([])
    key('Escape')
  })

  it('claims the gesture when a drawing is hit, so dragging it does not pan', async () => {
    await createWith('trend_line', [20, 250], [80, 250])
    const seen: string[] = []
    chartSurface.addEventListener('pointerdown', () => seen.push('chart'))

    act(() => {
      pointer('pointerdown', 50, 250)
    })
    expect(seen).toEqual([])
  })

  it('never claims a click on the chart action buttons', async () => {
    const actions = document.createElement('div')
    actions.className = 'tchart-actions'
    const button = document.createElement('button')
    actions.appendChild(button)
    container.appendChild(actions)
    const seen: string[] = []
    button.addEventListener('pointerdown', () => seen.push('button'))

    act(() => engine.setTool({ type: 'rectangle' }))
    act(() => {
      pointer('pointerdown', 560, 370, button)
    })
    expect(seen).toEqual(['button'])
    expect(engine.drawings).toHaveLength(0)
  })
})

describe('DrawingLayer selection bar', () => {
  beforeEach(setup)

  it('locks, hides, duplicates and deletes the selection', async () => {
    await createWith('trend_line', [20, 250], [60, 250])

    act(() => {
      screen.getByLabelText('Lock drawing').click()
    })
    expect(engine.drawings[0].locked).toBe(true)
    expect(screen.getByLabelText('Delete drawing')).toBeDisabled()

    act(() => {
      screen.getByLabelText('Unlock drawing').click()
    })
    expect(engine.drawings[0].locked).toBe(false)

    act(() => {
      screen.getByLabelText('Hide drawing').click()
    })
    expect(engine.drawings[0].visible).toBe(false)
    act(() => {
      screen.getByLabelText('Show drawing').click()
    })
    expect(engine.drawings[0].visible).toBe(true)

    act(() => {
      screen.getByLabelText('Duplicate drawing').click()
    })
    await waitFor(() => expect(engine.drawings).toHaveLength(2))
    expect(engine.drawings[1].points[0].t).toBe(engine.drawings[0].points[0].t + 2 * DAY)

    act(() => {
      screen.getByLabelText('Delete drawing').click()
    })
    await waitFor(() => expect(engine.drawings).toHaveLength(1))
  })

  it('edits a text label and stores it on blur', async () => {
    await createWith('text', [40, 200], [40, 200])
    const input = screen.getByLabelText('Drawing label text') as HTMLInputElement
    srv.state.calls = []

    // fireEvent.change goes through React's value setter; assigning .value
    // directly is invisible to a controlled input.
    fireEvent.change(input, { target: { value: 'Breakout watch' } })
    fireEvent.blur(input)
    await waitFor(() => expect(srv.state.bodies('PUT')).toHaveLength(1))
    expect((srv.state.bodies('PUT')[0]?.style as Record<string, unknown>).text).toBe(
      'Breakout watch'
    )
  })

  it('shows nothing when nothing is selected', async () => {
    expect(screen.queryByRole('group', { name: 'Selected drawing' })).not.toBeInTheDocument()
  })
})

describe('DrawingLayer honesty about what it is showing', () => {
  it('says so when the chart holds more drawings than one response carries', async () => {
    srv.state.truncated = true
    await setup()
    expect(
      screen.getByText(/Showing the first 250 drawings on this chart/)
    ).toBeInTheDocument()
  })

  it('stays quiet when the whole set came back', async () => {
    await setup()
    expect(screen.queryByText(/Showing the first/)).not.toBeInTheDocument()
  })
})

describe('DrawingToolbar', () => {
  // In the real page the toolbar sits in ChartToolbar's drawSlot; here it just
  // needs to be driven by the same engine as the layer.
  beforeEach(() => setup({ toolbar: true }))

  it('arms a tool from the menu and closes it', async () => {
    act(() => {
      screen.getByLabelText('Drawing tools').click()
    })
    act(() => {
      screen.getByLabelText('Draw Rectangle').click()
    })
    expect(engine.tool).toEqual({ type: 'rectangle', extend: undefined })
    expect(screen.queryByRole('menu', { name: 'Drawing tools' })).not.toBeInTheDocument()
    // The trigger now names the armed tool rather than saying "Draw".
    expect(screen.getByLabelText('Drawing tool: Rectangle')).toBeInTheDocument()
  })

  it('offers the three trend-line variants as one type with different reach', async () => {
    act(() => {
      screen.getByLabelText('Drawing tools').click()
    })
    act(() => {
      screen.getByLabelText('Draw Ray').click()
    })
    expect(engine.tool).toEqual({ type: 'trend_line', extend: 'ray' })
  })

  it('returns to the cursor', async () => {
    act(() => engine.setTool({ type: 'text' }))
    act(() => {
      screen.getByLabelText('Drawing tool: Text').click()
    })
    act(() => {
      screen.getByLabelText('Select and move drawings').click()
    })
    expect(engine.tool).toBeNull()
  })

  it('switches snap mode and marks the active one without relying on colour', async () => {
    // setup() turns snapping off so the gesture tests have exact coordinates;
    // the engine's own default is asserted in drawings.test.ts.
    act(() => engine.setSnap('weak'))
    expect(screen.getByLabelText('Snap weak')).toHaveAttribute('aria-pressed', 'true')
    act(() => {
      screen.getByLabelText('Snap strong').click()
    })
    expect(engine.snap).toBe('strong')
    expect(screen.getByLabelText('Snap strong')).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByLabelText('Snap weak')).toHaveAttribute('aria-pressed', 'false')
  })

  it('disables undo and redo until there is something to undo', async () => {
    expect(screen.getByLabelText('Undo drawing change')).toBeDisabled()
    expect(screen.getByLabelText('Redo drawing change')).toBeDisabled()

    await createWith('trend_line', [20, 250], [60, 250])
    expect(screen.getByLabelText('Undo drawing change')).toBeEnabled()

    act(() => {
      screen.getByLabelText('Undo drawing change').click()
    })
    expect(engine.drawings).toHaveLength(0)
    expect(screen.getByLabelText('Redo drawing change')).toBeEnabled()

    act(() => {
      screen.getByLabelText('Redo drawing change').click()
    })
    await waitFor(() => expect(engine.drawings).toHaveLength(1))
  })

  it('offers to restore hidden drawings, which are otherwise unreachable', async () => {
    await createWith('trend_line', [20, 250], [60, 250])
    expect(screen.queryByLabelText(/Show \d+ hidden drawings/)).not.toBeInTheDocument()

    act(() => engine.replace({ ...engine.drawings[0], visible: false }))
    const restore = screen.getByLabelText('Show 1 hidden drawings')
    expect(restore).toBeInTheDocument()

    act(() => {
      restore.click()
    })
    expect(engine.drawings[0].visible).toBe(true)
    expect(screen.queryByLabelText(/Show \d+ hidden drawings/)).not.toBeInTheDocument()
  })
})
