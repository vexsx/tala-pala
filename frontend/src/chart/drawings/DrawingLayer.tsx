import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { ChartCandle } from '../../api/types'
import { useSettings } from '../../lib/settings'
import type { DisplayUnit } from '../../lib/format'
import type { ChartHandle } from '../TradingChart'
import type { IntervalId } from '../intervals'
import {
  createProjector,
  defaultStyle,
  hitHandle,
  hitTest,
  moveDrawing,
  moveHandle,
  snapPoint,
  unproject,
  DRAWING_LABELS,
  DRAWING_POINT_COUNTS,
  MAX_TEXT_LENGTH,
  type Drawing,
  type DrawingPoint,
  type HandleSpec,
  type Projector,
  type Pt
} from './model'
import { paintDraft, paintDrawing, resolveDrawingPalette, type DrawingPalette } from './render'
import type { DrawingEngine } from './useDrawings'

/**
 * The overlay canvas and every pointer gesture that reaches it.
 *
 * Two rules run through the whole file:
 *
 *  - No React state per mousemove. A gesture in flight lives in `gestureRef`
 *    and is painted straight onto the canvas; React only hears about it once,
 *    on pointerup. That is also what makes "one PUT per gesture" structural
 *    rather than a debounce that hopes for the best.
 *  - The canvas never swallows a gesture it does not need. It is
 *    pointer-events:none, and the listeners sit on the chart container in the
 *    CAPTURE phase — so this layer sees a pointerdown before lightweight-charts
 *    does and can either claim it (stopPropagation) or leave it alone, in which
 *    case the chart pans and zooms exactly as it did before the layer existed.
 */

export interface DrawingLayerProps {
  /** Null until TradingChart's onReady fires; the layer simply paints nothing. */
  handle: ChartHandle | null
  engine: DrawingEngine
  symbol: string
  interval: IntervalId
  unit: DisplayUnit
  candles: ChartCandle[]
}

/** Pixel slack for grabbing a 1px line — anything less is a line you can see but not hold. */
const TOLERANCE_MOUSE = 6
/** A fingertip is not a mouse pointer. */
const TOLERANCE_TOUCH = 14

/** The in-progress drawing has no id: 0 is neither a server id nor a local one. */
const DRAFT_ID = 0

type GestureKind = 'create' | 'move' | 'handle'

interface Gesture {
  kind: GestureKind
  pointerId: number
  /** What is painted this frame. */
  draft: Drawing
  /** The drawing as it was when grabbed — every delta is applied to this, never accumulated. */
  origin: Drawing
  /** Pointer anchor at grab time, in data space. */
  grab: DrawingPoint
  handle: HandleSpec | null
  moved: boolean
}

function pointerPos(e: PointerEvent, container: HTMLElement): Pt {
  const rect = container.getBoundingClientRect()
  return { x: e.clientX - rect.left, y: e.clientY - rect.top }
}

/** True for a target that owns its own clicks — buttons, inputs, the Fit/Latest bar. */
function isInteractiveTarget(target: EventTarget | null): boolean {
  if (!(target instanceof Element)) return false
  return target.closest('button, input, select, textarea, a, .tchart-actions, .drawing-bar') !== null
}

function isTypingTarget(target: EventTarget | null): boolean {
  if (!(target instanceof Element)) return false
  const tag = target.tagName
  if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return true
  return (target as HTMLElement).isContentEditable === true
}

export function DrawingLayer({
  handle,
  engine,
  symbol,
  interval,
  unit,
  candles
}: DrawingLayerProps) {
  const { calendar } = useSettings()
  const canvasRef = useRef<HTMLCanvasElement | null>(null)
  const gestureRef = useRef<Gesture | null>(null)
  const hoveredRef = useRef<number | null>(null)
  const frameRef = useRef<number | null>(null)
  const queuedRef = useRef(false)
  const paletteRef = useRef<DrawingPalette | null>(null)
  const [textDraft, setTextDraft] = useState<string | null>(null)

  const times = useMemo(() => candles.map((c) => c.t), [candles])

  // Fresh props for handlers that subscribe once. Same shape as TradingChart's
  // `latest` ref, and for the same reason: these listeners must not be torn down
  // and rebuilt every time a candle arrives.
  const latest = useRef({ handle, engine, symbol, interval, unit, candles, times, calendar })
  useEffect(() => {
    latest.current = { handle, engine, symbol, interval, unit, candles, times, calendar }
  })

  // ---- painting ----------------------------------------------------------

  const paint = useCallback(() => {
    const canvas = canvasRef.current
    const state = latest.current
    const h = state.handle
    if (!canvas || !h) return
    const ctx = canvas.getContext('2d')
    if (!ctx) return

    const rect = h.container.getBoundingClientRect()
    const width = rect.width
    const height = rect.height
    const dpr = window.devicePixelRatio || 1
    const wantW = Math.max(1, Math.round(width * dpr))
    const wantH = Math.max(1, Math.round(height * dpr))
    if (canvas.width !== wantW || canvas.height !== wantH) {
      canvas.width = wantW
      canvas.height = wantH
      canvas.style.width = `${width}px`
      canvas.style.height = `${height}px`
    }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
    ctx.clearRect(0, 0, width, height)
    if (width === 0 || height === 0) return

    if (!paletteRef.current) paletteRef.current = resolveDrawingPalette()
    const proj = createProjector(h, state.times)
    const gesture = gestureRef.current
    const options = {
      ctx,
      proj,
      view: { width, height },
      palette: paletteRef.current,
      symbol: state.symbol,
      unit: state.unit,
      calendar: state.calendar,
      times: state.times,
      selected: false,
      hovered: false
    }

    for (const d of state.engine.drawings) {
      if (!d.visible) continue
      // The drawing under an active gesture is painted from the draft instead,
      // so it does not appear twice — once where it was, once where it is.
      if (gesture && gesture.draft.id === d.id) continue
      paintDrawing(d, {
        ...options,
        selected: d.id === state.engine.selectedId,
        hovered: d.id === hoveredRef.current
      })
    }
    if (gesture) paintDraft(gesture.draft, options)
  }, [])

  /**
   * Coalesce repaints to one per frame.
   *
   * The "already queued" latch is a separate flag rather than the frame id
   * itself: a synchronous requestAnimationFrame (a polyfill, or a test that
   * wants deterministic paints) runs the callback before the id is assigned, so
   * an id-based latch would be set by the very call that was meant to clear it
   * and no repaint would ever be scheduled again.
   */
  const schedule = useCallback(() => {
    if (queuedRef.current) return
    queuedRef.current = true
    frameRef.current = window.requestAnimationFrame(() => {
      queuedRef.current = false
      frameRef.current = null
      paint()
    })
  }, [paint])

  // Repaint whenever anything React knows about changes the picture.
  useEffect(() => {
    schedule()
  }, [schedule, engine.drawings, engine.selectedId, times, unit, calendar, symbol, handle])

  useEffect(
    () => () => {
      if (frameRef.current !== null) window.cancelAnimationFrame(frameRef.current)
      frameRef.current = null
      queuedRef.current = false
    },
    []
  )

  // Viewport: pan, zoom, resize and data change all land here.
  useEffect(() => {
    if (!handle) return
    const unsubscribe = handle.onViewportChange(schedule)
    const observer = new ResizeObserver(schedule)
    observer.observe(handle.container)
    return () => {
      unsubscribe()
      observer.disconnect()
    }
  }, [handle, schedule])

  // Theme flip: re-resolve the palette, then repaint. Colours are never baked
  // into a drawing, so this is all it takes.
  useEffect(() => {
    const observer = new MutationObserver(() => {
      paletteRef.current = resolveDrawingPalette()
      schedule()
    })
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] })
    return () => observer.disconnect()
  }, [schedule])

  // ---- gesture helpers ---------------------------------------------------

  const projector = useCallback((): Projector | null => {
    const h = latest.current.handle
    return h ? createProjector(h, latest.current.times) : null
  }, [])

  const toAnchor = useCallback((p: Pt, proj: Projector, snap = true): DrawingPoint => {
    const raw = unproject(p, proj)
    if (!snap) return raw
    return snapPoint(raw, {
      candles: latest.current.candles,
      mode: latest.current.engine.snap,
      proj
    })
  }, [])

  const endGesture = useCallback(() => {
    gestureRef.current = null
    schedule()
  }, [schedule])

  /** Drop an in-progress gesture without committing anything. */
  const cancelGesture = useCallback(() => {
    if (!gestureRef.current) return false
    endGesture()
    return true
  }, [endGesture])

  // ---- pointer interaction ------------------------------------------------

  useEffect(() => {
    const container = handle?.container
    if (!container) return

    const tolerance = (e: PointerEvent) =>
      e.pointerType === 'touch' ? TOLERANCE_TOUCH : TOLERANCE_MOUSE

    /** Top-most visible drawing under the pointer; later in the array is later painted. */
    const pick = (p: Pt, proj: Projector, tol: number): Drawing | null => {
      const list = latest.current.engine.drawings
      for (let i = list.length - 1; i >= 0; i--) {
        const d = list[i]
        if (!d.visible) continue
        if (hitTest(d, p, { proj, tolerance: tol })) return d
      }
      return null
    }

    const onMove = (e: PointerEvent) => {
      const gesture = gestureRef.current
      if (!gesture || e.pointerId !== gesture.pointerId) return
      const proj = projector()
      if (!proj) return
      const p = pointerPos(e, container)
      gesture.moved = true

      if (gesture.kind === 'create') {
        const next = gesture.draft.points.map((pt) => ({ ...pt }))
        next[next.length - 1] = toAnchor(p, proj)
        gesture.draft = { ...gesture.draft, points: next }
      } else if (gesture.kind === 'handle' && gesture.handle) {
        gesture.draft = moveHandle(gesture.origin, gesture.handle, toAnchor(p, proj))
      } else {
        const now = unproject(p, proj)
        const shifted = moveDrawing(
          gesture.origin,
          now.t - gesture.grab.t,
          now.price - gesture.grab.price
        )
        // Snap the drawing as a rigid body: correct by whatever the first anchor
        // moved, so a snapped trend line keeps its length and angle exactly.
        const snapped = snapPoint(shifted.points[0], {
          candles: latest.current.candles,
          mode: latest.current.engine.snap,
          proj
        })
        gesture.draft = moveDrawing(
          shifted,
          snapped.t - shifted.points[0].t,
          snapped.price - shifted.points[0].price
        )
      }
      schedule()
    }

    const onUp = (e: PointerEvent) => {
      const gesture = gestureRef.current
      if (!gesture || e.pointerId !== gesture.pointerId) return
      const engineNow = latest.current.engine

      if (gesture.kind === 'create') {
        const needed = DRAWING_POINT_COUNTS[gesture.draft.type]
        // A click with no drag would make a zero-size box or a zero-length line:
        // a drawing that cannot be seen and can never be grabbed again.
        if (needed > 1 && !gesture.moved) {
          endGesture()
          return
        }
        engineNow.create({
          type: gesture.draft.type,
          points: gesture.draft.points,
          style: gesture.draft.style
        })
        engineNow.setTool(null)
      } else if (gesture.moved) {
        engineNow.replace(gesture.draft)
      }
      endGesture()
    }

    const detach = () => {
      window.removeEventListener('pointermove', onMove, true)
      window.removeEventListener('pointerup', onUp, true)
      window.removeEventListener('pointercancel', onCancel, true)
    }

    const onCancel = (e: PointerEvent) => {
      const gesture = gestureRef.current
      if (!gesture || e.pointerId !== gesture.pointerId) return
      cancelGesture()
      detach()
    }

    const attach = () => {
      window.addEventListener('pointermove', onMove, true)
      window.addEventListener('pointerup', onUp, true)
      window.addEventListener('pointercancel', onCancel, true)
    }

    const onDown = (e: PointerEvent) => {
      if (e.button !== undefined && e.button !== 0) return
      if (isInteractiveTarget(e.target)) return
      const proj = projector()
      if (!proj) return
      const state = latest.current
      const engineNow = state.engine
      const p = pointerPos(e, container)
      const tol = tolerance(e)

      // 1. A tool is armed: start drawing, and keep the chart from panning.
      const tool = engineNow.tool
      if (tool) {
        const anchor = toAnchor(p, proj)
        const count = DRAWING_POINT_COUNTS[tool.type]
        const style = defaultStyle(tool.type)
        if (tool.extend) style.extend = tool.extend
        const draft: Drawing = {
          id: DRAFT_ID,
          symbol: state.symbol,
          interval: state.interval,
          type: tool.type,
          points: count === 1 ? [anchor] : [anchor, { ...anchor }],
          style,
          locked: false,
          visible: true,
          created_at: '',
          updated_at: ''
        }
        gestureRef.current = {
          kind: 'create',
          pointerId: e.pointerId,
          draft,
          origin: draft,
          grab: anchor,
          handle: null,
          moved: false
        }
        e.stopPropagation()
        e.preventDefault()
        attach()
        schedule()
        return
      }

      // 2. A handle of the current selection, which sits on top of everything.
      const selected = engineNow.selected
      if (selected && !selected.locked && selected.visible) {
        const grabbed = hitHandle(selected, p, { proj, tolerance: tol })
        if (grabbed) {
          gestureRef.current = {
            kind: 'handle',
            pointerId: e.pointerId,
            draft: selected,
            origin: selected,
            grab: unproject(p, proj),
            handle: grabbed,
            moved: false
          }
          e.stopPropagation()
          e.preventDefault()
          attach()
          return
        }
      }

      // 3. A drawing body: select it, and drag it unless it is locked.
      const hit = pick(p, proj, tol)
      if (hit) {
        engineNow.select(hit.id)
        e.stopPropagation()
        e.preventDefault()
        if (!hit.locked) {
          gestureRef.current = {
            kind: 'move',
            pointerId: e.pointerId,
            draft: hit,
            origin: hit,
            grab: unproject(p, proj),
            handle: null,
            moved: false
          }
          attach()
        }
        schedule()
        return
      }

      // 4. Nothing of ours. Clear the selection and let the chart have the drag.
      if (engineNow.selectedId !== null) engineNow.select(null)
    }

    /** Hover feedback only — never claims the event, never touches React state. */
    const onHover = (e: PointerEvent) => {
      if (gestureRef.current) return
      const engineNow = latest.current.engine
      const proj = projector()
      if (!proj) {
        return
      }
      const p = pointerPos(e, container)
      const tol = tolerance(e)
      const selected = engineNow.selected
      const overHandle =
        selected && !selected.locked && selected.visible
          ? hitHandle(selected, p, { proj, tolerance: tol })
          : null
      const hit = overHandle ? selected : pick(p, proj, tol)
      const nextId = hit ? hit.id : null
      if (nextId !== hoveredRef.current) {
        hoveredRef.current = nextId
        schedule()
      }
      container.style.cursor = engineNow.tool
        ? 'crosshair'
        : overHandle
          ? 'grab'
          : hit
            ? 'move'
            : ''
    }

    container.addEventListener('pointerdown', onDown, true)
    container.addEventListener('pointermove', onHover, true)
    return () => {
      container.removeEventListener('pointerdown', onDown, true)
      container.removeEventListener('pointermove', onHover, true)
      detach()
      container.style.cursor = ''
    }
  }, [handle, projector, toAnchor, schedule, endGesture, cancelGesture])

  // ---- keyboard -----------------------------------------------------------

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      // Never steal a key from a field the user is typing in — including this
      // layer's own text input.
      if (isTypingTarget(e.target)) return
      const engineNow = latest.current.engine

      if (e.key === 'Escape') {
        if (cancelGesture()) {
          e.preventDefault()
          return
        }
        if (engineNow.tool) {
          engineNow.setTool(null)
          e.preventDefault()
          return
        }
        if (engineNow.selectedId !== null) engineNow.select(null)
        return
      }

      if (e.key === 'Delete' || e.key === 'Backspace') {
        const selected = engineNow.selected
        if (!selected || selected.locked) return
        e.preventDefault()
        engineNow.remove(selected.id)
        return
      }

      const mod = e.metaKey || e.ctrlKey
      if (!mod) return
      const key = e.key.toLowerCase()
      if (key === 'z') {
        e.preventDefault()
        if (e.shiftKey) engineNow.redo()
        else engineNow.undo()
      } else if (key === 'y') {
        e.preventDefault()
        engineNow.redo()
      }
    }

    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [cancelGesture])

  // ---- selection bar ------------------------------------------------------

  const selected = engine.selected
  const selectedText = selected?.style.text ?? ''

  useEffect(() => {
    setTextDraft(null)
  }, [engine.selectedId])

  const commitText = useCallback(() => {
    if (textDraft === null || !selected) return
    if (textDraft !== selectedText) {
      engine.replace({ ...selected, style: { ...selected.style, text: textDraft } })
    }
    setTextDraft(null)
  }, [engine, selected, selectedText, textDraft])

  return (
    <>
      <canvas
        ref={canvasRef}
        className="drawing-layer"
        // The pointer surface is the chart container, not this element; the
        // canvas is a picture, and the selection bar below is what assistive
        // technology reads and operates.
        aria-hidden="true"
      />

      <div className="drawing-status" role="status">
        {engine.truncated && (
          <p className="drawing-note">
            Showing the first {engine.limit} drawings on this chart — there are more than one
            response carries.
          </p>
        )}
        {engine.error && (
          <p className="drawing-note drawing-note-error">
            {engine.error}{' '}
            <button
              type="button"
              className="btn btn-ghost btn-sm"
              aria-label="Dismiss drawing error"
              onClick={engine.dismissError}
            >
              Dismiss
            </button>
          </p>
        )}
      </div>

      {selected && (
        <div className="drawing-bar" role="group" aria-label="Selected drawing">
          <span className="drawing-bar-name">{DRAWING_LABELS[selected.type]}</span>
          {selected.locked && <span className="badge badge-off">Locked</span>}
          {!selected.visible && <span className="badge badge-off">Hidden</span>}

          {selected.type === 'text' && (
            <input
              className="drawing-text-input"
              type="text"
              maxLength={MAX_TEXT_LENGTH}
              aria-label="Drawing label text"
              value={textDraft ?? selectedText}
              onChange={(e) => setTextDraft(e.target.value)}
              onBlur={commitText}
              onKeyDown={(e) => {
                if (e.key === 'Enter') {
                  e.preventDefault()
                  commitText()
                } else if (e.key === 'Escape') {
                  e.preventDefault()
                  setTextDraft(null)
                }
              }}
            />
          )}

          <button
            type="button"
            className={`btn btn-sm ${selected.locked ? '' : 'btn-ghost'}`}
            aria-label={selected.locked ? 'Unlock drawing' : 'Lock drawing'}
            aria-pressed={selected.locked}
            onClick={() => engine.replace({ ...selected, locked: !selected.locked })}
          >
            {selected.locked ? 'Unlock' : 'Lock'}
          </button>
          <button
            type="button"
            className={`btn btn-sm ${selected.visible ? 'btn-ghost' : ''}`}
            aria-label={selected.visible ? 'Hide drawing' : 'Show drawing'}
            aria-pressed={!selected.visible}
            onClick={() => engine.replace({ ...selected, visible: !selected.visible })}
          >
            {selected.visible ? 'Hide' : 'Show'}
          </button>
          <button
            type="button"
            className="btn btn-sm btn-ghost"
            aria-label="Duplicate drawing"
            onClick={() => engine.duplicate(selected.id)}
          >
            Duplicate
          </button>
          <button
            type="button"
            className="btn btn-sm btn-ghost"
            aria-label="Delete drawing"
            disabled={selected.locked}
            onClick={() => engine.remove(selected.id)}
          >
            Delete
          </button>
        </div>
      )}
    </>
  )
}
