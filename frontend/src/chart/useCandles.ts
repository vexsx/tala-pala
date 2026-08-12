import { useCallback, useEffect, useRef, useState } from 'react'
import { api, errorMessage } from '../api/client'
import type {
  CandleCoverage,
  CandleOverlays,
  ChartCandle,
  ChartCandlesResponse,
  PivotLevels
} from '../api/types'
import { defaultBars, intervalSeconds, type IntervalId } from './intervals'

/** Same cadence the desk has always refreshed at. */
const POLL_MS = 60_000
/** A pan can fire the range callback dozens of times per second. */
const OLDER_DEBOUNCE_MS = 200
/** Newest buckets the live poll asks for — enough to cover a bucket rollover. */
const TAIL_LIMIT = 3

export interface CandleStore {
  candles: ChartCandle[]
  coverage: CandleCoverage | null
  overlays: CandleOverlays | null
  /**
   * Bucket start times the overlay arrays were computed against. Overlays are
   * index-aligned to the response that produced them, and both prepending older
   * pages and appending a rolled-over bucket shift those indices — so the chart
   * plots overlays against these times, not against array position.
   */
  overlayTimes: number[]
  pivots: PivotLevels | null
  support: number | null
  resistance: number | null
  /** First load for the current (symbol, interval). Never true on a refresh. */
  loading: boolean
  loadingOlder: boolean
  error: string | null
  hasMore: boolean
  loadOlder: () => void
  reload: () => void
  asOf: string | null
}

interface Meta {
  coverage: CandleCoverage | null
  overlays: CandleOverlays | null
  overlayTimes: number[]
  pivots: PivotLevels | null
  support: number | null
  resistance: number | null
  asOf: string | null
  hasMore: boolean
  nextBefore: string | null
}

const EMPTY_META: Meta = {
  coverage: null,
  overlays: null,
  overlayTimes: [],
  pivots: null,
  support: null,
  resistance: null,
  asOf: null,
  hasMore: false,
  nextBefore: null
}

function buildPath(
  symbol: string,
  interval: IntervalId,
  limit: number,
  opts: { before?: string | null; overlays: boolean }
): string {
  const params = new URLSearchParams({
    symbol,
    interval,
    limit: String(limit),
    overlays: opts.overlays ? '1' : '0'
  })
  if (opts.before) params.set('before', opts.before)
  return `/market/candles?${params.toString()}`
}

function sortedByTime(candles: ChartCandle[]): ChartCandle[] {
  return candles.slice().sort((a, b) => a.t - b.t)
}

function sameBar(a: ChartCandle, b: ChartCandle): boolean {
  return (
    a.open === b.open &&
    a.high === b.high &&
    a.low === b.low &&
    a.close === b.close &&
    a.ticks === b.ticks &&
    a.confirmed === b.confirmed
  )
}

/** Index of bucket `t` in an ascending array, or -1. */
export function indexOfTime(candles: ChartCandle[], t: number): number {
  let lo = 0
  let hi = candles.length - 1
  while (lo <= hi) {
    const mid = (lo + hi) >> 1
    const v = candles[mid].t
    if (v === t) return mid
    if (v < t) lo = mid + 1
    else hi = mid - 1
  }
  return -1
}

/**
 * A bucket that saw one observation has no traded range: its high and low are
 * just that one print. The server says so with `synthetic`/`ticks`; when an
 * older build omits both, the geometry still gives it away.
 */
export function isSingleObservation(candle: ChartCandle): boolean {
  if (typeof candle.synthetic === 'boolean') return candle.synthetic
  if (typeof candle.ticks === 'number') return candle.ticks <= 1
  return (
    candle.open === candle.high && candle.high === candle.low && candle.low === candle.close
  )
}

export function countSingleObservation(candles: ChartCandle[]): number {
  let n = 0
  for (const c of candles) if (isSingleObservation(c)) n++
  return n
}

/** Prepend older buckets, dropping any the store already holds. */
function mergeOlder(older: ChartCandle[], current: ChartCandle[]): ChartCandle[] {
  if (older.length === 0) return current
  const known = new Set(current.map((c) => c.t))
  const fresh = older.filter((c) => !known.has(c.t))
  if (fresh.length === 0) return current
  return sortedByTime(fresh).concat(current)
}

/**
 * Fold the newest few buckets into the tail: replace the bucket still forming,
 * append one when it rolls over. Returns the original array when nothing moved
 * so React — and therefore the chart — can skip the work entirely.
 */
function patchTail(current: ChartCandle[], fresh: ChartCandle[]): ChartCandle[] {
  if (current.length === 0) return sortedByTime(fresh)
  let out = current
  for (const bar of sortedByTime(fresh)) {
    const last = out[out.length - 1]
    if (bar.t > last.t) {
      if (out === current) out = current.slice()
      out.push(bar)
      continue
    }
    const i = indexOfTime(out, bar.t)
    if (i >= 0 && !sameBar(out[i], bar)) {
      if (out === current) out = current.slice()
      out[i] = bar
    }
  }
  return out
}

export interface UseCandlesOptions {
  /** Live poll cadence; 0 disables the poll (tests, or a paused chart). */
  pollMs?: number
}

/**
 * Paginated candle store.
 *
 * useApi cannot back a chart: it replaces `data` wholesale on every response,
 * which both discards the user's zoom (the chart has to re-set all data) and
 * makes prepending older history impossible. This keeps one ascending, deduped
 * array and mutates only the ends of it.
 */
export function useCandles(
  symbol: string,
  interval: IntervalId,
  options: UseCandlesOptions = {}
): CandleStore {
  const pollMs = options.pollMs ?? POLL_MS

  const [candles, setCandles] = useState<ChartCandle[]>([])
  const [meta, setMeta] = useState<Meta>(EMPTY_META)
  const [loading, setLoading] = useState(true)
  const [loadingOlder, setLoadingOlder] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [reloadTick, setReloadTick] = useState(0)

  // Everything the async callbacks need without re-subscribing on every render.
  const mainCtrlRef = useRef<AbortController | null>(null)
  const olderCtrlRef = useRef<AbortController | null>(null)
  const olderTimerRef = useRef<number | null>(null)
  const olderInFlightRef = useRef(false)
  const requestedCursorRef = useRef<string | null>(null)
  const nextBeforeRef = useRef<string | null>(null)
  const hasMoreRef = useRef(false)
  const readyRef = useRef(false)
  const candlesRef = useRef<ChartCandle[]>([])
  candlesRef.current = candles
  const keyRef = useRef(`${symbol}|${interval}`)

  const reload = useCallback(() => setReloadTick((t) => t + 1), [])

  // ---- newest page -------------------------------------------------------
  useEffect(() => {
    const ctrl = new AbortController()
    mainCtrlRef.current = ctrl

    // A new symbol or timeframe is a different data set, not a refresh: holding
    // the old bars on screen under the new label would show XAUUSD candles with
    // an 18k header for as long as the request takes. A refresh (reload()) keeps
    // its bars, which is what stops the chart blanking every minute.
    const key = `${symbol}|${interval}`
    if (keyRef.current !== key) {
      keyRef.current = key
      setCandles([])
      setMeta(EMPTY_META)
    }

    // A symbol/interval switch invalidates any older page still in flight.
    olderCtrlRef.current?.abort()
    olderCtrlRef.current = null
    olderInFlightRef.current = false
    requestedCursorRef.current = null
    if (olderTimerRef.current !== null) {
      window.clearTimeout(olderTimerRef.current)
      olderTimerRef.current = null
    }
    readyRef.current = false
    hasMoreRef.current = false
    nextBeforeRef.current = null

    setLoading(true)
    setLoadingOlder(false)
    setError(null)

    api<ChartCandlesResponse>(
      buildPath(symbol, interval, defaultBars(interval), { overlays: true }),
      { signal: ctrl.signal }
    )
      .then((res) => {
        if (ctrl.signal.aborted) return
        const next = sortedByTime(res.candles ?? [])
        setCandles(next)
        setMeta({
          coverage: res.coverage ?? null,
          overlays: res.overlays ?? null,
          overlayTimes: next.map((c) => c.t),
          pivots: res.pivots ?? null,
          support: res.support ?? null,
          resistance: res.resistance ?? null,
          asOf: res.as_of ?? null,
          hasMore: res.has_more === true,
          nextBefore: res.next_before ?? null
        })
        hasMoreRef.current = res.has_more === true
        nextBeforeRef.current = res.next_before ?? null
        readyRef.current = true
        setLoading(false)
      })
      .catch((err: unknown) => {
        if (ctrl.signal.aborted) return
        // Keep no stale bars around: an error on a new (symbol, interval) must
        // not leave the previous symbol's candles on screen.
        setCandles([])
        setMeta(EMPTY_META)
        setError(errorMessage(err))
        setLoading(false)
      })

    return () => ctrl.abort()
  }, [symbol, interval, reloadTick])

  // ---- older pages -------------------------------------------------------
  const fetchOlder = useCallback(
    (cursor: string) => {
      const ctrl = new AbortController()
      olderCtrlRef.current = ctrl
      olderInFlightRef.current = true
      requestedCursorRef.current = cursor
      setLoadingOlder(true)

      api<ChartCandlesResponse>(
        // Older pages carry no overlays: they are computed over the requested
        // window only, so a second set would not line up with the first.
        buildPath(symbol, interval, defaultBars(interval), { before: cursor, overlays: false }),
        { signal: ctrl.signal }
      )
        .then((res) => {
          if (ctrl.signal.aborted) return
          const older = res.candles ?? []
          setCandles((current) => mergeOlder(older, current))
          hasMoreRef.current = res.has_more === true && older.length > 0
          nextBeforeRef.current = res.next_before ?? null
          setMeta((m) => ({
            ...m,
            hasMore: hasMoreRef.current,
            nextBefore: nextBeforeRef.current
          }))
          olderInFlightRef.current = false
          setLoadingOlder(false)
        })
        .catch((err: unknown) => {
          if (ctrl.signal.aborted) return
          olderInFlightRef.current = false
          setLoadingOlder(false)
          // Older history failing is not worth blanking a working chart; the
          // cursor is kept so a later pan can retry.
          setError(errorMessage(err))
        })
    },
    [symbol, interval]
  )

  const loadOlder = useCallback(() => {
    if (!readyRef.current || !hasMoreRef.current) return
    if (olderInFlightRef.current || olderTimerRef.current !== null) return
    const cursor = nextBeforeRef.current
    if (!cursor || cursor === requestedCursorRef.current) return
    olderTimerRef.current = window.setTimeout(() => {
      olderTimerRef.current = null
      const latest = nextBeforeRef.current
      if (!latest || olderInFlightRef.current || !hasMoreRef.current) return
      if (latest === requestedCursorRef.current) return
      fetchOlder(latest)
    }, OLDER_DEBOUNCE_MS)
  }, [fetchOlder])

  useEffect(
    () => () => {
      if (olderTimerRef.current !== null) window.clearTimeout(olderTimerRef.current)
      olderCtrlRef.current?.abort()
    },
    []
  )

  // ---- live tail ---------------------------------------------------------
  useEffect(() => {
    if (pollMs <= 0) return
    let ctrl: AbortController | null = null

    const tick = () => {
      if (!readyRef.current || olderInFlightRef.current) return
      ctrl?.abort()
      ctrl = new AbortController()
      const signal = ctrl.signal
      api<ChartCandlesResponse>(
        buildPath(symbol, interval, TAIL_LIMIT, { overlays: false }),
        { signal }
      )
        .then((res) => {
          if (signal.aborted) return
          const fresh = sortedByTime(res.candles ?? [])
          setMeta((m) => (m.asOf === (res.as_of ?? null) ? m : { ...m, asOf: res.as_of ?? null }))
          if (fresh.length === 0) return
          const current = candlesRef.current
          const last = current[current.length - 1]
          if (last && fresh[0].t - last.t > intervalSeconds(interval)) {
            // The tab slept through more buckets than the poll window covers.
            // Stitching these on would leave a hole, so refetch the page.
            reload()
            return
          }
          setCandles((prev) => patchTail(prev, fresh))
        })
        .catch(() => {
          // A missed poll is not an error state; the next one is 60s away.
        })
    }

    const id = window.setInterval(tick, pollMs)
    return () => {
      window.clearInterval(id)
      ctrl?.abort()
    }
  }, [symbol, interval, pollMs, reload])

  return {
    candles,
    coverage: meta.coverage,
    overlays: meta.overlays,
    overlayTimes: meta.overlayTimes,
    pivots: meta.pivots,
    support: meta.support,
    resistance: meta.resistance,
    loading,
    loadingOlder,
    error,
    hasMore: meta.hasMore,
    loadOlder,
    reload,
    asOf: meta.asOf
  }
}
