import { useCallback, useEffect, useRef, useState } from 'react'
import { api, errorMessage } from '../api/client'
import type { CustomForecast } from '../api/types'

export const CUSTOM_DAYS_MIN = 1
export const CUSTOM_DAYS_MAX = 90

/** Parse a user-entered day count; null unless a whole number in [1, 90]. */
export function parseCustomDays(raw: string | number): number | null {
  const n = typeof raw === 'number' ? raw : Number.parseInt(raw, 10)
  return Number.isInteger(n) && n >= CUSTOM_DAYS_MIN && n <= CUSTOM_DAYS_MAX ? n : null
}

export interface CustomForecastState {
  result: CustomForecast | null
  loading: boolean
  error: string | null
  /** Fetch /predictions/custom?days=N immediately (cancels any pending run). */
  run: (days: number) => void
  /** Fetch after a short debounce — for typing into the day input. */
  runDebounced: (days: number, delayMs?: number) => void
  reset: () => void
}

/**
 * Shared on-demand custom-horizon forecast fetcher (18k only, 1–90 days).
 * The endpoint computes live and can take seconds; runs are sequenced so a
 * stale response never overwrites a newer one.
 */
export function useCustomForecast(): CustomForecastState {
  const [result, setResult] = useState<CustomForecast | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const ctrl = useRef<AbortController | null>(null)
  const seq = useRef(0)

  useEffect(
    () => () => {
      if (timer.current !== null) clearTimeout(timer.current)
      ctrl.current?.abort()
    },
    []
  )

  const run = useCallback((days: number) => {
    if (timer.current !== null) {
      clearTimeout(timer.current)
      timer.current = null
    }
    ctrl.current?.abort()
    const controller = new AbortController()
    ctrl.current = controller
    const id = ++seq.current
    setLoading(true)
    setError(null)
    api<CustomForecast>(`/predictions/custom?days=${days}`, { signal: controller.signal })
      .then((res) => {
        if (seq.current !== id || controller.signal.aborted) return
        setResult(res)
        setLoading(false)
      })
      .catch((err: unknown) => {
        if (seq.current !== id || controller.signal.aborted) return
        setResult(null)
        setError(errorMessage(err))
        setLoading(false)
      })
  }, [])

  const runDebounced = useCallback(
    (days: number, delayMs = 600) => {
      if (timer.current !== null) clearTimeout(timer.current)
      timer.current = setTimeout(() => {
        timer.current = null
        run(days)
      }, delayMs)
    },
    [run]
  )

  const reset = useCallback(() => {
    if (timer.current !== null) {
      clearTimeout(timer.current)
      timer.current = null
    }
    ctrl.current?.abort()
    seq.current += 1
    setResult(null)
    setLoading(false)
    setError(null)
  }, [])

  return { result, loading, error, run, runDebounced, reset }
}
