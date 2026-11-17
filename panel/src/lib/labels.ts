import { ApiError } from './api'
import type { MessageKey } from '../i18n/pt'

// Turns raw API values into everyday language.

export type Tone = 'success' | 'danger' | 'warning' | 'info' | 'neutral'

const EVENT_TONES: Record<string, Tone> = {
  delivered: 'success',
  opened: 'info',
  clicked: 'info',
  sent: 'neutral',
  queued: 'neutral',
  deferred: 'warning',
  soft_bounced: 'warning',
  bounced: 'danger',
  hard_bounced: 'danger',
  failed: 'danger',
  rejected: 'danger',
  complaint: 'danger',
  unsubscribed: 'neutral',
  suppressed: 'neutral',
  test: 'neutral',
}

export function eventTone(eventType: string): Tone {
  return EVENT_TONES[eventType] ?? 'neutral'
}

/** Event label; if the API sends a type the panel does not know, shows the raw value. */
export function eventLabel(tMaybe: (key: string) => string | null, eventType: string): string {
  return tMaybe(`event.${eventType}`) ?? eventType
}

/** Reads the "class.subject.detail" code (e.g. 5.1.1) from a DSN and explains it in one sentence. */
export function explainDsn(tMaybe: (key: string) => string | null, dsn: string | null | undefined): string | null {
  if (!dsn) return null
  const match = /\b([245])\.(\d{1,3})\.(\d{1,3})\b/.exec(dsn)
  if (!match) return null
  const [, klass, subject, detail] = match
  const code = `${klass}.${subject}.${detail}`
  const candidates = [
    `dsn.${code}`,
    subject === '2' && detail === '2' ? 'dsn.x.2.2' : '',
    `dsn.${klass}.${subject}`,
    subject === '4' ? 'dsn.x.4' : '',
    `dsn.${klass}`,
  ].filter(Boolean)
  for (const key of candidates) {
    const text = tMaybe(key)
    if (text) return text
  }
  return null
}

export const PERMISSION_KEYS: Record<string, MessageKey> = {
  'write:email': 'perm.write:email',
  'read:email': 'perm.read:email',
  'read:stat': 'perm.read:stat',
  'read:webhook': 'perm.read:webhook',
  'write:webhook': 'perm.write:webhook',
  'read:suppression': 'perm.read:suppression',
  'write:suppression': 'perm.write:suppression',
  admin: 'perm.admin',
}

/** Message for the user: network, permission and server errors become fixed sentences. */
export function errorMessage(t: (key: MessageKey) => string, err: unknown): string {
  if (err instanceof ApiError) {
    if (err.status === 0) return t('errors.network')
    if (err.status === 403) return err.message && err.message !== 'Forbidden' ? err.message : t('errors.forbidden')
    if (err.status === 404 && /^not found$/i.test(err.message)) return t('errors.notMounted')
    if (err.status >= 500) return t('errors.server')
    return err.message || t('errors.generic')
  }
  return t('errors.generic')
}

/** A route the API does not have (FastAPI returns {"detail": "Not Found"}). */
export function isMissingRoute(err: unknown): boolean {
  return err instanceof ApiError && err.status === 404 && /^not found$/i.test(err.message)
}

export function isNotFound(err: unknown): boolean {
  return err instanceof ApiError && err.status === 404
}

export const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/
