import { useEffect, type ReactNode } from 'react'
import { useI18n } from '../i18n'

// Page header, sections, segmented control, pagination and table region.

export function PageHeader({ title, description, actions }: { title: string; description?: ReactNode; actions?: ReactNode }) {
  const { t } = useI18n()
  useEffect(() => {
    document.title = `${title} · ${t('app.name')}`
  }, [title, t])

  return (
    <header className="flex flex-col gap-4 pb-8 md:flex-row md:items-end md:justify-between">
      <div className="min-w-0">
        <h1 id="page-title" tabIndex={-1} className="text-2xl font-semibold tracking-tight text-ink outline-none">
          {title}
        </h1>
        {description && <p className="mt-1.5 max-w-2xl text-sm leading-relaxed text-muted">{description}</p>}
      </div>
      {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
    </header>
  )
}

interface SectionProps {
  id: string
  title: string
  description?: ReactNode
  actions?: ReactNode
  children: ReactNode
  className?: string
}

export function Section({ id, title, description, actions, children, className = '' }: SectionProps) {
  return (
    <section aria-labelledby={`${id}-title`} className={`flex flex-col gap-4 ${className}`}>
      <div className="flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
        <div className="min-w-0">
          <h2 id={`${id}-title`} className="text-base font-semibold text-ink">
            {title}
          </h2>
          {description && <p className="mt-1 max-w-2xl text-sm leading-relaxed text-muted">{description}</p>}
        </div>
        {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
      </div>
      {children}
    </section>
  )
}

interface SegmentedProps<T extends string> {
  name: string
  legend: string
  value: T
  options: { value: T; label: string }[]
  onChange: (value: T) => void
}

/** Single choice built on native radio buttons: arrow keys and screen reader support for free. */
export function Segmented<T extends string>({ name, legend, value, options, onChange }: SegmentedProps<T>) {
  return (
    <fieldset className="min-w-0">
      <legend className="sr-only">{legend}</legend>
      <div className="inline-flex max-w-full flex-wrap gap-1 rounded-xl bg-subtle p-1">
        {options.map((option) => (
          <label key={option.value} className="relative">
            <input
              type="radio"
              name={name}
              value={option.value}
              checked={value === option.value}
              onChange={() => onChange(option.value)}
              className="peer sr-only"
            />
            <span className="inline-flex h-9 cursor-pointer items-center rounded-lg px-3.5 text-[0.8125rem] font-medium text-muted transition-colors duration-150 hover:text-ink peer-checked:bg-surface peer-checked:text-ink peer-checked:shadow-sm peer-focus-visible:outline peer-focus-visible:outline-2 peer-focus-visible:outline-offset-2 peer-focus-visible:outline-accent">
              {option.label}
            </span>
          </label>
        ))}
      </div>
    </fieldset>
  )
}

interface PaginationProps {
  page: number
  totalPages: number
  onPage: (page: number) => void
  summary?: ReactNode
}

export function Pagination({ page, totalPages, onPage, summary }: PaginationProps) {
  const { t } = useI18n()
  const pages = Math.max(1, totalPages)
  return (
    <nav aria-label={t('common.pagination')} className="flex flex-wrap items-center justify-between gap-3 pt-4 text-sm">
      <p className="text-muted">
        {summary}
        {summary ? ' · ' : ''}
        {t('common.pageOf', { page, pages })}
      </p>
      <div className="flex gap-2">
        <button
          type="button"
          onClick={() => onPage(page - 1)}
          disabled={page <= 1}
          className="h-9 rounded-lg border border-line px-3.5 text-[0.8125rem] font-medium text-ink transition-colors duration-150 hover:bg-subtle disabled:cursor-not-allowed disabled:opacity-50"
        >
          {t('common.previous')}
        </button>
        <button
          type="button"
          onClick={() => onPage(page + 1)}
          disabled={page >= pages}
          className="h-9 rounded-lg border border-line px-3.5 text-[0.8125rem] font-medium text-ink transition-colors duration-150 hover:bg-subtle disabled:cursor-not-allowed disabled:opacity-50"
        >
          {t('common.next')}
        </button>
      </div>
    </nav>
  )
}

/** Flat table on the page; on narrow screens it scrolls horizontally and takes keyboard focus. */
export function TableRegion({ label, children, dim }: { label: string; children: ReactNode; dim?: boolean }) {
  return (
    <div
      role="region"
      aria-label={label}
      tabIndex={0}
      className={`-mx-4 overflow-x-auto transition-opacity duration-150 ${dim ? 'opacity-60' : ''}`}
    >
      {children}
    </div>
  )
}

export const th = 'h-11 whitespace-nowrap px-4 text-left align-middle text-xs font-normal text-muted'
export const td = 'px-4 py-3 align-middle'
export const tr = 'border-t border-line/70 transition-colors duration-100 hover:bg-subtle/60'
