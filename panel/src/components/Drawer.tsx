import { useEffect, useId, useRef, type ReactNode } from 'react'
import { X } from 'lucide-react'
import { IconButton } from './Button'
import { useI18n } from '../i18n'

interface DrawerProps {
  open: boolean
  onClose: () => void
  title: ReactNode
  description?: ReactNode
  side?: 'right' | 'left'
  /** Width on desktop (a Tailwind class). */
  widthClass?: string
  children: ReactNode
}

/**
 * Side panel built on a modal <dialog>: the browser handles the focus trap, Esc and making
 * the rest of the page inert. On close, focus returns to the element that opened it.
 */
export function Drawer({
  open,
  onClose,
  title,
  description,
  side = 'right',
  widthClass = 'w-full max-w-full sm:w-[34rem]',
  children,
}: DrawerProps) {
  const { t } = useI18n()
  const ref = useRef<HTMLDialogElement>(null)
  const returnFocus = useRef<HTMLElement | null>(null)
  const onCloseRef = useRef(onClose)
  onCloseRef.current = onClose
  const titleId = useId()
  const descId = useId()

  useEffect(() => {
    const dialog = ref.current
    if (!dialog) return
    if (open && !dialog.open) {
      returnFocus.current = document.activeElement instanceof HTMLElement ? document.activeElement : null
      dialog.showModal()
    } else if (!open && dialog.open) {
      dialog.close()
    }
  }, [open])

  useEffect(() => {
    const dialog = ref.current
    if (!dialog) return
    const handleClose = () => {
      const target = returnFocus.current
      returnFocus.current = null
      if (target && document.contains(target)) target.focus()
      onCloseRef.current()
    }
    dialog.addEventListener('close', handleClose)
    return () => dialog.removeEventListener('close', handleClose)
  }, [])

  const position = side === 'right' ? 'right-0 left-auto drawer-right' : 'left-0 right-auto drawer-left'

  return (
    <dialog
      ref={ref}
      aria-labelledby={titleId}
      aria-describedby={description ? descId : undefined}
      onClick={(event) => {
        // A click on the dimmed backdrop (the <dialog> itself, outside the content) closes it.
        if (event.target === ref.current) ref.current?.close()
      }}
      className={`fixed inset-y-0 m-0 h-dvh max-h-none bg-surface p-0 text-ink shadow-pop ${widthClass} ${position}`}
    >
      {open && (
        <div className="flex h-full flex-col">
          <div className="flex items-start justify-between gap-4 border-b border-line px-5 py-4">
            <div className="min-w-0">
              <h2 id={titleId} className="truncate text-base font-semibold text-ink">
                {title}
              </h2>
              {description && (
                <p id={descId} className="mt-0.5 break-all text-xs text-muted">
                  {description}
                </p>
              )}
            </div>
            <IconButton label={t('common.close')} size="sm" onClick={() => ref.current?.close()}>
              <X className="h-4 w-4" strokeWidth={1.75} aria-hidden="true" />
            </IconButton>
          </div>
          <div className="flex-1 overflow-y-auto px-5 py-5">{children}</div>
        </div>
      )}
    </dialog>
  )
}
