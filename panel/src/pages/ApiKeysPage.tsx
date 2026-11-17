import { Fragment, useEffect, useState, type FormEvent } from 'react'
import { useLocation } from 'react-router-dom'
import { KeyRound, Plus, ShieldAlert } from 'lucide-react'
import { useI18n } from '../i18n'
import { useUser } from '../lib/auth'
import { useAsync } from '../lib/useAsync'
import { apiKeysApi } from '../lib/endpoints'
import { PERMISSION_KEYS, errorMessage } from '../lib/labels'
import { parseApiDate } from '../lib/dates'
import type { ApiKey, ApiKeyCreated } from '../lib/types'
import type { MessageKey } from '../i18n/pt'
import { PageHeader, TableRegion, td, th, tr } from '../components/Layout'
import { Badge, EmptyState, ErrorState, InlineConfirm, SkeletonRows } from '../components/Feedback'
import { Button } from '../components/Button'
import { Field, Select, TextInput, describedBy } from '../components/Form'
import { CodeBlock, CopyButton } from '../components/Copy'
import { useToast } from '../components/Toast'
import { SendExample } from './SendExample'

type Preset = 'send' | 'sendOnly' | 'full'

const PRESETS: Record<Preset, string[]> = {
  send: ['write:email', 'read:email', 'read:stat'],
  sendOnly: ['write:email'],
  full: ['write:email', 'read:email', 'read:stat', 'read:webhook', 'write:webhook', 'read:suppression', 'write:suppression'],
}
const EXPIRY_DAYS = ['never', '30', '90', '365'] as const
type Expiry = (typeof EXPIRY_DAYS)[number]

function keyStatus(key: ApiKey): 'active' | 'revoked' | 'expired' {
  if (!key.is_active) return 'revoked'
  const exp = parseApiDate(key.expires_at)
  if (exp && exp.getTime() <= Date.now()) return 'expired'
  return 'active'
}

export function ApiKeysPage() {
  const { t } = useI18n()
  const user = useUser()
  const location = useLocation()
  const keys = useAsync((signal) => apiKeysApi.list(signal), [])
  const [formOpen, setFormOpen] = useState(() => new URLSearchParams(location.search).get('new') === '1')
  const [created, setCreated] = useState<ApiKeyCreated | null>(null)

  // A "#send" link (from the overview) scrolls down to the sending example.
  useEffect(() => {
    if (location.hash === '#send') {
      const timer = window.setTimeout(() => document.getElementById('send-title')?.scrollIntoView({ block: 'start' }), 50)
      return () => window.clearTimeout(timer)
    }
  }, [location.hash])

  const list = keys.data?.api_keys ?? []

  return (
    <>
      <PageHeader
        title={t('keys.title')}
        description={t('keys.subtitle')}
        actions={
          !formOpen && !created ? (
            <Button onClick={() => setFormOpen(true)} icon={<Plus className="h-4 w-4" strokeWidth={2} aria-hidden="true" />}>
              {t('keys.create')}
            </Button>
          ) : undefined
        }
      />

      <div className="flex flex-col gap-12">
        {created && <SecretReveal created={created} onDone={() => setCreated(null)} />}

        {formOpen && !created && (
          <CreateKeyForm
            domainId={user.domainId}
            onCancel={() => setFormOpen(false)}
            onCreated={(key) => {
              setCreated(key)
              setFormOpen(false)
              keys.reload()
            }}
          />
        )}

        <section aria-label={t('keys.caption')}>
          {keys.error ? (
            <ErrorState error={keys.error} onRetry={keys.reload} what={t('keys.title').toLowerCase()} />
          ) : !keys.loading && list.length === 0 ? (
            <EmptyState
              icon={<KeyRound className="h-5 w-5" strokeWidth={1.5} />}
              title={t('keys.empty')}
              body={t('keys.empty.body')}
              actions={
                !formOpen && !created ? (
                  <Button onClick={() => setFormOpen(true)} icon={<Plus className="h-4 w-4" strokeWidth={2} aria-hidden="true" />}>
                    {t('keys.create')}
                  </Button>
                ) : undefined
              }
            />
          ) : (
            <KeysTable keys={list} loading={keys.loading} dim={keys.refreshing} onChanged={keys.reload} />
          )}
        </section>

        <SendExample domain={user.domain} />
      </div>
    </>
  )
}

function CreateKeyForm({
  domainId,
  onCancel,
  onCreated,
}: {
  domainId: string
  onCancel: () => void
  onCreated: (key: ApiKeyCreated) => void
}) {
  const { t } = useI18n()
  const [name, setName] = useState('')
  const [preset, setPreset] = useState<Preset>('send')
  const [expiry, setExpiry] = useState<Expiry>('never')
  const [nameError, setNameError] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    if (!name.trim()) {
      setNameError(t('keys.form.nameRequired'))
      return
    }
    setNameError(null)
    setError(null)
    setBusy(true)
    try {
      const expires_at = expiry === 'never' ? null : new Date(Date.now() + Number(expiry) * 86400000).toISOString()
      const key = await apiKeysApi.create({ name: name.trim(), permissions: PRESETS[preset], expires_at })
      // connect-lib's POST /api-keys ignores "channels". Enable the email channel for the
      // user's domain, as scripts/bootstrap_tenant.py does. If that fails, the key can still
      // send through the legacy domain_id stored at creation, so the error does not stop the flow.
      const domain = Number(domainId)
      if (!key.enabled_channels?.includes('email') && Number.isInteger(domain) && domain > 0) {
        await apiKeysApi.addEmailChannel(key.api_key_id, domain).catch(() => undefined)
      }
      onCreated(key)
    } catch (err) {
      setError(errorMessage(t, err))
      setBusy(false)
    }
  }

  const presets: Preset[] = ['send', 'sendOnly', 'full']
  return (
    <form onSubmit={submit} noValidate className="flex flex-col gap-6 rounded-xl border border-line p-5 sm:p-6" aria-labelledby="new-key-title">
      <h2 id="new-key-title" className="text-base font-semibold text-ink">
        {t('keys.form.title')}
      </h2>
      <div className="grid gap-5 md:grid-cols-2">
        <Field id="key-name" label={t('keys.form.name')} hint={t('keys.form.nameHint')} error={nameError}>
          <TextInput
            id="key-name"
            value={name}
            onChange={(event) => setName(event.target.value)}
            maxLength={255}
            invalid={!!nameError}
            aria-describedby={describedBy('key-name', t('keys.form.nameHint'), nameError)}
            autoFocus
          />
        </Field>
        <Field id="key-expiry" label={t('keys.form.expires')}>
          <Select id="key-expiry" value={expiry} onChange={(event) => setExpiry(event.target.value as Expiry)}>
            {EXPIRY_DAYS.map((d) => (
              <option key={d} value={d}>
                {t(`keys.expires.${d}` as MessageKey)}
              </option>
            ))}
          </Select>
        </Field>
      </div>
      <fieldset className="flex flex-col gap-2">
        <legend className="mb-2 text-[0.8125rem] font-medium text-ink">{t('keys.form.access')}</legend>
        <div className="grid gap-2 md:grid-cols-3">
          {presets.map((p) => (
            <label
              key={p}
              className={`flex cursor-pointer gap-3 rounded-xl border p-3.5 transition-colors duration-150 ${
                preset === p ? 'border-accent bg-accent/5' : 'border-line hover:bg-subtle/60'
              }`}
            >
              <input
                type="radio"
                name="key-preset"
                value={p}
                checked={preset === p}
                onChange={() => setPreset(p)}
                className="mt-0.5 h-4 w-4 shrink-0 accent-primary"
              />
              <span className="flex flex-col gap-0.5">
                <span className="text-sm font-medium text-ink">{t(`keys.preset.${p}` as MessageKey)}</span>
                <span className="text-xs leading-relaxed text-muted">{t(`keys.preset.${p}.desc` as MessageKey)}</span>
              </span>
            </label>
          ))}
        </div>
      </fieldset>
      {error && (
        <p role="alert" className="text-sm font-medium text-danger-text">
          {error}
        </p>
      )}
      <div className="flex flex-wrap gap-2">
        <Button type="submit" loading={busy}>
          {busy ? t('keys.form.submitting') : t('keys.form.submit')}
        </Button>
        <Button variant="ghost" onClick={onCancel} disabled={busy}>
          {t('common.cancel')}
        </Button>
      </div>
    </form>
  )
}

/** The secret key is shown only here, once. */
function SecretReveal({ created, onDone }: { created: ApiKeyCreated; onDone: () => void }) {
  const { t } = useI18n()
  return (
    <section
      aria-labelledby="secret-title"
      className="flex flex-col gap-4 rounded-xl border border-warning/50 bg-warning/10 p-5 sm:p-6"
    >
      <div className="flex gap-3">
        <ShieldAlert className="mt-0.5 h-5 w-5 shrink-0 text-warning-text" strokeWidth={1.75} aria-hidden="true" />
        <div>
          <h2 id="secret-title" className="text-base font-semibold text-ink">
            {t('keys.secret.title')}
          </h2>
          <p className="mt-1 max-w-2xl text-sm leading-relaxed text-ink">{t('keys.secret.body')}</p>
        </div>
      </div>
      <div className="flex flex-col gap-1.5">
        <span className="text-[0.8125rem] font-medium text-ink">
          {t('keys.secret.label')} · {created.name}
        </span>
        <div className="flex items-center gap-2 rounded-xl border border-line bg-surface p-1.5 pl-3.5">
          <code className="code min-w-0 flex-1 break-all text-ink">{created.api_key}</code>
          <CopyButton value={created.api_key} what={t('keys.secret.label').toLowerCase()} showLabel size="md" />
        </div>
      </div>
      <div className="flex flex-col gap-2">
        <p className="text-[0.8125rem] text-ink">{t('send.export')}</p>
        <CodeBlock code={`export CONNECT_API_KEY='${created.api_key}'`} label={t('keys.secret.label')} className="bg-surface" />
      </div>
      <div>
        <Button variant="secondary" onClick={onDone}>
          {t('keys.secret.done')}
        </Button>
      </div>
    </section>
  )
}

type Pending = { id: number; action: 'revoke' | 'delete' } | null

function KeysTable({ keys, loading, dim, onChanged }: { keys: ApiKey[]; loading: boolean; dim: boolean; onChanged: () => void }) {
  const { t, fmt } = useI18n()
  const notify = useToast()
  const [pending, setPending] = useState<Pending>(null)
  const [busy, setBusy] = useState<number | null>(null)

  const run = async (key: ApiKey, action: 'revoke' | 'reactivate' | 'delete') => {
    setBusy(key.id)
    try {
      if (action === 'delete') await apiKeysApi.remove(key.id)
      else await apiKeysApi.setActive(key.id, action === 'reactivate')
      notify(t(action === 'delete' ? 'keys.toast.deleted' : action === 'revoke' ? 'keys.toast.revoked' : 'keys.toast.reactivated'))
      setPending(null)
      onChanged()
    } catch (err) {
      notify(errorMessage(t, err), 'danger')
    } finally {
      setBusy(null)
    }
  }

  return (
    <TableRegion label={t('keys.caption')} dim={dim}>
      <table className="w-full min-w-[46rem] text-sm">
        <caption className="sr-only">{t('keys.caption')}</caption>
        <thead>
          <tr>
            <th scope="col" className={th}>
              {t('keys.col.name')}
            </th>
            <th scope="col" className={th}>
              {t('keys.col.access')}
            </th>
            <th scope="col" className={th}>
              {t('keys.col.lastUsed')}
            </th>
            <th scope="col" className={th}>
              {t('keys.col.created')}
            </th>
            <th scope="col" className={th}>
              {t('keys.col.status')}
            </th>
            <th scope="col" className={`${th} text-right`}>
              <span className="sr-only">{t('keys.col.actions')}</span>
            </th>
          </tr>
        </thead>
        <tbody>
          {loading ? (
            <SkeletonRows rows={3} cols={6} />
          ) : (
            keys.map((key) => {
              const status = keyStatus(key)
              const confirming = pending?.id === key.id ? pending.action : null
              return (
                <Fragment key={key.id}>
                  <tr className={tr}>
                    <td className={td}>
                      <p className="font-medium text-ink">{key.name}</p>
                      <p className="code text-xs text-muted">{key.api_key_id}</p>
                    </td>
                    <td className={`${td} max-w-[16rem] text-[0.8125rem] text-ink`}>
                      {key.permissions.map((p) => (PERMISSION_KEYS[p] ? t(PERMISSION_KEYS[p]) : p)).join(' · ')}
                    </td>
                    <td className={`${td} whitespace-nowrap text-muted`}>
                      {key.last_used_at ? (
                        <time dateTime={key.last_used_at} title={fmt.dateTime(key.last_used_at)}>
                          {fmt.relative(key.last_used_at)}
                        </time>
                      ) : (
                        t('keys.neverUsed')
                      )}
                    </td>
                    <td className={`${td} whitespace-nowrap text-muted`}>
                      {fmt.date(key.created_at)}
                      {key.expires_at && status === 'active' && (
                        <span className="block text-xs">{t('keys.expiresOn', { date: fmt.date(key.expires_at) })}</span>
                      )}
                    </td>
                    <td className={td}>
                      <Badge tone={status === 'active' ? 'success' : status === 'expired' ? 'warning' : 'neutral'}>
                        {t(`keys.status.${status}` as MessageKey)}
                      </Badge>
                    </td>
                    <td className={`${td} whitespace-nowrap text-right`}>
                      <div className="flex justify-end gap-1">
                        {key.is_active ? (
                          <Button variant="ghost" size="sm" onClick={() => setPending({ id: key.id, action: 'revoke' })} disabled={busy === key.id}>
                            {t('keys.revoke')}
                          </Button>
                        ) : (
                          <>
                            <Button variant="ghost" size="sm" onClick={() => run(key, 'reactivate')} loading={busy === key.id && !confirming}>
                              {t('keys.reactivate')}
                            </Button>
                            <Button variant="ghost" size="sm" onClick={() => setPending({ id: key.id, action: 'delete' })} disabled={busy === key.id}>
                              {t('common.delete')}
                            </Button>
                          </>
                        )}
                      </div>
                    </td>
                  </tr>
                  {confirming && (
                    <tr>
                      <td colSpan={6} className="px-4 pb-3">
                        <InlineConfirm
                          message={t(confirming === 'revoke' ? 'keys.revoke.confirm' : 'keys.delete.confirm', { name: key.name })}
                          confirmLabel={confirming === 'revoke' ? t('keys.revoke') : t('common.delete')}
                          onCancel={() => setPending(null)}
                          onConfirm={() => run(key, confirming)}
                          busy={busy === key.id}
                        />
                      </td>
                    </tr>
                  )}
                </Fragment>
              )
            })
          )}
        </tbody>
      </table>
    </TableRegion>
  )
}
