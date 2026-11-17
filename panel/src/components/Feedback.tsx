import type { ReactNode } from 'react'
import { AlertTriangle, Info } from 'lucide-react'
import { Button } from './Button'
import { useI18n } from '../i18n'
import { errorMessage, type Tone } from '../lib/labels'

const badgeTones: Record<Tone, string> = {
  success: 'bg-success/10 text-success-text',
  danger: 'bg-danger/10 text-danger-text',
  warning: 'bg-warning/15 text-warning-text',
  info: 'bg-info/10 text-info-text',
  neutral: 'bg-subtle text-muted',
}

const dotTones: Record<Tone, string> = {
  success: 'bg-success',
  danger: 'bg-danger',
  warning: 'bg-warning',
  info: 'bg-info',
  neutral: 'bg-field',
}

export function Badge({ tone = 'neutral', children, dot = true }: { tone?: Tone; children: ReactNode; dot?: boolean }) {
  return (
    <span
      className={`inline-flex max-w-full items-center gap-1.5 whitespace-nowrap rounded-full px-2.5 py-0.5 text-xs font-medium ${badgeTones[tone]}`}
    >
      {dot && <span aria-hidden="true" className={`h-1.5 w-1.5 shrink-0 rounded-full ${dotTones[tone]}`} />}
      {children}
    </span>
  )
}

export function Skeleton({ className = '' }: { className?: string }) {
  return <span aria-hidden="true" className={`block animate-pulse rounded-md bg-subtle ${className}`} />
}

/** Skeleton rows for tables while they load. */
export function SkeletonRows({ rows = 5, cols = 4 }: { rows?: number; cols?: number }) {
  return (
    <>
      {Array.from({ length: rows }).map((_, r) => (
        <tr key={r} className="border-t border-line/70">
          {Array.from({ length: cols }).map((__, c) => (
            <td key={c} className="px-4 py-3.5">
              <Skeleton className={`h-3.5 ${c === 0 ? 'w-28' : c === cols - 1 ? 'w-16' : 'w-40'}`} />
            </td>
          ))}
        </tr>
      ))}
    </>
  )
}

interface EmptyStateProps {
  icon?: ReactNode
  title: string
  body?: ReactNode
  actions?: ReactNode
  compact?: boolean
}

export function EmptyState({ icon, title, body, actions, compact }: EmptyStateProps) {
  return (
    <div className={`flex flex-col items-start gap-3 ${compact ? 'py-6' : 'py-12'}`}>
      {icon && (
        <span className="inline-flex h-10 w-10 items-center justify-center rounded-xl bg-subtle text-muted" aria-hidden="true">
          {icon}
        </span>
      )}
      <div className="flex max-w-lg flex-col gap-1">
        <h3 className="text-base font-semibold text-ink">{title}</h3>
        {body && <p className="text-sm leading-relaxed text-muted">{body}</p>}
      </div>
      {actions && <div className="flex flex-wrap gap-2 pt-1">{actions}</div>}
    </div>
  )
}

/** Load failure: states the problem and offers to try again. */
export function ErrorState({ error, onRetry, what }: { error: unknown; onRetry?: () => void; what?: string }) {
  const { t } = useI18n()
  return (
    <div role="alert" className="flex flex-col items-start gap-3 rounded-xl border border-danger/30 bg-danger/5 p-4 sm:flex-row sm:items-center">
      <AlertTriangle className="h-5 w-5 shrink-0 text-danger-text" strokeWidth={1.75} aria-hidden="true" />
      <div className="flex-1 text-sm">
        {what && <p className="font-medium text-ink">{t('errors.load', { what })}</p>}
        <p className="text-muted">{errorMessage(t, error)}</p>
      </div>
      {onRetry && (
        <Button variant="outline" size="sm" onClick={onRetry}>
          {t('common.retry')}
        </Button>
      )}
    </div>
  )
}

export function Callout({ tone = 'info', children, title }: { tone?: 'info' | 'warning'; children: ReactNode; title?: string }) {
  const styles = tone === 'warning' ? 'border-warning/40 bg-warning/10' : 'border-info/30 bg-info/5'
  const iconTone = tone === 'warning' ? 'text-warning-text' : 'text-info-text'
  const Icon = tone === 'warning' ? AlertTriangle : Info
  return (
    <div className={`flex gap-3 rounded-xl border p-4 text-sm ${styles}`}>
      <Icon className={`mt-0.5 h-4 w-4 shrink-0 ${iconTone}`} strokeWidth={1.75} aria-hidden="true" />
      <div className="flex flex-col gap-1 leading-relaxed text-ink">
        {title && <p className="font-medium">{title}</p>}
        <div className="text-muted">{children}</div>
      </div>
    </div>
  )
}

interface InlineConfirmProps {
  message: ReactNode
  confirmLabel: string
  onConfirm: () => void
  onCancel: () => void
  busy?: boolean
  tone?: 'danger' | 'primary'
}

/** In-place confirmation, no modal: the sentence states the effect, the buttons name the action. */
export function InlineConfirm({ message, confirmLabel, onConfirm, onCancel, busy, tone = 'danger' }: InlineConfirmProps) {
  const { t } = useI18n()
  return (
    <div
      role="group"
      className="flex flex-col gap-3 rounded-xl border border-line bg-subtle/60 p-3 text-sm sm:flex-row sm:items-center sm:justify-between"
    >
      <p className="text-ink">{message}</p>
      <div className="flex shrink-0 gap-2">
        <Button variant="ghost" size="sm" onClick={onCancel} disabled={busy}>
          {t('common.cancel')}
        </Button>
        <Button variant={tone === 'danger' ? 'danger' : 'primary'} size="sm" onClick={onConfirm} loading={busy} autoFocus>
          {confirmLabel}
        </Button>
      </div>
    </div>
  )
}
