import { useMemo, useState, type FormEvent } from 'react'
import { ChevronRight, MailOpen, MousePointerClick, Search, Send } from 'lucide-react'
import { useI18n } from '../i18n'
import { useAsync } from '../lib/useAsync'
import { statsApi } from '../lib/endpoints'
import { PERIOD_PRESETS, rangeForPreset, type PeriodPreset } from '../lib/dates'
import { eventLabel, eventTone } from '../lib/labels'
import type { MessageItem } from '../lib/types'
import type { MessageKey } from '../i18n/pt'
import { PageHeader, Pagination, Segmented, TableRegion, td, th, tr } from '../components/Layout'
import { Badge, EmptyState, ErrorState, SkeletonRows } from '../components/Feedback'
import { Button, ButtonLink } from '../components/Button'
import { Field, Select, TextInput } from '../components/Form'
import { EmailDrawer } from './EmailDrawer'

type Filter = 'all' | 'delivered' | 'problems' | 'pending' | 'deferred' | 'opened' | 'clicked'

// Maps each filter to GET /stats/messages parameters (event_type takes precedence over status).
const FILTER_QUERY: Record<Filter, { event_type?: string; status?: string }> = {
  all: {},
  delivered: { event_type: 'delivered' },
  problems: { status: 'error' },
  pending: { status: 'pending' },
  deferred: { event_type: 'deferred' },
  opened: { event_type: 'opened' },
  clicked: { event_type: 'clicked' },
}
const FILTERS = Object.keys(FILTER_QUERY) as Filter[]
const PAGE_SIZE = 25

export function EmailsPage() {
  const { t, tn, tMaybe, fmt } = useI18n()
  const [preset, setPreset] = useState<PeriodPreset>('7d')
  const [filter, setFilter] = useState<Filter>('all')
  const [draft, setDraft] = useState('')
  const [search, setSearch] = useState('')
  const [page, setPage] = useState(1)
  const [selected, setSelected] = useState<MessageItem | null>(null)
  const [lookupOpen, setLookupOpen] = useState(false)
  const [lookupDraft, setLookupDraft] = useState('')
  const [lookupId, setLookupId] = useState<string | null>(null)
  const range = useMemo(() => rangeForPreset(preset), [preset])

  const list = useAsync(
    (signal) =>
      statsApi.messages({ page, limit: PAGE_SIZE, ...range, ...FILTER_QUERY[filter], search: search || undefined }, signal),
    [page, range.start_date, range.end_date, filter, search],
  )

  const filtered = filter !== 'all' || search !== ''
  const empty = list.data !== undefined && list.data.pagination.total === 0
  // Empty list: was anything ever sent? Decides between "nothing yet" and "nothing matches these filters".
  const everSent = useAsync(
    (signal) => (empty ? statsApi.messages({ page: 1, limit: 1 }, signal).then((r) => r.pagination.total > 0) : Promise.resolve(true)),
    [empty],
  )

  const changeFilter = <T,>(setter: (v: T) => void) => (value: T) => {
    setter(value)
    setPage(1)
  }

  const submitSearch = (event: FormEvent) => {
    event.preventDefault()
    setSearch(draft.trim())
    setPage(1)
  }

  const clearFilters = () => {
    setFilter('all')
    setDraft('')
    setSearch('')
    setPage(1)
  }

  const submitLookup = (event: FormEvent) => {
    event.preventDefault()
    const id = lookupDraft.trim()
    if (id) setLookupId(id)
  }

  const data = list.data
  const totals = data?.totals

  return (
    <>
      <PageHeader
        title={t('emails.title')}
        description={t('emails.subtitle')}
        actions={
          <Button variant="outline" onClick={() => setLookupOpen((v) => !v)} aria-expanded={lookupOpen} icon={<Search className="h-4 w-4" strokeWidth={1.75} aria-hidden="true" />}>
            {t('emails.lookup')}
          </Button>
        }
      />

      {lookupOpen && (
        <form onSubmit={submitLookup} className="mb-8 flex flex-col gap-3 rounded-xl border border-line p-4 sm:flex-row sm:items-end">
          <Field id="lookup-id" label={t('emails.lookup.label')} hint={t('emails.lookup.hint')} className="flex-1">
            <TextInput
              id="lookup-id"
              value={lookupDraft}
              onChange={(event) => setLookupDraft(event.target.value)}
              aria-describedby="lookup-id-hint"
              autoComplete="off"
              spellCheck={false}
              className="code"
              autoFocus
            />
          </Field>
          <Button type="submit" disabled={!lookupDraft.trim()} className="sm:mb-[1.375rem]">
            {t('emails.lookup.submit')}
          </Button>
        </form>
      )}

      <div className="mb-5 flex flex-col gap-4 xl:flex-row xl:items-end xl:justify-between">
        <div className="flex flex-col gap-4 md:flex-row md:items-end">
          <Segmented
            name="emails-period"
            legend={t('period.label')}
            value={preset}
            onChange={changeFilter(setPreset)}
            options={PERIOD_PRESETS.map((p) => ({ value: p, label: t(`period.${p}` as MessageKey) }))}
          />
          <div className="flex min-w-0 flex-col gap-1.5 md:w-72">
            <label htmlFor="emails-filter" className="sr-only">
              {t('emails.filter')}
            </label>
            <Select id="emails-filter" value={filter} onChange={(event) => changeFilter(setFilter)(event.target.value as Filter)}>
              {FILTERS.map((f) => (
                <option key={f} value={f}>
                  {t(`emails.filter.${f}` as MessageKey)}
                </option>
              ))}
            </Select>
          </div>
        </div>
        <form role="search" onSubmit={submitSearch} className="flex gap-2 xl:w-96">
          <label htmlFor="emails-search" className="sr-only">
            {t('emails.search')}
          </label>
          <TextInput
            id="emails-search"
            type="search"
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            placeholder={t('emails.search.placeholder')}
            autoComplete="off"
          />
          <Button type="submit" variant="secondary">
            {t('emails.search.submit')}
          </Button>
        </form>
      </div>

      {list.error ? (
        <ErrorState error={list.error} onRetry={list.reload} what={t('emails.title').toLowerCase()} />
      ) : empty && !list.refreshing ? (
        everSent.data === false && !filtered ? (
          <EmptyState
            icon={<Send className="h-5 w-5" strokeWidth={1.5} />}
            title={t('emails.empty')}
            body={t('emails.empty.body')}
            actions={
              <ButtonLink to="/keys#send" variant="primary">
                {t('emails.empty.action')}
              </ButtonLink>
            }
          />
        ) : (
          <EmptyState
            title={t('emails.emptyFiltered')}
            body={t('emails.emptyFiltered.body')}
            actions={
              filtered ? (
                <Button variant="secondary" onClick={clearFilters}>
                  {t('emails.emptyFiltered.action')}
                </Button>
              ) : undefined
            }
          />
        )
      ) : (
        <>
          {totals && (
            <p className="num mb-3 text-[0.8125rem] text-muted">
              <span className="font-medium text-ink">{tn('emails.count', totals.total, { count: fmt.number(totals.total) })}</span>
              {' · '}
              {t('emails.totals', {
                delivered: fmt.number(totals.delivered),
                opened: fmt.number(totals.opened),
                clicked: fmt.number(totals.clicked),
                bounced: fmt.number(totals.bounced),
              })}
            </p>
          )}
          <TableRegion label={t('emails.caption')} dim={list.refreshing}>
            <table className="w-full min-w-[44rem] text-sm">
              <caption className="sr-only">{t('emails.caption')}</caption>
              <thead>
                <tr>
                  <th scope="col" className={th}>
                    {t('emails.col.recipient')}
                  </th>
                  <th scope="col" className={th}>
                    {t('emails.col.status')}
                  </th>
                  <th scope="col" className={th}>
                    {t('emails.col.engagement')}
                  </th>
                  <th scope="col" className={`${th} text-right`}>
                    {t('emails.col.updated')}
                  </th>
                  <th scope="col" className={`${th} w-10`}>
                    <span className="sr-only">{t('common.details')}</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {list.loading ? (
                  <SkeletonRows rows={8} cols={5} />
                ) : (
                  data?.messages.map((message) => {
                    const opened = message.journey.includes('opened')
                    const clicked = message.journey.includes('clicked')
                    return (
                      <tr
                        key={`${message.message_id}-${message.recipient}`}
                        className={`${tr} cursor-pointer`}
                        onClick={() => setSelected(message)}
                      >
                        <td className={`${td} max-w-[24rem]`}>
                          <button
                            type="button"
                            onClick={(event) => {
                              event.stopPropagation()
                              setSelected(message)
                            }}
                            aria-label={t('emails.open', { email: message.recipient })}
                            className="flex w-full min-w-0 flex-col items-start rounded-md text-left"
                          >
                            <span className="w-full truncate font-medium text-ink">{message.recipient}</span>
                            <span className="w-full truncate text-xs text-muted">{message.subject || t('emails.noSubject')}</span>
                          </button>
                        </td>
                        <td className={td}>
                          <Badge tone={eventTone(message.delivery_status)}>{eventLabel(tMaybe, message.delivery_status)}</Badge>
                        </td>
                        <td className={td}>
                          {opened || clicked ? (
                            <span className="flex flex-wrap items-center gap-3 text-xs text-ink">
                              {opened && (
                                <span className="inline-flex items-center gap-1">
                                  <MailOpen className="h-3.5 w-3.5 text-info-text" strokeWidth={1.75} aria-hidden="true" />
                                  {eventLabel(tMaybe, 'opened')}
                                </span>
                              )}
                              {clicked && (
                                <span className="inline-flex items-center gap-1">
                                  <MousePointerClick className="h-3.5 w-3.5 text-info-text" strokeWidth={1.75} aria-hidden="true" />
                                  {eventLabel(tMaybe, 'clicked')}
                                </span>
                              )}
                            </span>
                          ) : (
                            <span className="text-muted">—</span>
                          )}
                        </td>
                        <td className={`${td} whitespace-nowrap text-right text-muted`}>
                          <time dateTime={message.last_update ?? undefined} title={fmt.dateTime(message.last_update)}>
                            {fmt.relative(message.last_update)}
                          </time>
                        </td>
                        <td className={`${td} text-right`}>
                          <ChevronRight className="ml-auto h-4 w-4 text-muted" strokeWidth={1.5} aria-hidden="true" />
                        </td>
                      </tr>
                    )
                  })
                )}
              </tbody>
            </table>
          </TableRegion>
          {data && data.pagination.total_pages > 1 && (
            <Pagination page={page} totalPages={data.pagination.total_pages} onPage={setPage} />
          )}
        </>
      )}

      <EmailDrawer open={selected !== null} onClose={() => setSelected(null)} message={selected} />
      <EmailDrawer open={lookupId !== null} onClose={() => setLookupId(null)} lookupId={lookupId} />
    </>
  )
}
