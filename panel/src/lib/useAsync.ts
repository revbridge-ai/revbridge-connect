import { useCallback, useEffect, useRef, useState, type DependencyList } from 'react'
import { isAbort } from './api'

export interface AsyncState<T> {
  data: T | undefined
  error: unknown
  /** First load, with no previous data to show. */
  loading: boolean
  /** Reloading while the previous data stays on screen (the chart keeps its frame). */
  refreshing: boolean
  reload: () => void
  setData: (updater: T | ((prev: T | undefined) => T)) => void
}

/**
 * Fetches data and fetches again when the dependencies change. Cancels the previous call
 * (AbortController) so a stale response never overwrites a newer one.
 */
export function useAsync<T>(fn: (signal: AbortSignal) => Promise<T>, deps: DependencyList): AsyncState<T> {
  const [data, setDataState] = useState<T | undefined>(undefined)
  const [error, setError] = useState<unknown>(null)
  const [pending, setPending] = useState(true)
  const [tick, setTick] = useState(0)
  const fnRef = useRef(fn)
  fnRef.current = fn

  useEffect(() => {
    const controller = new AbortController()
    setPending(true)
    setError(null)
    fnRef
      .current(controller.signal)
      .then((result) => {
        if (!controller.signal.aborted) {
          setDataState(result)
          setPending(false)
        }
      })
      .catch((err: unknown) => {
        if (controller.signal.aborted || isAbort(err)) return
        setError(err)
        setPending(false)
      })
    return () => controller.abort()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, tick])

  const reload = useCallback(() => setTick((n) => n + 1), [])
  const setData = useCallback((updater: T | ((prev: T | undefined) => T)) => {
    setDataState((prev) => (typeof updater === 'function' ? (updater as (p: T | undefined) => T)(prev) : updater))
  }, [])

  return {
    data,
    error,
    loading: pending && data === undefined,
    refreshing: pending && data !== undefined,
    reload,
    setData,
  }
}
