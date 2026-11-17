import { request } from './api'
import type {
  ActivityResponse,
  AllowanceResponse,
  ApiKey,
  ApiKeyChannel,
  ApiKeyCreateBody,
  ApiKeyCreated,
  ApiKeyListResponse,
  DashboardResponse,
  DkimKey,
  DomainDnsInfoResponse,
  EmailConfig,
  EmailConfigUpdate,
  EmailStatusResponse,
  ExecutionStatusResponse,
  LoginResponse,
  MeResponse,
  MessageHeadersResponse,
  MessagesResponse,
  PaginationMeta,
  SuppressionListResponse,
  SuppressionType,
  TimeseriesResponse,
  Webhook,
  WebhookCreateBody,
  WebhookEventRecord,
  WebhookUpdateBody,
} from './types'

// Every API call the panel makes, in one place. Paths are given without the /api prefix.

export interface DateRange {
  start_date: string
  end_date: string
}

const enc = encodeURIComponent

// ── authentication (connect_lib/auth/router.py, mounted at /auth) ────────────
export const authApi = {
  login: (email: string, password: string) =>
    request<LoginResponse>('/auth/login', { method: 'POST', body: { email, password }, auth: false }),
  me: (signal?: AbortSignal) => request<MeResponse>('/auth/me', { signal }),
}

// ── reports (stats_router.py, prefix /stats) ─────────────────────────────────
export interface ActivityQuery extends Partial<DateRange> {
  page?: number
  limit?: number
  event_type?: string
  status?: string
}

export interface MessagesQuery extends Partial<DateRange> {
  page: number
  limit: number
  search?: string
  /** delivered | opened | clicked | bounced | failed | deferred (takes precedence over status) */
  event_type?: string
  /** success | error | pending */
  status?: string
}

export const statsApi = {
  dashboard: (range: DateRange, signal?: AbortSignal) =>
    request<DashboardResponse>('/stats/dashboard', { query: { ...range, granularity: 'day' }, signal }),
  timeseries: (range: DateRange, signal?: AbortSignal) =>
    request<TimeseriesResponse>('/stats/timeseries', { query: { ...range, granularity: 'day' }, signal }),
  messages: (query: MessagesQuery, signal?: AbortSignal) =>
    request<MessagesResponse>('/stats/messages', { query: { ...query }, signal }),
  activity: (query: ActivityQuery, signal?: AbortSignal) =>
    request<ActivityResponse>('/stats/activity', { query: { ...query }, signal }),
  /** With `rcpt`, `to` is that copy's recipient instead of every recipient of the send. */
  messageHeaders: (messageId: string, rcpt?: string, signal?: AbortSignal) =>
    request<MessageHeadersResponse>('/stats/message/headers', {
      query: rcpt ? { message_id: messageId, rcpt } : { message_id: messageId },
      signal,
    }),
}

/** The /stats/activity response nests the pagination; this returns the right object. */
export function activityPagination(res: ActivityResponse): PaginationMeta {
  const p = res.pagination as PaginationMeta & { pagination?: PaginationMeta }
  return p.pagination ?? p
}

// ── sending (email_router.py, no prefix) ─────────────────────────────────────
export const emailApi = {
  status: (messageId: string, signal?: AbortSignal) =>
    request<EmailStatusResponse>(`/status/${enc(messageId)}`, { signal }),
  executionStatus: (messageId: string, signal?: AbortSignal) =>
    request<ExecutionStatusResponse>(`/execution-status/${enc(messageId)}`, { signal }),
}

// ── volume (volume_router.py, prefix /volume) ────────────────────────────────
export const volumeApi = {
  allowance: (signal?: AbortSignal) => request<AllowanceResponse>('/volume/allowance', { signal }),
}

// ── API keys (connect_lib/api_key/router.py, mounted at /api-keys) ───────────
export const apiKeysApi = {
  list: (signal?: AbortSignal) => request<ApiKeyListResponse>('/api-keys', { query: { page: 1, limit: 100 }, signal }),
  create: (body: ApiKeyCreateBody) => request<ApiKeyCreated>('/api-keys', { method: 'POST', body }),
  // The library's POST /api-keys ignores "channels"; the email channel is added here.
  addEmailChannel: (apiKeyId: string, domainId: number) =>
    request<ApiKeyChannel>(`/api-keys/${enc(apiKeyId)}/channels`, {
      method: 'POST',
      body: { channel: 'email', config: { domain_id: domainId, include_subdomains: true }, is_active: true },
    }),
  setActive: (id: number, isActive: boolean) =>
    request<ApiKey>(`/api-keys/${id}`, { method: 'PUT', body: { is_active: isActive } }),
  remove: (id: number) => request<void>(`/api-keys/${id}`, { method: 'DELETE' }),
}

// ── domain: DKIM (dkim_key_router.py) and email configs (email_config_router.py) ─
export const dkimApi = {
  list: (domain: string, signal?: AbortSignal) =>
    request<DkimKey[]>('/dkim/keys', { query: { dkim_domain: domain }, signal }),
  // Idempotent POST: for the same (cluster_id, selector, dkim_domain) it returns the existing
  // key, with dns_record_value derived from the stored PEM (dkim_key_service.py).
  // It neither creates nor replaces a key that already exists.
  revealRecord: (key: Pick<DkimKey, 'cluster_id' | 'selector' | 'dkim_domain'>) =>
    request<DkimKey>('/dkim/keys', {
      method: 'POST',
      body: { cluster_id: key.cluster_id, selector: key.selector, dkim_domain: key.dkim_domain, generate_key: true },
    }),
}

export const emailConfigApi = {
  list: (signal?: AbortSignal) => request<EmailConfig[]>('/email-configs', { signal }),
  update: (configId: string, body: EmailConfigUpdate) =>
    request<EmailConfig>(`/email-configs/${enc(configId)}`, { method: 'PUT', body }),
  toggle: (configId: string) => request<EmailConfig>(`/email-configs/${enc(configId)}/toggle`, { method: 'PATCH' }),
}

// DNS records of the sending domain (connect_domains_router.py, GET /domains/{domain}/dns-info).
// If this call fails (e.g. an older API without the route), the Domains page falls back to
// what it can build from /dkim/keys.
export const domainApi = {
  dnsInfo: (domain: string, signal?: AbortSignal) =>
    request<DomainDnsInfoResponse>(`/domains/${enc(domain)}/dns-info`, { signal }),
}

// ── suppressions (suppressions_router.py, prefix /suppressions) ──────────────
export interface SuppressionQuery {
  page: number
  limit: number
  email?: string
  suppression_type?: SuppressionType
  is_active?: boolean
}

export const suppressionsApi = {
  list: (query: SuppressionQuery, signal?: AbortSignal) =>
    request<SuppressionListResponse>('/suppressions', { query: { ...query }, signal }),
  create: (body: { email: string; reason: string; domain_name: string }) =>
    request<{ success: boolean; message: string }>('/suppressions', {
      method: 'POST',
      body: { ...body, suppression_type: 'manual' },
    }),
  remove: (id: number) => request<{ success: boolean; message: string }>(`/suppressions/${id}`, { method: 'DELETE' }),
}

// ── webhooks (webhook_router.py and events_router.py, prefix /webhooks) ──────
export const webhooksApi = {
  list: (signal?: AbortSignal) => request<Webhook[]>('/webhooks', { query: { service_type: 'email' }, signal }),
  create: (body: WebhookCreateBody) => request<Webhook>('/webhooks', { method: 'POST', body }),
  update: (id: number, body: WebhookUpdateBody) => request<Webhook>(`/webhooks/${id}`, { method: 'PUT', body }),
  // is_active goes in the query string: the handler declares "is_active: bool" without Body().
  toggle: (id: number, isActive: boolean) =>
    request<Webhook>(`/webhooks/${id}/toggle`, { method: 'PATCH', query: { is_active: isActive } }),
  remove: (id: number) => request<void>(`/webhooks/${id}`, { method: 'DELETE' }),
  // WebhookTestRequest requires webhook_id whenever a body is sent.
  test: (id: number) =>
    request<{ message: string; webhook_id: number }>(`/webhooks/${id}/test`, { method: 'POST', body: { webhook_id: id } }),
  events: (id: number, signal?: AbortSignal) =>
    request<WebhookEventRecord[]>(`/webhooks/${id}/events`, { query: { limit: 10 }, signal }),
}
