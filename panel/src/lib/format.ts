import { parseApiDate } from './dates'

// Locale-bound formatters (en: 1,234 · 98.5% · 11/7/26, 2:32 PM; pt-BR: 1.234 · 98,5% · 07/11/2026, 14:32).
export interface Formatters {
  number: (value: number) => string
  compact: (value: number) => string
  /** Value already in percentage points (98.5 → "98.5%"). */
  percent: (value: number, digits?: number) => string
  date: (value: string | Date | null | undefined) => string
  dateTime: (value: string | Date | null | undefined) => string
  shortDay: (value: string | Date) => string
  relative: (value: string | Date | null | undefined) => string
}

function toDate(value: string | Date | null | undefined): Date | null {
  if (!value) return null
  return value instanceof Date ? value : parseApiDate(value)
}

export function makeFormatters(locale: string): Formatters {
  const numberFmt = new Intl.NumberFormat(locale)
  const compactFmt = new Intl.NumberFormat(locale, { notation: 'compact', maximumFractionDigits: 1 })
  const dateFmt = new Intl.DateTimeFormat(locale, { dateStyle: 'medium' })
  const dateTimeFmt = new Intl.DateTimeFormat(locale, { dateStyle: 'short', timeStyle: 'short' })
  const dayFmt =
    locale === 'en'
      ? new Intl.DateTimeFormat(locale, { month: 'short', day: 'numeric' })
      : new Intl.DateTimeFormat(locale, { day: '2-digit', month: '2-digit' })
  const relativeFmt = new Intl.RelativeTimeFormat(locale, { numeric: 'auto' })
  const percentFmts = new Map<number, Intl.NumberFormat>()

  const percent = (value: number, digits = 1) => {
    let fmt = percentFmts.get(digits)
    if (!fmt) {
      fmt = new Intl.NumberFormat(locale, { style: 'percent', maximumFractionDigits: digits, minimumFractionDigits: 0 })
      percentFmts.set(digits, fmt)
    }
    return fmt.format((Number.isFinite(value) ? value : 0) / 100)
  }

  const relative = (value: string | Date | null | undefined) => {
    const date = toDate(value)
    if (!date) return ''
    const seconds = Math.round((date.getTime() - Date.now()) / 1000)
    const abs = Math.abs(seconds)
    if (abs < 45) return relativeFmt.format(0, 'second')
    if (abs < 3600) return relativeFmt.format(Math.round(seconds / 60), 'minute')
    if (abs < 86400) return relativeFmt.format(Math.round(seconds / 3600), 'hour')
    if (abs < 86400 * 30) return relativeFmt.format(Math.round(seconds / 86400), 'day')
    if (abs < 86400 * 365) return relativeFmt.format(Math.round(seconds / (86400 * 30)), 'month')
    return relativeFmt.format(Math.round(seconds / (86400 * 365)), 'year')
  }

  return {
    number: (value) => numberFmt.format(Number.isFinite(value) ? value : 0),
    compact: (value) => (Math.abs(value) < 10000 ? numberFmt.format(value) : compactFmt.format(value)),
    percent,
    date: (value) => {
      const date = toDate(value)
      return date ? dateFmt.format(date) : ''
    },
    dateTime: (value) => {
      const date = toDate(value)
      return date ? dateTimeFmt.format(date) : ''
    },
    shortDay: (value) => {
      const date = toDate(value)
      return date ? dayFmt.format(date) : String(value)
    },
    relative,
  }
}
