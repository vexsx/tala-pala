import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api, errorMessage, ApiError } from '../../api/client'
import type { ChartDrawing, ChartDrawingsResponse } from '../../api/types'
import type { IntervalId } from '../intervals'
import {
  canRedo as historyCanRedo,
  canUndo as historyCanUndo,
  diffDrawings,
  emptyHistory,
  isPersistedId,
  isValidDraft,
  nextLocalId,
  offsetDrawing,
  parseDrawing,
  parseDrawings,
  pushHistory,
  redoHistory,
  remapHistoryId,
  toRequestBody,
  undoHistory,
  DEFAULT_SNAP_MODE,
  type Drawing,
  type DrawingDraft,
  type DrawingHistory,
  type DrawingType,
  type LineExtend,
  type SnapMode
} from './model'

/**
 * Server-side persistence for one chart's drawings, plus the undo/redo history
 * that sits on top of it.
 *
 * Two invariants shape everything here:
 *
 *  1. The drawing set is scoped strictly to (symbol, interval). Switching either
 *     one clears the set before the new request goes out, so a 300ms fetch can
 *     never paint IR_GOLD_18K's trend lines over XAUUSD's candles.
 *  2. Local state is optimistic and every failure rolls back. A drawing that the
 *     server refused must not stay on screen looking saved.
 */

const BASE = '/chart/drawings'

/**
 * Collapses a burst of edits to one drawing into a single PUT.
 *
 * The layer already keeps a drag in a ref and commits once on pointerup, so in
 * practice a gesture produces one call. This is the belt to that's braces: any
 * caller that patches repeatedly still costs one request per pause, never one
 * per frame.
 */
const WRITE_DEBOUNCE_MS = 250

/** Where a duplicate lands, in bars, so it does not hide under the original. */
const DUPLICATE_OFFSET_BARS = 2

export interface ActiveTool {
  type: DrawingType
  /** trend_line only: line, ray or extended. */
  extend?: LineExtend
}

export interface DrawingEngine {
  drawings: Drawing[]
  loading: boolean
  error: string | null
  /** The chart holds more drawings than one response carries. */
  truncated: boolean
  limit: number
  hiddenCount: number
  tool: ActiveTool | null
  setTool: (tool: ActiveTool | null) => void
  snap: SnapMode
  setSnap: (mode: SnapMode) => void
  selectedId: number | null
  selected: Drawing | null
  select: (id: number | null) => void
  create: (draft: DrawingDraft) => void
  replace: (next: Drawing) => void
  remove: (id: number) => void
  duplicate: (id: number) => void
  showAll: () => void
  undo: () => void
  redo: () => void
  canUndo: boolean
  canRedo: boolean
  reload: () => void
  dismissError: () => void
}

function listPath(symbol: string, interval: IntervalId): string {
  const params = new URLSearchParams({ symbol, interval })
  return `${BASE}?${params.toString()}`
}

export interface UseDrawingsOptions {
  /** Seconds per bucket — only used to offset a duplicate onto free space. */
  intervalSeconds?: number
}

export function useDrawings(
  symbol: string,
  interval: IntervalId,
  options: UseDrawingsOptions = {}
): DrawingEngine {
  const [history, setHistory] = useState<DrawingHistory>(() => emptyHistory())
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [truncated, setTruncated] = useState(false)
  const [limit, setLimit] = useState(0)
  const [selectedId, setSelectedId] = useState<number | null>(null)
  const [tool, setTool] = useState<ActiveTool | null>(null)
  const [snap, setSnap] = useState<SnapMode>(DEFAULT_SNAP_MODE)
  const [reloadTick, setReloadTick] = useState(0)

  // The chart this hook is currently answering for. Every async continuation
  // checks it before touching state: a response for the previous symbol must be
  // dropped, not merged.
  const chartRef = useRef(`${symbol}|${interval}`)
  chartRef.current = `${symbol}|${interval}`

  const historyRef = useRef(history)
  historyRef.current = history

  // Last state the server is known to hold, per id — the rollback target when a
  // write fails. Keyed by id so two drawings in flight cannot clobber each other.
  const baselineRef = useRef(new Map<number, Drawing>())
  // Debounced writes that have not gone out yet, each with the thunk that sends
  // it immediately. Kept so a pending edit can be flushed rather than dropped
  // when the chart changes or the page unmounts.
  const pendingRef = useRef(new Map<number, { timer: number; flush: () => void }>())
  const mountedRef = useRef(true)

  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
    }
  }, [])

  /**
   * Send every debounced write now.
   *
   * Runs when the chart changes and on unmount: an edit made 200ms before the
   * user switched timeframe is still an edit, and dropping the timer would
   * silently discard it. Defined above the load effect because that effect's
   * cleanup is what calls it.
   */
  const flushPending = useCallback(() => {
    const pending = Array.from(pendingRef.current.values())
    pendingRef.current.clear()
    for (const entry of pending) entry.flush()
  }, [])

  const drawings = history.present

  /** Apply a state transition only if the chart has not changed underneath it. */
  const applyHistory = useCallback(
    (chart: string, fn: (h: DrawingHistory) => DrawingHistory) => {
      if (!mountedRef.current || chartRef.current !== chart) return
      setHistory((h) => fn(h))
    },
    []
  )

  // ---- load ---------------------------------------------------------------
  useEffect(() => {
    const chart = `${symbol}|${interval}`
    const ctrl = new AbortController()

    // Clear first, then fetch. Holding the previous chart's drawings while the
    // new ones load would paint one chart's annotations over another's candles.
    setHistory(emptyHistory())
    setSelectedId(null)
    setTruncated(false)
    setLoading(true)
    setError(null)
    baselineRef.current = new Map()

    api<ChartDrawingsResponse>(listPath(symbol, interval), { signal: ctrl.signal })
      .then((res) => {
        if (ctrl.signal.aborted || !mountedRef.current) return
        const parsed = parseDrawings(res?.items)
        for (const d of parsed) baselineRef.current.set(d.id, d)
        // A fresh load is the new floor: undoing past it would delete drawings
        // the user never touched in this session.
        setHistory(emptyHistory(parsed))
        setTruncated(res?.truncated === true)
        setLimit(typeof res?.limit === 'number' ? res.limit : 0)
        setLoading(false)
      })
      .catch((err: unknown) => {
        if (ctrl.signal.aborted || !mountedRef.current) return
        setError(errorMessage(err))
        setLoading(false)
      })

    return () => {
      ctrl.abort()
      flushPending()
    }
  }, [symbol, interval, reloadTick, flushPending])

  const reload = useCallback(() => setReloadTick((t) => t + 1), [])
  const dismissError = useCallback(() => setError(null), [])

  // ---- writes -------------------------------------------------------------

  /** PUT one drawing, rolling back to the last server-known state on failure. */
  const flushUpdate = useCallback(
    (chart: string, id: number) => {
      const current = historyRef.current.present.find((d) => d.id === id)
      if (!current || !isPersistedId(id)) return
      const baseline = baselineRef.current.get(id)
      api<ChartDrawing>(`${BASE}/${id}`, { method: 'PUT', body: toRequestBody(current) })
        .then((res) => {
          if (chartRef.current !== chart) return
          const saved = parseDrawing(res)
          if (saved) baselineRef.current.set(id, saved)
        })
        .catch((err: unknown) => {
          if (chartRef.current !== chart || !mountedRef.current) return
          setError(errorMessage(err))
          if (!baseline) return
          applyHistory(chart, (h) => ({
            ...h,
            present: h.present.map((d) => (d.id === id ? baseline : d))
          }))
        })
    },
    [applyHistory]
  )

  const scheduleUpdate = useCallback(
    (id: number) => {
      const chart = chartRef.current
      const existing = pendingRef.current.get(id)
      if (existing) window.clearTimeout(existing.timer)
      const send = () => {
        pendingRef.current.delete(id)
        flushUpdate(chart, id)
      }
      const timer = window.setTimeout(send, WRITE_DEBOUNCE_MS)
      pendingRef.current.set(id, {
        timer,
        flush: () => {
          window.clearTimeout(timer)
          send()
        }
      })
    },
    [flushUpdate]
  )

  const create = useCallback(
    (draft: DrawingDraft) => {
      if (!isValidDraft(draft)) return
      const chart = chartRef.current
      const localId = nextLocalId()
      const now = new Date().toISOString()
      const optimistic: Drawing = {
        id: localId,
        symbol,
        interval,
        type: draft.type,
        points: draft.points.map((p) => ({ t: Math.round(p.t), price: p.price })),
        style: draft.style,
        locked: false,
        visible: true,
        created_at: now,
        updated_at: now
      }
      applyHistory(chart, (h) => pushHistory(h, h.present.concat([optimistic])))
      setSelectedId(localId)

      api<ChartDrawing>(BASE, { method: 'POST', body: toRequestBody(optimistic) })
        .then((res) => {
          if (chartRef.current !== chart || !mountedRef.current) return
          const saved = parseDrawing(res)
          if (!saved) {
            setError('The server returned a drawing this build cannot read.')
            return
          }
          baselineRef.current.set(saved.id, saved)
          // The row exists now, under a different id. Rewriting the id through
          // the whole history keeps undo and redo pointing at a real row.
          applyHistory(chart, (h) => remapHistoryId(h, localId, saved.id))
          setSelectedId((cur) => (cur === localId ? saved.id : cur))
          // It may have been dragged while the POST was in flight; persist that.
          const live = historyRef.current.present.find((d) => d.id === localId)
          if (live) {
            const moved = live.points.some(
              (p, i) => p.t !== saved.points[i]?.t || p.price !== saved.points[i]?.price
            )
            if (moved) scheduleUpdate(saved.id)
          }
        })
        .catch((err: unknown) => {
          if (chartRef.current !== chart || !mountedRef.current) return
          setError(errorMessage(err))
          // The create never happened, so it must not survive anywhere in the
          // history — including in entries recorded after it.
          applyHistory(chart, (h) => ({
            past: h.past.map((s) => s.filter((d) => d.id !== localId)),
            present: h.present.filter((d) => d.id !== localId),
            future: h.future.map((s) => s.filter((d) => d.id !== localId))
          }))
          setSelectedId((cur) => (cur === localId ? null : cur))
        })
    },
    [applyHistory, interval, scheduleUpdate, symbol]
  )

  const replace = useCallback(
    (next: Drawing) => {
      const chart = chartRef.current
      applyHistory(chart, (h) => {
        if (!h.present.some((d) => d.id === next.id)) return h
        return pushHistory(
          h,
          h.present.map((d) => (d.id === next.id ? next : d))
        )
      })
      // A drawing whose POST has not landed has no row to PUT; the create's own
      // continuation notices the move and writes it.
      if (isPersistedId(next.id)) scheduleUpdate(next.id)
    },
    [applyHistory, scheduleUpdate]
  )

  const remove = useCallback(
    (id: number) => {
      const chart = chartRef.current
      const victim = historyRef.current.present.find((d) => d.id === id)
      if (!victim) return
      applyHistory(chart, (h) => pushHistory(h, h.present.filter((d) => d.id !== id)))
      setSelectedId((cur) => (cur === id ? null : cur))
      // A queued edit to a drawing that is being deleted is moot, and sending it
      // after the DELETE would only race for a 404.
      const pending = pendingRef.current.get(id)
      if (pending) {
        window.clearTimeout(pending.timer)
        pendingRef.current.delete(id)
      }
      if (!isPersistedId(id)) return

      api<null>(`${BASE}/${id}`, { method: 'DELETE' })
        .then(() => {
          baselineRef.current.delete(id)
        })
        .catch((err: unknown) => {
          if (chartRef.current !== chart || !mountedRef.current) return
          // Already gone is the outcome that was wanted, not a failure.
          if (err instanceof ApiError && err.status === 404) {
            baselineRef.current.delete(id)
            return
          }
          setError(errorMessage(err))
          applyHistory(chart, (h) => ({ ...h, present: h.present.concat([victim]) }))
        })
    },
    [applyHistory]
  )

  const duplicate = useCallback(
    (id: number) => {
      const source = historyRef.current.present.find((d) => d.id === id)
      if (!source) return
      const step = (options.intervalSeconds ?? 0) * DUPLICATE_OFFSET_BARS
      const copy = offsetDrawing(source, step)
      create({ type: copy.type, points: copy.points, style: { ...copy.style } })
    },
    [create, options.intervalSeconds]
  )

  /**
   * Move the whole set to `next`, issuing exactly the writes that differ.
   *
   * This is the undo/redo path. A create that is being redone was deleted on the
   * server, so it is POSTed afresh and the new id is threaded back through the
   * history — the alternative is a history full of ids that 404.
   */
  const syncTo = useCallback(
    (chart: string, from: Drawing[], to: Drawing[]) => {
      const diff = diffDrawings(from, to)

      for (const d of diff.deleted) {
        if (!isPersistedId(d.id)) continue
        api<null>(`${BASE}/${d.id}`, { method: 'DELETE' })
          .then(() => baselineRef.current.delete(d.id))
          .catch((err: unknown) => {
            if (err instanceof ApiError && err.status === 404) return
            if (chartRef.current !== chart || !mountedRef.current) return
            setError(errorMessage(err))
            reload()
          })
      }

      for (const d of diff.updated) {
        if (!isPersistedId(d.id)) continue
        api<ChartDrawing>(`${BASE}/${d.id}`, { method: 'PUT', body: toRequestBody(d) })
          .then((res) => {
            const saved = parseDrawing(res)
            if (saved) baselineRef.current.set(saved.id, saved)
          })
          .catch((err: unknown) => {
            if (chartRef.current !== chart || !mountedRef.current) return
            setError(errorMessage(err))
            reload()
          })
      }

      for (const d of diff.created) {
        api<ChartDrawing>(BASE, { method: 'POST', body: toRequestBody(d) })
          .then((res) => {
            if (chartRef.current !== chart || !mountedRef.current) return
            const saved = parseDrawing(res)
            if (!saved) return
            baselineRef.current.set(saved.id, saved)
            applyHistory(chart, (h) => remapHistoryId(h, d.id, saved.id))
            setSelectedId((cur) => (cur === d.id ? saved.id : cur))
          })
          .catch((err: unknown) => {
            if (chartRef.current !== chart || !mountedRef.current) return
            setError(errorMessage(err))
            reload()
          })
      }
    },
    [applyHistory, reload]
  )

  const undo = useCallback(() => {
    const chart = chartRef.current
    const current = historyRef.current
    if (!historyCanUndo(current)) return
    const next = undoHistory(current)
    setHistory(next)
    syncTo(chart, current.present, next.present)
    setSelectedId((cur) => (cur !== null && next.present.some((d) => d.id === cur) ? cur : null))
  }, [syncTo])

  const redo = useCallback(() => {
    const chart = chartRef.current
    const current = historyRef.current
    if (!historyCanRedo(current)) return
    const next = redoHistory(current)
    setHistory(next)
    syncTo(chart, current.present, next.present)
  }, [syncTo])

  const showAll = useCallback(() => {
    const current = historyRef.current
    const hidden = current.present.filter((d) => !d.visible)
    if (hidden.length === 0) return
    setHistory(pushHistory(current, current.present.map((d) => (d.visible ? d : { ...d, visible: true }))))
    for (const d of hidden) {
      if (isPersistedId(d.id)) scheduleUpdate(d.id)
    }
  }, [scheduleUpdate])

  const select = useCallback((id: number | null) => setSelectedId(id), [])

  const selected = useMemo(
    () => (selectedId === null ? null : drawings.find((d) => d.id === selectedId) ?? null),
    [drawings, selectedId]
  )

  const hiddenCount = useMemo(() => drawings.filter((d) => !d.visible).length, [drawings])

  return {
    drawings,
    loading,
    error,
    truncated,
    limit,
    hiddenCount,
    tool,
    setTool,
    snap,
    setSnap,
    selectedId,
    selected,
    select,
    create,
    replace,
    remove,
    duplicate,
    showAll,
    undo,
    redo,
    canUndo: historyCanUndo(history),
    canRedo: historyCanRedo(history),
    reload,
    dismissError
  }
}
