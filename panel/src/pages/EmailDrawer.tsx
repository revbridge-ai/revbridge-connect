import { useI18n } from '../i18n'
import { useAsync } from '../lib/useAsync'
import { emailApi, statsApi } from '../lib/endpoints'
import { eventLabel, eventTone, explainDsn, isNotFound } from '../lib/labels'
import type { MessageItem, MessageTimelineEvent } from '../lib/types'
import { Drawer } from '../components/Drawer'
import { Badge, ErrorState, Skeleton } from '../components/Feedback'
import { CodeBlock, CopyButton } from '../components/Copy'
import type { ReactNode } from 'react'

interface Props {
  open: boolean
  onClose: () => void
  /** Message from the list (carries the full timeline). */
  message?: MessageItem | null
  /** Direct lookup by ID (from the POST /send response): no timeline. */
  lookupId?: string | null
}

const dotTone: Record<string, string> = {
  success: 'bg-success',
  danger: 'bg-danger',
  warning: 'bg-warning',
  info: 'bg-info',
  neutral: 'bg-field',
}

function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="grid grid-cols-[8.5rem_1fr] gap-3 py-2 text-sm">
      <dt className="text-muted">{label}</dt>
      <dd className="min-w-0 break-words text-ink">{children}</dd>
    </div>
  )
}

export function EmailDrawer({ open, onClose, message, lookupId }: Props) {
  const { t } = useI18n()
  const messageId = message?.message_id ?? lookupId ?? ''

  return (
    <Drawer open={open} onClose={onClose} title={message?.recipient ?? t('drawer.title')} description={messageId || undefined}>
      {open && messageId && (message ? <MessageDetails message={message} /> : <LookupDetails messageId={messageId} />)}
    </Drawer>
  )
}

function MessageDetails({ message }: { message: MessageItem }) {
  const { t, tMaybe, fmt } = useI18n()
  const status = message.delivery_status
  const last = message.last_event
  return (
    <div className="flex flex-col gap-8">
      <section aria-labelledby="drawer-now" className="flex flex-col gap-2">
        <h3 id="drawer-now" className="text-sm font-semibold text-ink">
          {t('drawer.now')}
        </h3>
        <div className="flex flex-wrap items-center gap-2">
          <Badge tone={eventTone(status)}>{eventLabel(tMaybe, status)}</Badge>
          {last?.timestamp && <span className="text-xs text-muted">{fmt.dateTime(last.timestamp)}</span>}
        </div>
        {tMaybe(`drawer.status.${status}`) && <p className="text-sm leading-relaxed text-muted">{tMaybe(`drawer.status.${status}`)}</p>}
      </section>

      <dl className="divide-y divide-line/70">
        <Row label={t('drawer.recipient')}>{message.recipient}</Row>
        <Row label={t('drawer.subject')}>{message.subject || <span className="text-muted">{t('emails.noSubject')}</span>}</Row>
        {message.sender_email && <Row label={t('drawer.sender')}>{message.sender_email}</Row>}
        <Row label={t('drawer.messageId')}>
          <span className="inline-flex max-w-full items-center gap-1">
            <span className="code break-all">{message.message_id}</span>
            <CopyButton value={message.message_id} what={t('drawer.messageId')} />
          </span>
        </Row>
      </dl>

      <Timeline events={message.timeline} />
      <Headers messageId={message.message_id} rcpt={message.recipient} />
    </div>
  )
}

function Timeline({ events }: { events: MessageTimelineEvent[] }) {
  const { t, tMaybe, fmt } = useI18n()
  return (
    <section aria-labelledby="drawer-timeline" className="flex flex-col gap-3">
      <h3 id="drawer-timeline" className="text-sm font-semibold text-ink">
        {t('drawer.timeline')}
      </h3>
      {events.length === 0 ? (
        <p className="text-sm text-muted">{t('drawer.timelineEmpty')}</p>
      ) : (
        <ol className="relative flex flex-col">
          {events.map((event, index) => {
            const meta = event.metadata ?? {}
            const explanation = explainDsn(tMaybe, meta.dsn)
            const details: { label: string; value: string }[] = []
            if (meta.dsn) details.push({ label: t('drawer.field.dsn'), value: explanation ? `${meta.dsn} — ${explanation}` : meta.dsn })
            if (meta.host) details.push({ label: t('drawer.field.host'), value: meta.host })
            if (meta.click_url) details.push({ label: t('drawer.field.url'), value: meta.click_url })
            if (meta.open_status) details.push({ label: t('drawer.field.open'), value: tMaybe(`open.${meta.open_status}`) ?? meta.open_status })
            if (meta.ip_address) details.push({ label: t('drawer.field.ip'), value: meta.ip_address })
            if (meta.user_agent) details.push({ label: t('drawer.field.agent'), value: meta.user_agent })
            const isLast = index === events.length - 1
            return (
              <li key={`${event.event_type}-${index}`} className="relative flex gap-3 pb-5 last:pb-0">
                {!isLast && <span aria-hidden="true" className="absolute left-[5px] top-4 h-[calc(100%-0.5rem)] w-px bg-line" />}
                <span aria-hidden="true" className={`relative mt-1.5 h-[11px] w-[11px] shrink-0 rounded-full ring-2 ring-surface ${dotTone[eventTone(event.event_type)]}`} />
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-baseline justify-between gap-x-3">
                    <p className="text-sm font-medium text-ink">
                      {eventLabel(tMaybe, event.event_type)}
                      {meta.is_bot && <span className="ml-2 text-xs font-normal text-muted">({t('drawer.bot')})</span>}
                    </p>
                    {event.timestamp && <time className="num text-xs text-muted">{fmt.dateTime(event.timestamp)}</time>}
                  </div>
                  {details.length > 0 && (
                    <dl className="mt-1 flex flex-col gap-0.5 text-xs">
                      {details.map((d) => (
                        <div key={d.label} className="flex gap-2">
                          <dt className="shrink-0 text-muted">{d.label}:</dt>
                          <dd className="min-w-0 break-all text-ink">{d.value}</dd>
                        </div>
                      ))}
                    </dl>
                  )}
                </div>
              </li>
            )
          })}
        </ol>
      )}
    </section>
  )
}

function Headers({ messageId, rcpt }: { messageId: string; rcpt?: string }) {
  const { t } = useI18n()
  const headers = useAsync((signal) => statsApi.messageHeaders(messageId, rcpt, signal), [messageId, rcpt])
  let body: ReactNode
  if (headers.loading) body = <Skeleton className="h-20 w-full" />
  else if (headers.error) {
    body = isNotFound(headers.error) ? (
      <p className="text-sm text-muted">{t('drawer.headersMissing')}</p>
    ) : (
      <ErrorState error={headers.error} onRetry={headers.reload} />
    )
  } else if (headers.data) {
    const entries = Object.entries(headers.data.headers ?? {}).filter(([, v]) => v !== null && v !== '')
    body = (
      <div className="flex flex-col gap-3">
        <dl className="divide-y divide-line/70">
          {entries.map(([key, value]) => (
            <Row key={key} label={key}>
              <span className="code break-all">{Array.isArray(value) ? value.join('\n') : String(value)}</span>
            </Row>
          ))}
        </dl>
        {headers.data.raw && (
          <details className="group">
            <summary className="cursor-pointer text-[0.8125rem] font-medium text-muted hover:text-ink">{t('drawer.headersRaw')}</summary>
            <CodeBlock code={headers.data.raw} label={t('drawer.headers')} className="mt-2" />
          </details>
        )}
      </div>
    )
  }
  return (
    <section aria-labelledby="drawer-headers" className="flex flex-col gap-2">
      <h3 id="drawer-headers" className="text-sm font-semibold text-ink">
        {t('drawer.headers')}
      </h3>
      {body}
    </section>
  )
}

function LookupDetails({ messageId }: { messageId: string }) {
  const { t, tMaybe, fmt } = useI18n()
  // Queue (Redis) and last delivery event; each call can fail on its own.
  const state = useAsync(async (signal) => {
    const [exec, status] = await Promise.allSettled([
      emailApi.executionStatus(messageId, signal),
      emailApi.status(messageId, signal),
    ])
    return {
      exec: exec.status === 'fulfilled' ? exec.value : null,
      status: status.status === 'fulfilled' ? status.value : null,
      statusError: status.status === 'rejected' && !isNotFound(status.reason) ? status.reason : null,
    }
  }, [messageId])

  let now: ReactNode
  if (state.loading) now = <Skeleton className="h-5 w-48" />
  else if (state.error) now = <ErrorState error={state.error} onRetry={state.reload} />
  else if (state.data?.exec?.in_queue) now = <p className="text-sm text-ink">{t('drawer.inQueue')}</p>
  else if (state.data?.status) {
    const s = state.data.status
    const explanation = explainDsn(tMaybe, s.dsn)
    now = (
      <div className="flex flex-col gap-1.5">
        <div className="flex flex-wrap items-center gap-2">
          <Badge tone={eventTone(s.status)}>{eventLabel(tMaybe, s.status)}</Badge>
          {s.ts && <span className="text-xs text-muted">{fmt.dateTime(s.ts)}</span>}
        </div>
        {s.dsn && (
          <p className="text-xs text-muted">
            {t('drawer.field.dsn')}: {s.dsn}
            {explanation ? ` — ${explanation}` : ''}
          </p>
        )}
      </div>
    )
  } else if (state.data?.statusError) now = <ErrorState error={state.data.statusError} onRetry={state.reload} />
  else now = <p className="text-sm text-muted">{t('drawer.noRecord')}</p>

  return (
    <div className="flex flex-col gap-8">
      <section aria-labelledby="lookup-now" className="flex flex-col gap-2">
        <h3 id="lookup-now" className="text-sm font-semibold text-ink">
          {t('drawer.now')}
        </h3>
        {now}
      </section>
      <dl className="divide-y divide-line/70">
        <Row label={t('drawer.messageId')}>
          <span className="inline-flex max-w-full items-center gap-1">
            <span className="code break-all">{messageId}</span>
            <CopyButton value={messageId} what={t('drawer.messageId')} />
          </span>
        </Row>
      </dl>
      <Headers messageId={messageId} />
    </div>
  )
}
