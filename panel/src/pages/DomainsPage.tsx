import { useState, type FormEvent } from 'react'
import { Globe } from 'lucide-react'
import { useI18n } from '../i18n'
import { useUser } from '../lib/auth'
import { useAsync, type AsyncState } from '../lib/useAsync'
import { dkimApi, domainApi, emailConfigApi } from '../lib/endpoints'
import { EMAIL_RE, errorMessage } from '../lib/labels'
import type { DkimKey, DnsRecordInfo, DomainDnsInfoResponse, EmailConfig, EmailConfigUpdate } from '../lib/types'
import type { MessageKey } from '../i18n/pt'
import { PageHeader, Section, TableRegion, td, th, tr } from '../components/Layout'
import { Badge, Callout, EmptyState, ErrorState, InlineConfirm, Skeleton, SkeletonRows } from '../components/Feedback'
import { Button } from '../components/Button'
import { Field, Switch, TextInput, describedBy } from '../components/Form'
import { CopyButton } from '../components/Copy'
import { useToast } from '../components/Toast'

// Suggested DMARC record when the API does not return the records: start in observe-only mode.
const DMARC_SUGGESTED = 'v=DMARC1; p=none; aspf=r; adkim=r'

type Purpose = 'returnPath' | 'dkim' | 'dmarc' | 'other'

interface RecordRow extends DnsRecordInfo {
  purpose: Purpose
  /** Source DKIM key (rows built by the panel), used to fetch the value on demand. */
  dkim?: DkimKey
}

function purposeOf(record: DnsRecordInfo): Purpose {
  const name = record.name.toLowerCase()
  if (name.startsWith('_dmarc.')) return 'dmarc'
  if (name.includes('._domainkey.')) return 'dkim'
  if (record.type.toUpperCase() === 'CNAME' && /^rbm/.test(name)) return 'returnPath'
  return 'other'
}

export function DomainsPage() {
  const { t } = useI18n()
  const user = useUser()
  const domain = user.domain

  const dnsInfo = useAsync((signal) => domainApi.dnsInfo(domain, signal), [domain])
  const dkim = useAsync((signal) => dkimApi.list(domain, signal), [domain])
  const configs = useAsync((signal) => emailConfigApi.list(signal), [])

  return (
    <>
      <PageHeader title={t('domains.title')} description={t('domains.subtitle')} />

      <div className="flex flex-col gap-12">
        <div className="flex items-center gap-3">
          <span className="inline-flex h-10 w-10 items-center justify-center rounded-xl bg-subtle text-muted" aria-hidden="true">
            <Globe className="h-5 w-5" strokeWidth={1.5} />
          </span>
          <div className="min-w-0">
            <p className="text-xs text-muted">{t('domains.sending')}</p>
            <p className="truncate text-lg font-semibold tracking-tight text-ink">{domain}</p>
          </div>
        </div>

        <DnsRecords domain={domain} dnsInfo={dnsInfo} dkim={dkim} />
        <DkimKeys domain={domain} state={dkim} />
        <EmailConfigs domain={domain} state={configs} />
      </div>
    </>
  )
}

// ── DNS records ──────────────────────────────────────────────────────────────

function DnsRecords({
  domain,
  dnsInfo,
  dkim,
}: {
  domain: string
  dnsInfo: AsyncState<DomainDnsInfoResponse>
  dkim: AsyncState<DkimKey[]>
}) {
  const { t } = useI18n()
  const [revealed, setRevealed] = useState<Record<number, string | null>>({})
  const [revealing, setRevealing] = useState<number | null>(null)
  const [revealError, setRevealError] = useState<string | null>(null)

  const fromApi = dnsInfo.data?.dns_records
  // If the dns-info call fails (e.g. an older API without the route), build what we can from /dkim/keys.
  const usingFallback = !fromApi && !!dnsInfo.error
  const loading = dnsInfo.loading || (usingFallback && dkim.loading)

  let rows: RecordRow[] = []
  if (fromApi) {
    rows = fromApi.map((r) => ({ ...r, purpose: purposeOf(r) }))
  } else if (usingFallback) {
    rows = (dkim.data ?? []).map((key) => ({
      type: 'TXT',
      name: key.dns_record_name ?? `${key.selector}._domainkey.${key.dkim_domain}`,
      value: key.dns_record_value ?? revealed[key.id] ?? '',
      purpose: 'dkim' as const,
      dkim: key,
    }))
    rows.push({ type: 'TXT', name: `_dmarc.${domain}`, value: DMARC_SUGGESTED, purpose: 'dmarc' })
  }
  const hasReturnPath = rows.some((r) => r.purpose === 'returnPath')

  const reveal = async (key: DkimKey) => {
    setRevealing(key.id)
    setRevealError(null)
    try {
      const res = await dkimApi.revealRecord(key)
      setRevealed((prev) => ({ ...prev, [key.id]: res.dns_record_value ?? null }))
    } catch (err) {
      setRevealError(errorMessage(t, err))
    } finally {
      setRevealing(null)
    }
  }

  return (
    <Section id="dns" title={t('domains.dns.title')} description={t('domains.dns.subtitle')}>
      {dkim.error && usingFallback ? (
        <ErrorState error={dkim.error} onRetry={dkim.reload} what={t('domains.dns.title').toLowerCase()} />
      ) : (
        <>
          <TableRegion label={t('domains.dns.caption')}>
            <table className="w-full min-w-[48rem] text-sm">
              <caption className="sr-only">{t('domains.dns.caption')}</caption>
              <thead>
                <tr>
                  <th scope="col" className={`${th} w-20`}>
                    {t('domains.dns.type')}
                  </th>
                  <th scope="col" className={th}>
                    {t('domains.dns.name')}
                  </th>
                  <th scope="col" className={th}>
                    {t('domains.dns.value')}
                  </th>
                  <th scope="col" className={`${th} w-64`}>
                    {t('domains.dns.purpose')}
                  </th>
                </tr>
              </thead>
              <tbody>
                {loading ? (
                  <SkeletonRows rows={3} cols={4} />
                ) : (
                  rows.map((row) => {
                    const missingValue = row.purpose === 'dkim' && !row.value && row.dkim
                    const unavailable = row.dkim && revealed[row.dkim.id] === null
                    return (
                      <tr key={`${row.type}-${row.name}`} className={`${tr} align-top`}>
                        <td className={`${td} align-top`}>
                          <span className="code font-medium text-ink">{row.type}</span>
                        </td>
                        <td className={`${td} align-top`}>
                          <span className="flex items-start gap-1">
                            <span className="code min-w-0 break-all pt-1 text-ink">{row.name}</span>
                            <CopyButton value={row.name} what={t('domains.dns.name').toLowerCase()} />
                          </span>
                        </td>
                        <td className={`${td} align-top`}>
                          {missingValue && !unavailable ? (
                            <Button size="sm" variant="secondary" loading={revealing === row.dkim!.id} onClick={() => reveal(row.dkim!)}>
                              {t('domains.dkim.showValue')}
                            </Button>
                          ) : unavailable ? (
                            <span className="text-xs text-muted">{t('domains.dkim.noValue')}</span>
                          ) : (
                            <span className="flex items-start gap-1">
                              <span className="code min-w-0 max-w-[28rem] break-all pt-1 text-ink">{row.value}</span>
                              <CopyButton value={row.value} what={t('domains.dns.value').toLowerCase()} />
                            </span>
                          )}
                        </td>
                        <td className={`${td} align-top text-[0.8125rem] leading-relaxed text-muted`}>
                          {t(`domains.purpose.${row.purpose}` as MessageKey)}
                          {row.purpose === 'dmarc' && usingFallback && (
                            <span className="mt-1 block text-xs">{t('domains.dmarc.suggested')}</span>
                          )}
                        </td>
                      </tr>
                    )
                  })
                )}
              </tbody>
            </table>
          </TableRegion>
          {revealError && (
            <p role="alert" className="text-sm text-danger-text">
              {revealError}
            </p>
          )}
          <p className="text-xs text-muted">{t('domains.dns.hostHint', { domain })}</p>
          {!loading && !hasReturnPath && <Callout tone="warning">{t('domains.returnPath.missing')}</Callout>}
        </>
      )}
    </Section>
  )
}

// ── DKIM keys ────────────────────────────────────────────────────────────────

function DkimKeys({ domain, state }: { domain: string; state: AsyncState<DkimKey[]> }) {
  const { t, fmt } = useI18n()
  return (
    <Section id="dkim" title={t('domains.keys.title')} description={t('domains.keys.subtitle')}>
      {state.error ? (
        <ErrorState error={state.error} onRetry={state.reload} what={t('domains.keys.title').toLowerCase()} />
      ) : !state.loading && (state.data ?? []).length === 0 ? (
        <Callout tone="warning">{t('domains.keys.empty', { domain })}</Callout>
      ) : (
        <TableRegion label={t('domains.keys.caption')}>
          <table className="w-full min-w-[32rem] text-sm">
            <caption className="sr-only">{t('domains.keys.caption')}</caption>
            <thead>
              <tr>
                <th scope="col" className={th}>
                  {t('domains.keys.selector')}
                </th>
                <th scope="col" className={th}>
                  {t('domains.keys.domain')}
                </th>
                <th scope="col" className={th}>
                  {t('domains.keys.created')}
                </th>
              </tr>
            </thead>
            <tbody>
              {state.loading ? (
                <SkeletonRows rows={2} cols={3} />
              ) : (
                (state.data ?? []).map((key) => (
                  <tr key={key.id} className={tr}>
                    <td className={td}>
                      <span className="code font-medium text-ink">{key.selector}</span>
                    </td>
                    <td className={`${td} text-ink`}>{key.dkim_domain}</td>
                    <td className={`${td} text-muted`}>{fmt.date(key.created_at)}</td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </TableRegion>
      )}
    </Section>
  )
}

// ── sending configurations ───────────────────────────────────────────────────

function EmailConfigs({ domain, state }: { domain: string; state: AsyncState<EmailConfig[]> }) {
  const { t } = useI18n()
  const configs = state.data ?? []
  const replace = (next: EmailConfig) => state.setData((prev) => (prev ?? []).map((c) => (c.config_id === next.config_id ? next : c)))

  return (
    <Section id="configs" title={t('domains.configs.title')} description={t('domains.configs.subtitle')}>
      {state.error ? (
        <ErrorState error={state.error} onRetry={state.reload} what={t('domains.configs.title').toLowerCase()} />
      ) : state.loading ? (
        <Skeleton className="h-40 w-full rounded-xl" />
      ) : configs.length === 0 ? (
        <EmptyState compact title={t('domains.configs.empty', { domain })} />
      ) : (
        <ul className="flex flex-col gap-4">
          {configs.map((config) => (
            <li key={config.config_id}>
              <ConfigCard config={config} onChange={replace} />
            </li>
          ))}
        </ul>
      )}
    </Section>
  )
}

function ConfigCard({ config, onChange }: { config: EmailConfig; onChange: (c: EmailConfig) => void }) {
  const { t, fmt } = useI18n()
  const notify = useToast()
  const [editing, setEditing] = useState(false)
  const [confirmOff, setConfirmOff] = useState(false)
  const [busy, setBusy] = useState<string | null>(null)

  const save = async (patch: EmailConfigUpdate, key: string) => {
    setBusy(key)
    try {
      onChange(await emailConfigApi.update(config.config_id, patch))
      return true
    } catch (err) {
      notify(errorMessage(t, err), 'danger')
      return false
    } finally {
      setBusy(null)
    }
  }

  const toggleActive = async () => {
    setBusy('active')
    try {
      onChange(await emailConfigApi.toggle(config.config_id))
      setConfirmOff(false)
    } catch (err) {
      notify(errorMessage(t, err), 'danger')
    } finally {
      setBusy(null)
    }
  }

  const sender = config.default_from_email
    ? config.default_from_name
      ? `${config.default_from_name} <${config.default_from_email}>`
      : config.default_from_email
    : null

  return (
    <article aria-labelledby={`cfg-${config.config_id}`} className="flex flex-col gap-5 rounded-xl border border-line p-5">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div className="flex min-w-0 flex-wrap items-center gap-2">
          <h3 id={`cfg-${config.config_id}`} className="truncate text-sm font-semibold text-ink">
            {config.name}
          </h3>
          {config.is_default && <Badge tone="info" dot={false}>{t('domains.configs.default')}</Badge>}
          <Badge tone={config.is_active ? 'success' : 'neutral'}>
            {config.is_active ? t('domains.configs.active') : t('domains.configs.inactive')}
          </Badge>
        </div>
        <Button
          size="sm"
          variant="ghost"
          loading={busy === 'active' && !confirmOff}
          onClick={() => (config.is_active ? setConfirmOff(true) : toggleActive())}
        >
          {config.is_active ? t('domains.configs.deactivate') : t('domains.configs.activate')}
        </Button>
      </div>

      {confirmOff && (
        <InlineConfirm
          message={t('domains.configs.deactivate.confirm', { name: config.name })}
          confirmLabel={t('domains.configs.deactivate')}
          onCancel={() => setConfirmOff(false)}
          onConfirm={toggleActive}
          busy={busy === 'active'}
        />
      )}

      {editing ? (
        <SenderForm config={config} busy={busy === 'sender'} onCancel={() => setEditing(false)} onSave={async (patch) => {
          if (await save(patch, 'sender')) {
            setEditing(false)
            notify(t('domains.configs.saved'))
          }
        }} />
      ) : (
        <dl className="grid gap-x-8 gap-y-3 text-sm sm:grid-cols-2">
          <div>
            <dt className="text-xs text-muted">{t('domains.configs.from')}</dt>
            <dd className="break-all text-ink">{sender ?? <span className="text-muted">{t('domains.configs.notSet')}</span>}</dd>
          </div>
          <div>
            <dt className="text-xs text-muted">{t('domains.configs.replyTo')}</dt>
            <dd className="break-all text-ink">
              {config.default_reply_to ?? <span className="text-muted">{t('domains.configs.notSet')}</span>}
            </dd>
          </div>
          <div className="sm:col-span-2">
            <Button size="sm" variant="secondary" onClick={() => setEditing(true)}>
              {t('common.edit')}
            </Button>
          </div>
        </dl>
      )}

      <fieldset className="flex flex-col gap-1 border-t border-line/70 pt-4">
        <legend className="sr-only">{t('domains.configs.tracking')}</legend>
        <ToggleRow
          label={t('domains.configs.tracking')}
          description={t('domains.configs.tracking.desc')}
          checked={config.tracking_enabled}
          busy={busy === 'tracking_enabled'}
          onChange={(v) => save({ tracking_enabled: v }, 'tracking_enabled')}
        />
        <ToggleRow
          label={t('domains.configs.opens')}
          description={t('domains.configs.opens.desc')}
          checked={config.inject_pixel}
          disabled={!config.tracking_enabled}
          busy={busy === 'inject_pixel'}
          onChange={(v) => save({ inject_pixel: v }, 'inject_pixel')}
        />
        <ToggleRow
          label={t('domains.configs.clicks')}
          description={t('domains.configs.clicks.desc')}
          checked={config.rewrite_links}
          disabled={!config.tracking_enabled}
          busy={busy === 'rewrite_links'}
          onChange={(v) => save({ rewrite_links: v }, 'rewrite_links')}
        />
      </fieldset>

      <p className="num text-xs text-muted">
        {t('domains.configs.limits', {
          minute: fmt.number(config.rate_limit_per_minute),
          hour: fmt.number(config.rate_limit_per_hour),
          day: fmt.number(config.rate_limit_per_day),
        })}
      </p>
    </article>
  )
}

function ToggleRow(props: {
  label: string
  description: string
  checked: boolean
  disabled?: boolean
  busy?: boolean
  onChange: (value: boolean) => void
}) {
  return (
    <div className="flex items-center justify-between gap-4 py-1.5">
      <div className="min-w-0">
        <p className={`text-sm ${props.disabled ? 'text-muted' : 'text-ink'}`}>{props.label}</p>
        <p className="text-xs text-muted">{props.description}</p>
      </div>
      <Switch checked={props.checked} onChange={props.onChange} label={props.label} disabled={props.disabled} busy={props.busy} />
    </div>
  )
}

function SenderForm({
  config,
  busy,
  onCancel,
  onSave,
}: {
  config: EmailConfig
  busy: boolean
  onCancel: () => void
  onSave: (patch: EmailConfigUpdate) => void
}) {
  const { t } = useI18n()
  const [fromName, setFromName] = useState(config.default_from_name ?? '')
  const [fromEmail, setFromEmail] = useState(config.default_from_email ?? '')
  const [replyTo, setReplyTo] = useState(config.default_reply_to ?? '')
  const [errors, setErrors] = useState<{ from?: string; reply?: string }>({})
  const id = config.config_id

  const submit = (event: FormEvent) => {
    event.preventDefault()
    const next: typeof errors = {}
    if (fromEmail.trim() && !EMAIL_RE.test(fromEmail.trim())) next.from = t('domains.configs.emailInvalid')
    if (replyTo.trim() && !EMAIL_RE.test(replyTo.trim())) next.reply = t('domains.configs.emailInvalid')
    setErrors(next)
    if (next.from || next.reply) return
    // The API ignores missing fields; send only what changed and is not empty.
    const patch: EmailConfigUpdate = {}
    if (fromName.trim() !== (config.default_from_name ?? '') && fromName.trim()) patch.default_from_name = fromName.trim()
    if (fromEmail.trim() !== (config.default_from_email ?? '') && fromEmail.trim()) patch.default_from_email = fromEmail.trim()
    if (replyTo.trim() !== (config.default_reply_to ?? '') && replyTo.trim()) patch.default_reply_to = replyTo.trim()
    if (Object.keys(patch).length === 0) {
      onCancel()
      return
    }
    onSave(patch)
  }

  return (
    <form onSubmit={submit} noValidate className="flex flex-col gap-4">
      <div className="grid gap-4 sm:grid-cols-2">
        <Field id={`${id}-from-name`} label={t('domains.configs.fromName')}>
          <TextInput id={`${id}-from-name`} value={fromName} onChange={(e) => setFromName(e.target.value)} maxLength={255} />
        </Field>
        <Field id={`${id}-from-email`} label={t('domains.configs.from')} error={errors.from}>
          <TextInput
            id={`${id}-from-email`}
            type="email"
            value={fromEmail}
            onChange={(e) => setFromEmail(e.target.value)}
            invalid={!!errors.from}
            aria-describedby={describedBy(`${id}-from-email`, undefined, errors.from)}
          />
        </Field>
        <Field id={`${id}-reply`} label={t('domains.configs.replyTo')} error={errors.reply} optional>
          <TextInput
            id={`${id}-reply`}
            type="email"
            value={replyTo}
            onChange={(e) => setReplyTo(e.target.value)}
            invalid={!!errors.reply}
            aria-describedby={describedBy(`${id}-reply`, undefined, errors.reply)}
          />
        </Field>
      </div>
      <div className="flex gap-2">
        <Button type="submit" size="sm" loading={busy}>
          {busy ? t('common.saving') : t('common.save')}
        </Button>
        <Button size="sm" variant="ghost" onClick={onCancel} disabled={busy}>
          {t('common.cancel')}
        </Button>
      </div>
    </form>
  )
}
