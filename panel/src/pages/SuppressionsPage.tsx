import { Fragment, useState, type FormEvent } from 'react'
import { MailX, Plus } from 'lucide-react'
import { useI18n } from '../i18n'
import { useUser } from '../lib/auth'
import { useAsync } from '../lib/useAsync'
import { suppressionsApi } from '../lib/endpoints'
import { EMAIL_RE, errorMessage } from '../lib/labels'
import type { Suppression, SuppressionType } from '../lib/types'
import type { MessageKey } from '../i18n/pt'
import { PageHeader, Pagination, Segmented, TableRegion, td, th, tr } from '../components/Layout'
import { EmptyState, ErrorState, InlineConfirm, SkeletonRows } from '../components/Feedback'
import { Button } from '../components/Button'
import { Field, TextInput, describedBy } from '../components/Form'
import { useToast } from '../components/Toast'

type TypeFilter = 'all' | SuppressionType
const TYPES: TypeFilter[] = ['all', 'unsubscribe', 'bounce', 'complaint', 'manual']
const PAGE_SIZE = 50

export function SuppressionsPage() {
  const { t, tn, fmt } = useI18n()
  const user = useUser()
  const notify = useToast()
  const [type, setType] = useState<TypeFilter>('all')
  const [draft, setDraft] = useState('')
  const [search, setSearch] = useState('')
  const [page, setPage] = useState(1)
  const [formOpen, setFormOpen] = useState(false)
  const [pendingId, setPendingId] = useState<number | null>(null)
  const [removing, setRemoving] = useState<number | null>(null)

  const list = useAsync(
    (signal) =>
      suppressionsApi.list(
        {
          page,
          limit: PAGE_SIZE,
          email: search || undefined,
          suppression_type: type === 'all' ? undefined : type,
          is_active: true,
        },
        signal,
      ),
    [page, search, type],
  )

  const filtered = type !== 'all' || search !== ''
  const rows = list.data?.suppressions ?? []

  const remove = async (item: Suppression) => {
    setRemoving(item.id)
    try {
      await suppressionsApi.remove(item.id)
      notify(t('sup.toast.removed'))
      setPendingId(null)
      list.reload()
    } catch (err) {
      notify(errorMessage(t, err), 'danger')
    } finally {
      setRemoving(null)
    }
  }

  return (
    <>
      <PageHeader
        title={t('sup.title')}
        description={t('sup.subtitle')}
        actions={
          !formOpen ? (
            <Button onClick={() => setFormOpen(true)} icon={<Plus className="h-4 w-4" strokeWidth={2} aria-hidden="true" />}>
              {t('sup.add')}
            </Button>
          ) : undefined
        }
      />

      {formOpen && (
        <AddSuppressionForm
          domain={user.domain}
          onCancel={() => setFormOpen(false)}
          onAdded={(email) => {
            notify(t('sup.toast.added', { email }))
            setFormOpen(false)
            setPage(1)
            list.reload()
          }}
        />
      )}

      <div className="mb-5 flex flex-col gap-4 lg:flex-row lg:items-end lg:justify-between">
        <Segmented
          name="sup-type"
          legend={t('sup.type')}
          value={type}
          onChange={(value) => {
            setType(value)
            setPage(1)
          }}
          options={TYPES.map((value) => ({ value, label: t(`sup.type.${value}` as MessageKey) }))}
        />
        <form
          role="search"
          className="flex gap-2 lg:w-96"
          onSubmit={(event) => {
            event.preventDefault()
            setSearch(draft.trim())
            setPage(1)
          }}
        >
          <label htmlFor="sup-search" className="sr-only">
            {t('sup.search')}
          </label>
          <TextInput
            id="sup-search"
            type="search"
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            placeholder={t('sup.search')}
            autoComplete="off"
          />
          <Button type="submit" variant="secondary">
            {t('emails.search.submit')}
          </Button>
        </form>
      </div>

      {list.error ? (
        <ErrorState error={list.error} onRetry={list.reload} what={t('sup.title').toLowerCase()} />
      ) : !list.loading && rows.length === 0 ? (
        filtered ? (
          <EmptyState compact title={t('sup.emptyFiltered')} />
        ) : (
          <EmptyState icon={<MailX className="h-5 w-5" strokeWidth={1.5} />} title={t('sup.empty')} body={t('sup.empty.body')} />
        )
      ) : (
        <>
          {list.data && (
            <p className="num mb-3 text-[0.8125rem] font-medium text-ink">
              {tn('sup.count', list.data.total, { count: fmt.number(list.data.total) })}
            </p>
          )}
          <TableRegion label={t('sup.caption')} dim={list.refreshing}>
            <table className="w-full min-w-[36rem] text-sm">
              <caption className="sr-only">{t('sup.caption')}</caption>
              <thead>
                <tr>
                  <th scope="col" className={th}>
                    {t('sup.col.email')}
                  </th>
                  <th scope="col" className={th}>
                    {t('sup.col.reason')}
                  </th>
                  <th scope="col" className={th}>
                    {t('sup.col.since')}
                  </th>
                  <th scope="col" className={`${th} text-right`}>
                    <span className="sr-only">{t('keys.col.actions')}</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {list.loading ? (
                  <SkeletonRows rows={6} cols={4} />
                ) : (
                  rows.map((item) => (
                    <Fragment key={item.id}>
                      <tr className={tr}>
                        <td className={`${td} font-medium text-ink`}>{item.email}</td>
                        <td className={`${td} max-w-[22rem] text-muted`}>
                          {item.suppression_type && (
                            <span className="mr-2 inline-block rounded border border-line px-1.5 py-0.5 text-xs text-ink">
                              {t(`sup.type.${item.suppression_type}` as MessageKey)}
                            </span>
                          )}
                          {item.reason || '—'}
                        </td>
                        <td className={`${td} whitespace-nowrap text-muted`}>{fmt.date(item.created_at)}</td>
                        <td className={`${td} text-right`}>
                          <Button variant="ghost" size="sm" onClick={() => setPendingId(item.id)} disabled={removing === item.id}>
                            {t('sup.remove')}
                          </Button>
                        </td>
                      </tr>
                      {pendingId === item.id && (
                        <tr>
                          <td colSpan={4} className="px-4 pb-3">
                            <InlineConfirm
                              message={t('sup.remove.confirm', { email: item.email })}
                              confirmLabel={t('sup.remove')}
                              onCancel={() => setPendingId(null)}
                              onConfirm={() => remove(item)}
                              busy={removing === item.id}
                            />
                          </td>
                        </tr>
                      )}
                    </Fragment>
                  ))
                )}
              </tbody>
            </table>
          </TableRegion>
          {list.data && list.data.total_pages > 1 && (
            <Pagination page={page} totalPages={list.data.total_pages} onPage={setPage} />
          )}
        </>
      )}
    </>
  )
}

function AddSuppressionForm({ domain, onCancel, onAdded }: { domain: string; onCancel: () => void; onAdded: (email: string) => void }) {
  const { t } = useI18n()
  const [email, setEmail] = useState('')
  const [reason, setReason] = useState('')
  const [errors, setErrors] = useState<{ email?: string; reason?: string }>({})
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    const next: typeof errors = {}
    if (!EMAIL_RE.test(email.trim())) next.email = t('sup.form.emailInvalid')
    if (!reason.trim()) next.reason = t('sup.form.reasonRequired')
    setErrors(next)
    setError(null)
    if (next.email || next.reason) return
    setBusy(true)
    try {
      const address = email.trim().toLowerCase()
      await suppressionsApi.create({ email: address, reason: reason.trim(), domain_name: domain })
      onAdded(address)
    } catch (err) {
      setError(errorMessage(t, err))
      setBusy(false)
    }
  }

  return (
    <form onSubmit={submit} noValidate className="mb-8 flex flex-col gap-4 rounded-xl border border-line p-5" aria-label={t('sup.add')}>
      <div className="grid gap-4 md:grid-cols-2">
        <Field id="sup-email" label={t('sup.form.email')} error={errors.email}>
          <TextInput
            id="sup-email"
            type="email"
            value={email}
            onChange={(event) => setEmail(event.target.value)}
            invalid={!!errors.email}
            aria-describedby={describedBy('sup-email', undefined, errors.email)}
            autoFocus
          />
        </Field>
        <Field id="sup-reason" label={t('sup.form.reason')} error={errors.reason}>
          <TextInput
            id="sup-reason"
            value={reason}
            onChange={(event) => setReason(event.target.value)}
            placeholder={t('sup.form.reason.placeholder')}
            maxLength={500}
            invalid={!!errors.reason}
            aria-describedby={describedBy('sup-reason', undefined, errors.reason)}
          />
        </Field>
      </div>
      {error && (
        <p role="alert" className="text-sm font-medium text-danger-text">
          {error}
        </p>
      )}
      <div className="flex gap-2">
        <Button type="submit" loading={busy}>
          {busy ? t('common.saving') : t('sup.form.submit')}
        </Button>
        <Button variant="ghost" onClick={onCancel} disabled={busy}>
          {t('common.cancel')}
        </Button>
      </div>
    </form>
  )
}
