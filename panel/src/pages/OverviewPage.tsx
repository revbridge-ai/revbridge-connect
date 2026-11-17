import { useMemo, useState, type ReactNode } from 'react'
import { ArrowDownRight, ArrowRight, ArrowUpRight, Check } from 'lucide-react'
import { Link } from 'react-router-dom'
import { useI18n } from '../i18n'
import { useUser } from '../lib/auth'
import { useAsync } from '../lib/useAsync'
import { activityPagination, apiKeysApi, statsApi, volumeApi } from '../lib/endpoints'
import { PERIOD_PRESETS, rangeForPreset, type PeriodPreset } from '../lib/dates'
import type { AllowanceResponse, DashboardData, TimeseriesPoint } from '../lib/types'
import type { MessageKey } from '../i18n/pt'
import { PageHeader, Section, Segmented } from '../components/Layout'
import { Badge, ErrorState, Skeleton } from '../components/Feedback'
import { TimeSeriesChart, type ChartSeries } from '../components/charts/TimeSeriesChart'
import type { Tone } from '../lib/labels'

type SeriesKey = 'sent' | 'delivered' | 'opened' | 'clicked'

export function OverviewPage() {
  const { t } = useI18n()
  const user = useUser()
  const [preset, setPreset] = useState<PeriodPreset>('30d')
  const range = useMemo(() => rangeForPreset(preset), [preset])

  const dashboard = useAsync((signal) => statsApi.dashboard(range, signal), [range.start_date, range.end_date])
  const timeseries = useAsync((signal) => statsApi.timeseries(range, signal), [range.start_date, range.end_date])
  // Unsubscribes: "unsubscribed" events in the period. (The Connect dashboard also returns
  // "unsubscribes" and "unsubscribe_rate"; this count stays consistent with the Emails page.)
  const unsubs = useAsync(
    (signal) =>
      statsApi
        .activity({ event_type: 'unsubscribed', start_date: range.start_date, end_date: range.end_date, page: 1, limit: 1 }, signal)
        .then((res) => activityPagination(res).total),
    [range.start_date, range.end_date],
  )
  const allowance = useAsync((signal) => volumeApi.allowance(signal), [])
  // Getting-started steps: only mark as done what can be checked through the API.
  const setup = useAsync(async (signal) => {
    const [keys, messages] = await Promise.allSettled([
      apiKeysApi.list(signal),
      statsApi.messages({ page: 1, limit: 1 }, signal),
    ])
    return {
      hasKey: keys.status === 'fulfilled' ? keys.value.total > 0 : null,
      hasSend: messages.status === 'fulfilled' ? messages.value.pagination.total > 0 : null,
    }
  }, [])

  const periodLabel = t(`period.desc.${preset}` as MessageKey)
  const showSetup = setup.data && (setup.data.hasKey === false || setup.data.hasSend === false)

  return (
    <>
      <PageHeader
        title={t('overview.title')}
        description={t('overview.subtitle', { domain: user.domain, period: periodLabel })}
        actions={
          <Segmented
            name="period"
            legend={t('period.label')}
            value={preset}
            onChange={setPreset}
            options={PERIOD_PRESETS.map((p) => ({ value: p, label: t(`period.${p}` as MessageKey) }))}
          />
        }
      />

      <div className="flex flex-col gap-12">
        {showSetup && setup.data && <SetupSteps hasKey={setup.data.hasKey} hasSend={setup.data.hasSend} />}

        {dashboard.error ? (
          <ErrorState error={dashboard.error} onRetry={dashboard.reload} what={t('overview.results').toLowerCase()} />
        ) : (
          <Kpis
            data={dashboard.data?.data}
            loading={dashboard.loading}
            dim={dashboard.refreshing}
            unsubs={unsubs.data}
            unsubsFailed={!!unsubs.error}
            comparable={preset !== 'today'}
          />
        )}

        <Section id="chart" title={t('chart.title')}>
          <ChartBlock
            points={timeseries.data?.data}
            loading={timeseries.loading}
            dim={timeseries.refreshing}
            error={timeseries.error}
            onRetry={timeseries.reload}
            singleDay={preset === 'today'}
          />
        </Section>

        <div className="grid gap-12 lg:grid-cols-2 lg:gap-10">
          <HealthBlock data={dashboard.data?.data} loading={dashboard.loading} />
          <VolumeBlock data={allowance.data} loading={allowance.loading} error={allowance.error} onRetry={allowance.reload} domain={user.domain} />
        </div>
      </div>
    </>
  )
}

// ── getting started ──────────────────────────────────────────────────────────

function SetupSteps({ hasKey, hasSend }: { hasKey: boolean | null; hasSend: boolean | null }) {
  const { t } = useI18n()
  const steps: { title: MessageKey; desc: MessageKey; to: string; action: MessageKey; done: boolean | null; neutral?: MessageKey }[] = [
    { title: 'setup.dns', desc: 'setup.dns.desc', to: '/domains', action: 'setup.dns.action', done: null, neutral: 'setup.dns.status' },
    { title: 'setup.key', desc: 'setup.key.desc', to: '/keys?new=1', action: 'setup.key.action', done: hasKey },
    { title: 'setup.send', desc: 'setup.send.desc', to: '/keys#send', action: 'setup.send.action', done: hasSend },
  ]
  return (
    <section aria-labelledby="setup-title" className="rounded-xl border border-line">
      <div className="px-5 pb-2 pt-5">
        <h2 id="setup-title" className="text-base font-semibold text-ink">
          {t('setup.title')}
        </h2>
        <p className="mt-1 text-sm text-muted">{t('setup.subtitle')}</p>
      </div>
      <ol className="flex flex-col">
        {steps.map((step, index) => (
          <li key={step.title} className="flex flex-col gap-3 border-t border-line/70 px-5 py-4 first:border-t-0 sm:flex-row sm:items-center">
            <span
              aria-hidden="true"
              className={`inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-full text-xs font-semibold ${
                step.done ? 'bg-success/15 text-success-text' : 'bg-subtle text-muted'
              }`}
            >
              {step.done ? <Check className="h-4 w-4" strokeWidth={2.25} /> : index + 1}
            </span>
            <div className="min-w-0 flex-1">
              <p className="text-sm font-medium text-ink">{t(step.title)}</p>
              <p className="text-[0.8125rem] text-muted">{t(step.desc)}</p>
            </div>
            <div className="flex items-center gap-3 sm:justify-end">
              <span className="text-xs text-muted">
                {step.done === true ? t('setup.done') : step.done === false ? t('setup.pending') : step.neutral ? t(step.neutral) : ''}
              </span>
              {step.done !== true && (
                <Link
                  to={step.to}
                  className="inline-flex h-9 items-center gap-1.5 rounded-lg px-3 text-[0.8125rem] font-medium text-primary-text hover:bg-subtle"
                >
                  {t(step.action)}
                  <ArrowRight className="h-3.5 w-3.5" strokeWidth={1.75} aria-hidden="true" />
                </Link>
              )}
            </div>
          </li>
        ))}
      </ol>
    </section>
  )
}

// ── KPIs ─────────────────────────────────────────────────────────────────────

interface KpiItem {
  label: string
  value: string
  hint: ReactNode
  before?: { text: string; better: boolean | null }
}

function KpiStrip({ items, loading, dim }: { items: KpiItem[]; loading: boolean; dim: boolean }) {
  const { t } = useI18n()
  // Each cell draws its right and bottom borders; the ones on the last column/row fall
  // outside the box (-mr-px/-mb-px) and are clipped. No gaps at fractional widths.
  const cols = items.length === 3 ? 'grid-cols-1 sm:grid-cols-3' : 'grid-cols-2 md:grid-cols-4'
  return (
    <div className={`overflow-hidden rounded-xl border border-line transition-opacity duration-150 ${dim ? 'opacity-60' : ''}`}>
      <dl className={`-mb-px -mr-px grid ${cols}`}>
      {items.map((item) => (
        <div key={item.label} className="flex min-w-0 flex-col gap-1 border-b border-r border-line p-4 sm:p-5">
          <dt className="text-[0.8125rem] text-muted">{item.label}</dt>
          {loading ? (
            <dd>
              <Skeleton className="mt-1 h-8 w-20" />
              <Skeleton className="mt-2.5 h-3 w-32" />
            </dd>
          ) : (
            <>
              <dd className="text-[1.75rem] font-semibold leading-tight tracking-[-0.02em] text-ink">{item.value}</dd>
              <dd className="text-xs leading-relaxed text-muted">{item.hint}</dd>
              {item.before && (
                <dd className="mt-0.5 flex items-center gap-1 text-xs text-muted">
                  {item.before.better === true && (
                    <ArrowUpRight className="h-3.5 w-3.5 text-success-text" strokeWidth={2} aria-hidden="true" />
                  )}
                  {item.before.better === false && (
                    <ArrowDownRight className="h-3.5 w-3.5 text-danger-text" strokeWidth={2} aria-hidden="true" />
                  )}
                  {item.before.better !== null && (
                    <span className="sr-only">{item.before.better ? t('kpi.better') : t('kpi.worse')}, </span>
                  )}
                  {item.before.text}
                </dd>
              )}
            </>
          )}
        </div>
      ))}
      </dl>
    </div>
  )
}

interface KpisProps {
  data: DashboardData | undefined
  loading: boolean
  dim: boolean
  unsubs: number | undefined
  unsubsFailed: boolean
  comparable: boolean
}

function Kpis({ data, loading, dim, unsubs, unsubsFailed, comparable }: KpisProps) {
  const { t, fmt } = useI18n()
  const s = data?.summary
  const c = data?.comparison
  const sent = s?.emails_sent ?? 0
  const delivered = s?.emails_delivered ?? 0
  const prevSent = c?.emails_sent.previous ?? 0
  // With no sends in the previous period there is nothing to compare against.
  const compare = comparable && prevSent > 0

  const rate = (value: number | undefined, base: number) => (base > 0 && value !== undefined ? fmt.percent(value) : '—')
  const before = (current: number, previous: number, asPercent: boolean): KpiItem['before'] =>
    compare
      ? {
          text: t('kpi.before', { value: asPercent ? fmt.percent(previous) : fmt.number(previous) }),
          better: current === previous ? null : current > previous,
        }
      : undefined

  const results: KpiItem[] = [
    {
      label: t('kpi.sent'),
      value: fmt.number(sent),
      hint: t('kpi.sent.hint'),
      before: c ? before(c.emails_sent.current, c.emails_sent.previous, false) : undefined,
    },
    {
      label: t('kpi.delivered'),
      value: rate(s?.delivery_rate, sent),
      hint: t('kpi.delivered.hint', { count: fmt.number(delivered) }),
      before: c && sent > 0 ? before(c.delivery_rate.current, c.delivery_rate.previous, true) : undefined,
    },
    {
      label: t('kpi.opens'),
      value: rate(s?.open_rate, delivered),
      hint: t('kpi.opens.hint'),
      before: c && delivered > 0 ? before(c.open_rate.current, c.open_rate.previous, true) : undefined,
    },
    {
      label: t('kpi.clicks'),
      value: rate(s?.click_rate, delivered),
      hint: t('kpi.clicks.hint'),
      before: c && delivered > 0 ? before(c.click_rate.current, c.click_rate.previous, true) : undefined,
    },
  ]

  const unsubRate = unsubs !== undefined && delivered > 0 ? fmt.percent((unsubs / delivered) * 100, 2) : null
  const problems: KpiItem[] = [
    { label: t('kpi.bounces'), value: rate(s?.bounce_rate, sent), hint: t('kpi.bounces.hint') },
    // Complaints = spam reports (ARF) from the providers' feedback loops (FBL), received at
    // bounces+fbl@rbm<n>.<domain>. Gmail does not send per-message complaints.
    {
      label: t('kpi.complaints'),
      value: s?.complaints === undefined ? '—' : fmt.number(s.complaints),
      hint: s?.complaints ? t('kpi.complaints.hint', { rate: fmt.percent(s.spam_rate, 2) }) : t('kpi.complaints.hintNone'),
    },
    {
      label: t('kpi.unsubs'),
      value: unsubsFailed ? '—' : unsubs === undefined ? '…' : fmt.number(unsubs),
      hint: unsubsFailed ? t('kpi.unavailable') : unsubRate ? t('kpi.unsubs.hint', { rate: unsubRate }) : t('kpi.unsubs.hintNoRate'),
    },
  ]

  return (
    <div className="flex flex-col gap-8">
      <section aria-labelledby="kpi-results" className="flex flex-col gap-3">
        <h2 id="kpi-results" className="text-base font-semibold text-ink">
          {t('overview.results')}
        </h2>
        <KpiStrip items={results} loading={loading} dim={dim} />
      </section>
      <section aria-labelledby="kpi-problems" className="flex flex-col gap-3">
        <h2 id="kpi-problems" className="text-base font-semibold text-ink">
          {t('overview.problems')}
        </h2>
        <KpiStrip items={problems} loading={loading} dim={dim} />
      </section>
    </div>
  )
}

// ── chart ────────────────────────────────────────────────────────────────────

interface ChartBlockProps {
  points: TimeseriesPoint[] | undefined
  loading: boolean
  dim: boolean
  error: unknown
  onRetry: () => void
  singleDay: boolean
}

function ChartBlock({ points, loading, dim, error, onRetry, singleDay }: ChartBlockProps) {
  const { t } = useI18n()
  const series: ChartSeries<SeriesKey>[] = [
    { key: 'sent', label: t('chart.sent'), color: 'var(--series-1)' },
    { key: 'delivered', label: t('chart.delivered'), color: 'var(--series-2)' },
    { key: 'opened', label: t('chart.opened'), color: 'var(--series-3)' },
    { key: 'clicked', label: t('chart.clicked'), color: 'var(--series-4)' },
  ]

  if (error) return <ErrorState error={error} onRetry={onRetry} what={t('chart.title').toLowerCase()} />
  if (singleDay) return <p className="rounded-xl border border-dashed border-line px-5 py-8 text-sm text-muted">{t('chart.needsDays')}</p>
  if (loading || !points) return <Skeleton className="h-[260px] w-full rounded-xl" />

  const chartPoints = points.map((p) => ({
    date: p.date,
    values: { sent: p.sent ?? 0, delivered: p.delivered ?? 0, opened: p.opened ?? 0, clicked: p.clicked ?? 0 },
  }))
  return <TimeSeriesChart series={series} points={chartPoints} dim={dim} />
}

// ── health and volume ────────────────────────────────────────────────────────

const HEALTH_TONE: Record<string, Tone> = {
  excellent: 'success',
  good: 'success',
  fair: 'warning',
  poor: 'danger',
  critical: 'danger',
  unknown: 'neutral',
}

function HealthBlock({ data, loading }: { data: DashboardData | undefined; loading: boolean }) {
  const { t, tMaybe } = useI18n()
  const score = data?.domain_health?.reputation_score ?? 'unknown'
  const known = score in HEALTH_TONE ? score : 'unknown'
  return (
    <Section id="health" title={t('health.title')} description={t('health.window')}>
      {loading ? (
        <div className="flex flex-col gap-2">
          <Skeleton className="h-6 w-24" />
          <Skeleton className="h-4 w-64" />
        </div>
      ) : (
        <div className="flex flex-col items-start gap-2.5">
          <Badge tone={HEALTH_TONE[known]}>{tMaybe(`health.${known}`) ?? known}</Badge>
          <p className="text-sm leading-relaxed text-ink">{tMaybe(`health.${known}.desc`)}</p>
          <p className="text-xs text-muted">{t('health.basis')}</p>
        </div>
      )}
    </Section>
  )
}

interface VolumeBlockProps {
  data: AllowanceResponse | undefined
  loading: boolean
  error: unknown
  onRetry: () => void
  domain: string
}

function VolumeBlock({ data, loading, error, onRetry, domain }: VolumeBlockProps) {
  const { t, tMaybe, fmt } = useI18n()
  let body: ReactNode
  if (error) body = <ErrorState error={error} onRetry={onRetry} />
  else if (loading || !data) {
    body = (
      <div className="flex flex-col gap-2">
        <Skeleton className="h-4 w-56" />
        <Skeleton className="h-2 w-full" />
      </div>
    )
  } else if (!data.governed) {
    body = <p className="text-sm leading-relaxed text-ink">{t('volume.ungoverned', { domain })}</p>
  } else if (data.exempt) {
    body = <p className="text-sm leading-relaxed text-ink">{t('volume.exempt')}</p>
  } else {
    const quota = data.daily_quota ?? 0
    const used = data.consumed_today ?? 0
    const left = data.remaining_today ?? Math.max(0, quota - used)
    const pct = quota > 0 ? Math.min(100, (used / quota) * 100) : 0
    const fill = pct >= 90 ? 'bg-warning' : 'bg-info'
    body = (
      <div className="flex flex-col gap-3">
        <p className="text-sm font-medium text-ink">{t('volume.quota', { quota: fmt.number(quota) })}</p>
        <div
          role="meter"
          aria-label={t('volume.meter')}
          aria-valuemin={0}
          aria-valuemax={quota}
          aria-valuenow={Math.min(used, quota)}
          aria-valuetext={t('volume.used', { used: fmt.number(used), left: fmt.number(left) })}
          className="h-2 w-full overflow-hidden rounded-full bg-info/15"
        >
          <div className={`h-full rounded-full ${fill}`} style={{ width: `${pct}%` }} />
        </div>
        <p className="num text-xs text-muted">{t('volume.used', { used: fmt.number(used), left: fmt.number(left) })}</p>
        {data.state && tMaybe(`volume.state.${data.state}`) && (
          <p className="text-sm leading-relaxed text-ink">{tMaybe(`volume.state.${data.state}`)}</p>
        )}
        <p className="text-xs leading-relaxed text-muted">{t('volume.over')}</p>
        {data.mode === 'dryrun' && <p className="text-xs leading-relaxed text-muted">{t('volume.dryrun')}</p>}
      </div>
    )
  }
  return (
    <Section id="volume" title={t('volume.title')}>
      {body}
    </Section>
  )
}
