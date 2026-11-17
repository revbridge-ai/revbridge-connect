// The API takes and returns days as YYYY-MM-DD (calendar day) and times without a time
// zone. Connect's MySQL runs in UTC (docker-compose.yml: TZ=UTC on the db service) and
// nothing changes the session time zone, so a time without a zone is UTC.

export type PeriodPreset = 'today' | '7d' | '30d' | '90d'

export const PERIOD_PRESETS: PeriodPreset[] = ['today', '7d', '30d', '90d']

const PRESET_DAYS: Record<PeriodPreset, number> = { today: 1, '7d': 7, '30d': 30, '90d': 90 }

export function toISODate(date: Date): string {
  const y = date.getFullYear()
  const m = String(date.getMonth() + 1).padStart(2, '0')
  const d = String(date.getDate()).padStart(2, '0')
  return `${y}-${m}-${d}`
}

/**
 * Inclusive period ending today: "7 days" = today and the 6 days before it.
 * Uses UTC days because the server stores and filters in UTC. With the browser's local day,
 * a user at UTC-3 after 21:00 would ask for a "today" the server has already left behind,
 * and the most recent sends would fall outside the range.
 */
export function rangeForPreset(preset: PeriodPreset, now = new Date()): { start_date: string; end_date: string } {
  const end = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate()))
  const start = new Date(end)
  start.setUTCDate(end.getUTCDate() - (PRESET_DAYS[preset] - 1))
  return { start_date: toUTCISODate(start), end_date: toUTCISODate(end) }
}

function toUTCISODate(date: Date): string {
  return date.toISOString().slice(0, 10)
}

/** "YYYY-MM-DD" becomes local midnight; a date and time without a time zone is read as UTC. */
export function parseApiDate(value: string | null | undefined): Date | null {
  if (!value) return null
  const dateOnly = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value)
  if (dateOnly) return new Date(Number(dateOnly[1]), Number(dateOnly[2]) - 1, Number(dateOnly[3]))
  let iso = value.trim().replace(' ', 'T')
  if (/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d+)?)?$/.test(iso)) iso += 'Z'
  const parsed = new Date(iso)
  return Number.isNaN(parsed.getTime()) ? null : parsed
}

export function addDays(date: Date, days: number): Date {
  const next = new Date(date)
  next.setDate(next.getDate() + days)
  return next
}
