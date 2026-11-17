import { useState, type FormEvent } from 'react'
import { ChevronDown, Plus, Webhook as WebhookIcon } from 'lucide-react'
import { useI18n } from '../i18n'
import { useAsync } from '../lib/useAsync'
import { webhooksApi } from '../lib/endpoints'
import { errorMessage } from '../lib/labels'
import type { Webhook } from '../lib/types'
import type { MessageKey } from '../i18n/pt'
import { PageHeader, TableRegion, td, th, tr } from '../components/Layout'
import { Badge, EmptyState, ErrorState, InlineConfirm, Skeleton, SkeletonRows } from '../components/Feedback'
import { Button } from '../components/Button'
import { Checkbox, Field, Switch, TextInput, describedBy } from '../components/Form'
import { CopyButton } from '../components/Copy'
import { useToast } from '../components/Toast'

// Email events the panel offers (WebhookEventType in models/webhook_models.py).
const EVENTS = ['delivered', 'bounced', 'deferred', 'opened', 'clicked', 'unsubscribed', 'complaint', 'sent', 'test'] as const
const DEFAULT_EVENTS = ['delivered', 'bounced', 'opened', 'clicked', 'unsubscribed', 'test']

function randomSecret(): string {
  const bytes = new Uint8Array(32)
  crypto.getRandomValues(bytes)
  return Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('')
}

function isHttpUrl(value: string): boolean {
  try {
    const url = new URL(value)
    return url.protocol === 'https:' || url.protocol === 'http:'
  } catch {
    return false
  }
}

export function WebhooksPage() {
  const { t } = useI18n()
  const hooks = useAsync((signal) => webhooksApi.list(signal), [])
  const [formOpen, setFormOpen] = useState(false)
  const list = hooks.data ?? []
  const replace = (next: Webhook) => hooks.setData((prev) => (prev ?? []).map((h) => (h.id === next.id ? next : h)))

  return (
    <>
      <PageHeader
        title={t('wh.title')}
        description={t('wh.subtitle')}
        actions={
          !formOpen ? (
            <Button onClick={() => setFormOpen(true)} icon={<Plus className="h-4 w-4" strokeWidth={2} aria-hidden="true" />}>
              {t('wh.create')}
            </Button>
          ) : undefined
        }
      />

      {formOpen && (
        <div className="mb-8">
          <WebhookForm
            onCancel={() => setFormOpen(false)}
            onSaved={() => {
              setFormOpen(false)
              hooks.reload()
            }}
          />
        </div>
      )}

      {hooks.error ? (
        <ErrorState error={hooks.error} onRetry={hooks.reload} what={t('wh.title').toLowerCase()} />
      ) : hooks.loading ? (
        <div className="flex flex-col gap-4">
          <Skeleton className="h-32 w-full rounded-xl" />
          <Skeleton className="h-32 w-full rounded-xl" />
        </div>
      ) : list.length === 0 ? (
        <EmptyState
          icon={<WebhookIcon className="h-5 w-5" strokeWidth={1.5} />}
          title={t('wh.empty')}
          body={t('wh.empty.body')}
          actions={
            !formOpen ? (
              <Button onClick={() => setFormOpen(true)} icon={<Plus className="h-4 w-4" strokeWidth={2} aria-hidden="true" />}>
                {t('wh.create')}
              </Button>
            ) : undefined
          }
        />
      ) : (
        <ul className="flex flex-col gap-4">
          {list.map((hook) => (
            <li key={hook.id}>
              <WebhookCard hook={hook} onChange={replace} onRemoved={hooks.reload} />
            </li>
          ))}
        </ul>
      )}
    </>
  )
}

function WebhookForm({ hook, onCancel, onSaved }: { hook?: Webhook; onCancel: () => void; onSaved: (hook: Webhook) => void }) {
  const { t } = useI18n()
  const notify = useToast()
  const editing = !!hook
  const prefix = hook ? `wh-${hook.id}` : 'wh-new'
  const [name, setName] = useState(hook?.name ?? '')
  const [url, setUrl] = useState(hook?.url ?? '')
  const [events, setEvents] = useState<string[]>(hook?.events ?? DEFAULT_EVENTS)
  const [secret, setSecret] = useState('')
  const [errors, setErrors] = useState<{ name?: string; url?: string; events?: string }>({})
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const toggleEvent = (event: string, on: boolean) =>
    setEvents((prev) => (on ? Array.from(new Set([...prev, event])) : prev.filter((e) => e !== event)))

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    const next: typeof errors = {}
    if (!name.trim()) next.name = t('wh.form.nameRequired')
    if (!isHttpUrl(url.trim())) next.url = t('wh.form.urlInvalid')
    if (events.length === 0) next.events = t('wh.form.eventsRequired')
    setErrors(next)
    setError(null)
    if (next.name || next.url || next.events) return
    setBusy(true)
    try {
      const saved = hook
        ? await webhooksApi.update(hook.id, {
            name: name.trim(),
            url: url.trim(),
            events,
            ...(secret.trim() ? { secret_key: secret.trim() } : {}),
          })
        : await webhooksApi.create({
            name: name.trim(),
            url: url.trim(),
            events,
            secret_key: secret.trim() || null,
            service_type: 'email',
          })
      notify(t(editing ? 'wh.toast.saved' : 'wh.toast.created'))
      onSaved(saved)
    } catch (err) {
      setError(errorMessage(t, err))
      setBusy(false)
    }
  }

  return (
    <form
      onSubmit={submit}
      noValidate
      aria-labelledby={`${prefix}-title`}
      className="flex flex-col gap-5 rounded-xl border border-line p-5 sm:p-6"
    >
      <h2 id={`${prefix}-title`} className="text-base font-semibold text-ink">
        {editing ? t('wh.form.edit') : t('wh.form.new')}
      </h2>
      <div className="grid gap-4 md:grid-cols-2">
        <Field id={`${prefix}-name`} label={t('wh.form.name')} error={errors.name}>
          <TextInput
            id={`${prefix}-name`}
            value={name}
            onChange={(e) => setName(e.target.value)}
            maxLength={255}
            invalid={!!errors.name}
            aria-describedby={describedBy(`${prefix}-name`, undefined, errors.name)}
            autoFocus={!editing}
          />
        </Field>
        <Field id={`${prefix}-url`} label={t('wh.form.url')} hint={t('wh.form.url.hint')} error={errors.url}>
          <TextInput
            id={`${prefix}-url`}
            type="url"
            inputMode="url"
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            placeholder="https://"
            invalid={!!errors.url}
            aria-describedby={describedBy(`${prefix}-url`, t('wh.form.url.hint'), errors.url)}
            className="code"
          />
        </Field>
      </div>

      <fieldset aria-describedby={errors.events ? `${prefix}-events-error` : undefined}>
        <legend className="mb-2 text-[0.8125rem] font-medium text-ink">{t('wh.form.events')}</legend>
        <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
          {EVENTS.map((ev) => (
            <Checkbox
              key={ev}
              id={`${prefix}-ev-${ev}`}
              label={t(`wh.ev.${ev}` as MessageKey)}
              checked={events.includes(ev)}
              onChange={(e) => toggleEvent(ev, e.target.checked)}
            />
          ))}
        </div>
        {errors.events && (
          <p id={`${prefix}-events-error`} className="mt-2 text-xs font-medium text-danger-text">
            {errors.events}
          </p>
        )}
      </fieldset>

      <Field
        id={`${prefix}-secret`}
        label={t('wh.form.secret')}
        hint={editing ? `${t('wh.form.secret.hint')} ${t('wh.form.secret.keep')}` : t('wh.form.secret.hint')}
        optional
      >
        <div className="flex gap-2">
          <TextInput
            id={`${prefix}-secret`}
            value={secret}
            onChange={(e) => setSecret(e.target.value)}
            maxLength={255}
            autoComplete="off"
            spellCheck={false}
            className="code"
            aria-describedby={`${prefix}-secret-hint`}
          />
          {secret && <CopyButton value={secret} what={t('wh.form.secret').toLowerCase()} size="md" />}
          <Button variant="secondary" onClick={() => setSecret(randomSecret())}>
            {t('wh.form.generate')}
          </Button>
        </div>
      </Field>

      {error && (
        <p role="alert" className="text-sm font-medium text-danger-text">
          {error}
        </p>
      )}
      <div className="flex gap-2">
        <Button type="submit" loading={busy}>
          {busy ? t('common.saving') : editing ? t('wh.form.submit.edit') : t('wh.form.submit.new')}
        </Button>
        <Button variant="ghost" onClick={onCancel} disabled={busy}>
          {t('common.cancel')}
        </Button>
      </div>
    </form>
  )
}

function WebhookCard({ hook, onChange, onRemoved }: { hook: Webhook; onChange: (h: Webhook) => void; onRemoved: () => void }) {
  const { t, tMaybe, fmt } = useI18n()
  const notify = useToast()
  const [editing, setEditing] = useState(false)
  const [confirmDelete, setConfirmDelete] = useState(false)
  const [busy, setBusy] = useState<'toggle' | 'test' | 'delete' | null>(null)
  const [historyOpen, setHistoryOpen] = useState(false)

  const receivesTest = hook.events.includes('test')
  const testBlocked = !hook.is_active ? t('wh.test.needsActive') : !receivesTest ? t('wh.test.needsEvent') : null

  const toggle = async (next: boolean) => {
    setBusy('toggle')
    try {
      onChange(await webhooksApi.toggle(hook.id, next))
      notify(t(next ? 'wh.toast.resumed' : 'wh.toast.paused'))
    } catch (err) {
      notify(errorMessage(t, err), 'danger')
    } finally {
      setBusy(null)
    }
  }

  const test = async () => {
    setBusy('test')
    try {
      await webhooksApi.test(hook.id)
      notify(t('wh.test.sent'))
    } catch (err) {
      notify(errorMessage(t, err), 'danger')
    } finally {
      setBusy(null)
    }
  }

  const remove = async () => {
    setBusy('delete')
    try {
      await webhooksApi.remove(hook.id)
      notify(t('wh.toast.deleted'))
      onRemoved()
    } catch (err) {
      notify(errorMessage(t, err), 'danger')
      setBusy(null)
    }
  }

  if (editing) {
    return (
      <WebhookForm
        hook={hook}
        onCancel={() => setEditing(false)}
        onSaved={(saved) => {
          onChange(saved)
          setEditing(false)
        }}
      />
    )
  }

  return (
    <article aria-labelledby={`wh-${hook.id}-name`} className="flex flex-col gap-4 rounded-xl border border-line p-5">
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <h2 id={`wh-${hook.id}-name`} className="truncate text-sm font-semibold text-ink">
              {hook.name}
            </h2>
            <Badge tone={hook.is_active ? 'success' : 'neutral'}>{hook.is_active ? t('wh.active') : t('wh.paused')}</Badge>
            <Badge tone="neutral" dot={false}>
              {hook.secret_key ? t('wh.signed') : t('wh.unsigned')}
            </Badge>
          </div>
          <p className="code mt-1 break-all text-xs text-muted">{hook.url}</p>
        </div>
        <Switch checked={hook.is_active} onChange={toggle} label={t('wh.toggle', { name: hook.name })} busy={busy === 'toggle'} />
      </div>

      <ul className="flex flex-wrap gap-1.5" aria-label={t('wh.form.events')}>
        {hook.events.map((ev) => (
          <li key={ev}>
            <Badge tone="neutral" dot={false}>
              {tMaybe(`wh.ev.${ev}`)?.replace(/\s*\(.*\)$/, '') ?? ev}
            </Badge>
          </li>
        ))}
      </ul>

      {confirmDelete && (
        <InlineConfirm
          message={t('wh.delete.confirm', { name: hook.name })}
          confirmLabel={t('common.delete')}
          onCancel={() => setConfirmDelete(false)}
          onConfirm={remove}
          busy={busy === 'delete'}
        />
      )}

      <div className="flex flex-wrap items-center gap-2 border-t border-line/70 pt-4">
        <Button size="sm" variant="secondary" onClick={test} loading={busy === 'test'} disabled={!!testBlocked} aria-describedby={testBlocked ? `wh-${hook.id}-test-note` : undefined}>
          {t('wh.test')}
        </Button>
        <Button size="sm" variant="ghost" onClick={() => setEditing(true)}>
          {t('common.edit')}
        </Button>
        <Button size="sm" variant="ghost" onClick={() => setConfirmDelete(true)} disabled={busy === 'delete'}>
          {t('common.delete')}
        </Button>
        <button
          type="button"
          onClick={() => setHistoryOpen((v) => !v)}
          aria-expanded={historyOpen}
          aria-controls={`wh-${hook.id}-history`}
          className="ml-auto inline-flex h-9 items-center gap-1.5 rounded-lg px-3 text-[0.8125rem] font-medium text-muted hover:bg-subtle hover:text-ink"
        >
          {t('wh.history')}
          <ChevronDown className={`h-4 w-4 transition-transform duration-150 ${historyOpen ? 'rotate-180' : ''}`} strokeWidth={1.75} aria-hidden="true" />
        </button>
        {testBlocked && (
          <p id={`wh-${hook.id}-test-note`} className="w-full text-xs text-muted">
            {testBlocked}
          </p>
        )}
      </div>

      <div id={`wh-${hook.id}-history`} hidden={!historyOpen}>
        {historyOpen && <WebhookHistory hook={hook} formatWhen={fmt.dateTime} />}
      </div>
    </article>
  )
}

function WebhookHistory({ hook, formatWhen }: { hook: Webhook; formatWhen: (value: string | null) => string }) {
  const { t, tMaybe } = useI18n()
  const events = useAsync((signal) => webhooksApi.events(hook.id, signal), [hook.id])
  if (events.error) return <ErrorState error={events.error} onRetry={events.reload} />
  if (!events.loading && (events.data ?? []).length === 0) return <p className="text-sm text-muted">{t('wh.history.empty')}</p>

  const tone = (status: string) =>
    status === 'success' ? 'success' : status === 'failed' ? 'danger' : status === 'retrying' ? 'warning' : 'neutral'

  return (
    <TableRegion label={t('wh.history.caption', { name: hook.name })}>
      <table className="w-full min-w-[34rem] text-sm">
        <caption className="sr-only">{t('wh.history.caption', { name: hook.name })}</caption>
        <thead>
          <tr>
            <th scope="col" className={th}>
              {t('wh.history.event')}
            </th>
            <th scope="col" className={th}>
              {t('wh.history.status')}
            </th>
            <th scope="col" className={th}>
              {t('wh.history.code')}
            </th>
            <th scope="col" className={`${th} text-right`}>
              {t('wh.history.attempts')}
            </th>
            <th scope="col" className={`${th} text-right`}>
              {t('wh.history.when')}
            </th>
          </tr>
        </thead>
        <tbody>
          {events.loading ? (
            <SkeletonRows rows={3} cols={5} />
          ) : (
            (events.data ?? []).map((ev) => (
              <tr key={ev.id} className={tr}>
                <td className={`${td} text-ink`}>{tMaybe(`event.${ev.event_type}`) ?? ev.event_type}</td>
                <td className={td}>
                  <Badge tone={tone(ev.status)}>{tMaybe(`wh.status.${ev.status}`) ?? ev.status}</Badge>
                  {ev.error_message && <span className="mt-1 block max-w-[18rem] truncate text-xs text-muted" title={ev.error_message}>{ev.error_message}</span>}
                </td>
                <td className={`${td} num text-muted`}>{ev.response_code ?? '—'}</td>
                <td className={`${td} num text-right text-muted`}>{ev.attempt_count}</td>
                <td className={`${td} whitespace-nowrap text-right text-muted`}>{formatWhen(ev.sent_at ?? ev.created_at)}</td>
              </tr>
            ))
          )}
        </tbody>
      </table>
    </TableRegion>
  )
}
