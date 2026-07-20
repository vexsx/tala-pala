import { useCallback, useEffect, useState } from 'react'
import { api, errorMessage } from '../api/client'

export interface ApiState<T> {
  data: T | null
  loading: boolean
  error: string | null
  /** Re-run the request (stable identity). */
  reload: () => void
}

/**
 * Declarative GET hook with AbortController cleanup.
 * Pass `null` as the path to skip fetching.
 */
export function useApi<T>(path: string | null, deps: ReadonlyArray<unknown> = []): ApiState<T> {
  const [data, setData] = useState<T | null>(null)
  const [loading, setLoading] = useState<boolean>(path !== null)
  const [error, setError] = useState<string | null>(null)
  const [tick, setTick] = useState(0)

  const reload = useCallback(() => setTick((t) => t + 1), [])

  useEffect(() => {
    if (path === null) {
      setData(null)
      setLoading(false)
      setError(null)
      return
    }
    const ctrl = new AbortController()
    setLoading(true)
    setError(null)
    api<T>(path, { signal: ctrl.signal })
      .then((result) => {
        if (ctrl.signal.aborted) return
        setData(result)
        setLoading(false)
      })
      .catch((err: unknown) => {
        if (ctrl.signal.aborted) return
        setData(null)
        setError(errorMessage(err))
        setLoading(false)
      })
    return () => ctrl.abort()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path, tick, ...deps])

  return { data, loading, error, reload }
}
