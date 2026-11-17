import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from 'react'
import { CheckCircle2, AlertTriangle, X } from 'lucide-react'
import { useI18n } from '../i18n'

type ToastTone = 'success' | 'danger'
interface ToastItem {
  id: number
  tone: ToastTone
  message: string
}

type Notify = (message: string, tone?: ToastTone) => void
const ToastContext = createContext<Notify | null>(null)

/** Short confirmation notices. The aria-live region exists from the start so it gets announced. */
export function ToastProvider({ children }: { children: ReactNode }) {
  const { t } = useI18n()
  const [items, setItems] = useState<ToastItem[]>([])
  const seq = useRef(0)
  const timers = useRef(new Map<number, number>())

  const dismiss = useCallback((id: number) => {
    setItems((list) => list.filter((item) => item.id !== id))
    const timer = timers.current.get(id)
    if (timer) window.clearTimeout(timer)
    timers.current.delete(id)
  }, [])

  const notify = useCallback<Notify>(
    (message, tone = 'success') => {
      const id = ++seq.current
      setItems((list) => [...list.slice(-2), { id, tone, message }])
      timers.current.set(id, window.setTimeout(() => dismiss(id), tone === 'danger' ? 8000 : 4500))
    },
    [dismiss],
  )

  useEffect(() => {
    const map = timers.current
    return () => map.forEach((timer) => window.clearTimeout(timer))
  }, [])

  return (
    <ToastContext.Provider value={notify}>
      {children}
      <div
        role="status"
        aria-live="polite"
        className="pointer-events-none fixed inset-x-4 bottom-4 z-50 flex flex-col items-end gap-2 sm:inset-x-auto sm:right-6 sm:w-[24rem]"
      >
        {items.map((item) => (
          <div
            key={item.id}
            className="toast-in pointer-events-auto flex w-full items-start gap-3 rounded-xl border border-line bg-surface p-3.5 text-sm text-ink shadow-pop"
          >
            {item.tone === 'success' ? (
              <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0 text-success-text" strokeWidth={1.75} aria-hidden="true" />
            ) : (
              <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-danger-text" strokeWidth={1.75} aria-hidden="true" />
            )}
            <p className="flex-1 leading-relaxed">{item.message}</p>
            <button
              type="button"
              onClick={() => dismiss(item.id)}
              className="-m-1 inline-flex h-7 w-7 items-center justify-center rounded-md text-muted hover:bg-subtle hover:text-ink"
              aria-label={t('common.close')}
            >
              <X className="h-3.5 w-3.5" strokeWidth={1.75} aria-hidden="true" />
            </button>
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  )
}

export function useToast(): Notify {
  const ctx = useContext(ToastContext)
  if (!ctx) throw new Error('useToast must be used inside <ToastProvider>')
  return ctx
}
