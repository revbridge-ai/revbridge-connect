// API response shapes, copied from the mail-api code (services/api) and connect-lib
// (libs/connect-lib). Each block names its source file; when that file changes, update it here.

// connect_lib/auth/router.py — POST /auth/login
export interface LoginUser {
  user_id: string
  username: string
  email: string
  client_id: string | null
  domain_id: string
  domain: string
  role: string
}

export interface LoginResponse {
  access_token: string
  token_type: string
  expires_in: number
  user: LoginUser
}

// connect_lib/auth/router.py — GET /auth/me
export interface MeResponse {
  user_id: string
  username: string
  client_id: string | null
  domain_id: string
  role: string
  iat?: number
  exp?: number
}

// stats_router.py — GET /stats/dashboard
export interface DashboardComparisonValue {
  current: number
  previous: number
  change: string
}

export type ReputationScore = 'excellent' | 'good' | 'fair' | 'poor' | 'critical' | 'unknown'

export interface DashboardData {
  domain_name: string
  period: { start_date: string; end_date: string; granularity: string }
  summary: {
    emails_sent: number
    emails_delivered: number
    delivery_rate: number
    open_rate: number
    human_open_rate: number
    click_rate: number
    bounce_rate: number
    open_avg_confidence_score: number
    // RevBridge Connect: measured values (complaints = ARF/FBL reports; unsubscribes = opt-out requests)
    spam_rate: number
    unsubscribe_rate: number
    complaints?: number
    unsubscribes?: number
  }
  delivery_metrics: {
    total_events: number
    unique_messages: number
    unique_recipients: number
    average_attempts: number
    max_attempts: number
  }
  comparison: {
    period: { current: { start: string; end: string }; previous: { start: string; end: string } }
    emails_sent: DashboardComparisonValue
    delivery_rate: DashboardComparisonValue
    open_rate: DashboardComparisonValue
    human_open_rate: DashboardComparisonValue
    open_avg_confidence_score: DashboardComparisonValue
    click_rate: DashboardComparisonValue
  }
  domain_health: {
    reputation_score: ReputationScore | string
    dns_status: string
    dkim_status: string
    spf_status: string
    overall_status: string
    last_check: string
    error?: string
  }
}

export interface DashboardResponse {
  status: string
  data: DashboardData
  timestamp: string
}

// stats_router.py — GET /stats/timeseries
export interface TimeseriesPoint {
  date: string
  sent: number
  delivered: number
  opened: number
  clicked: number
  opened_human: number
  bounced: number
  deferred: number
  failed: number
  // unsubscribed and spam_complaints exist in the response but are placeholders
  // (they count 1 per period with deliveries). The panel does not use them.
  delivery_rate: number
  open_rate: number
  click_rate: number
  bounce_rate: number
}

export interface TimeseriesResponse {
  status: string
  domain_name: string
  period: { start_date: string; end_date: string; granularity: string }
  data: TimeseriesPoint[]
}

// stats_router.py — GET /stats/activity
export type ActivityEventType =
  | 'delivered'
  | 'bounced'
  | 'deferred'
  | 'failed'
  | 'sent'
  | 'opened'
  | 'clicked'
  | 'unsubscribed'
  | string

export interface ActivityItem {
  id: number
  timestamp: string | null
  event_type: ActivityEventType
  message_id: string
  recipient: { email: string; name: string | null }
  sender: { email: string; name: string | null }
  subject: string | null
  status: 'success' | 'error' | 'pending' | string
  metadata: {
    ip_address?: string
    user_agent?: string
    bounce_reason?: string
    click_url?: string
    queue_id?: string
    attempt?: number
    host?: string
    service?: string
    open_confidence_score?: number
    open_source?: string
    open_status?: string
  }
}

export interface PaginationMeta {
  page: number
  per_page?: number
  limit?: number
  total: number
  total_pages: number
  has_next: boolean
  has_prev: boolean
}

export interface ActivityResponse {
  status: string
  domain_name: string
  // The backend nests it: build_pagination_metadata() returns {"pagination": {...}} and the
  // handler puts that inside "pagination". Both shapes are accepted.
  pagination: PaginationMeta | { pagination: PaginationMeta }
  activities: ActivityItem[]
}

// stats_router.py — GET /stats/messages (in Connect: stats_mysql.messages_from_mysql, same shape)
export interface MessageTimelineEvent {
  event_type: string
  timestamp: string | null
  metadata: {
    dsn?: string
    host?: string
    ip_address?: string
    user_agent?: string
    click_url?: string
    open_source?: string
    open_status?: string
    open_confidence_score?: number
    is_bot?: boolean
    bot_reason?: string | null
    [key: string]: unknown
  }
}

export interface MessageItem {
  message_id: string
  recipient: string
  domain_name: string
  subject: string | null
  sender_email: string | null
  /** MySQL: delivered | bounced | deferred | failed | sent | queued */
  delivery_status: string
  first_seen: string | null
  last_update: string | null
  journey: string[]
  last_event: MessageTimelineEvent | null
  timeline: MessageTimelineEvent[]
}

export interface MessagesTotals {
  total: number
  sent: number
  delivered: number
  opened: number
  clicked: number
  bounced: number
  failed: number
  deferred: number
}

export interface MessagesResponse {
  status: string
  domain_name: string
  pagination: { page: number; limit: number; total: number; total_pages: number; has_next: boolean; has_prev: boolean }
  totals: MessagesTotals
  messages: MessageItem[]
  source?: string
}

// stats_router.py — GET /stats/message/headers
export interface MessageHeadersResponse {
  status: string
  message_id: string
  headers: Record<string, string | string[] | null>
  subject: string | null
  sender_email: string | null
  raw: string
  source?: string
}

// email_router.py — GET /status/{message_id}
export interface EmailStatusResponse {
  message_id: string
  recipient: string
  status: string
  dsn: string | null
  attempt: number | null
  ts: string | null
}

// email_router.py — GET /execution-status/{message_id}
export interface ExecutionStatusResponse {
  message_id: string
  execution_status: 'queued' | 'running' | 'succeeded' | 'failed' | 'not_found' | string
  in_queue: boolean
  in_delivery_events: boolean
  delivery_status: string | null
  last_event_timestamp: string | null
}

// volume_router.py + services/volume_governor.py — GET /volume/allowance
export interface AllowanceResponse {
  domain: string
  governed: boolean
  mode: string
  governed_key?: string
  state?: 'active' | 'braked' | 'cold' | string
  exempt?: boolean
  daily_quota?: number
  consumed_today?: number
  remaining_today?: number
}

// connect_lib/api_key/models.py
export interface ApiKeyChannel {
  id: number
  channel: string
  display_name: string
  is_active: boolean
  config: Record<string, unknown> | null
}

export interface ApiKey {
  id: number
  api_key_id: string
  name: string
  permissions: string[]
  is_active: boolean
  last_used_at: string | null
  expires_at: string | null
  created_at: string
  updated_at: string | null
  owner_id: string | null
  client_id: string | null
  domain_id: number | null
  channels?: ApiKeyChannel[] | null
  enabled_channels?: string[] | null
}

export interface ApiKeyCreated extends Omit<ApiKey, 'last_used_at' | 'updated_at'> {
  api_key: string
}

export interface ApiKeyListResponse {
  api_keys: ApiKey[]
  total: number
  page: number
  limit: number
}

export interface ApiKeyCreateBody {
  name: string
  permissions: string[]
  expires_at?: string | null
}

// models/dkim_models.py — /dkim/keys
export interface DkimKey {
  id: number
  cluster_id: string
  selector: string
  dkim_domain: string
  private_key_path: string
  public_key: string | null
  dns_record_name: string | null
  dns_record_value: string | null
  created_at: string
  updated_at: string
}

// models/email_config_models.py — /email-configs
export interface EmailConfig {
  id: number
  config_id: string
  domain_id: number
  name: string
  smtp_host: string
  smtp_port: number
  tracking_enabled: boolean
  inject_pixel: boolean
  rewrite_links: boolean
  tracking_domain: string | null
  default_from_name: string | null
  default_from_email: string | null
  default_reply_to: string | null
  rate_limit_per_minute: number
  rate_limit_per_hour: number
  rate_limit_per_day: number
  is_active: boolean
  is_default: boolean
  priority: number
  notes: string | null
  created_at: string
  updated_at: string
}

export type EmailConfigUpdate = Partial<
  Pick<
    EmailConfig,
    'default_from_name' | 'default_from_email' | 'default_reply_to' | 'tracking_enabled' | 'inject_pixel' | 'rewrite_links'
  >
>

// connect_domains_router.py — GET /domains/{domain}/dns-info
export interface DnsRecordInfo {
  type: string
  name: string
  value: string
  ttl?: string
}

export interface DomainDnsInfoResponse {
  domain: string
  virtual_domain_name: string
  zone_name: string
  zone_status: string | null
  zone_target: string
  dns_records: DnsRecordInfo[]
}

// models/tracking_models.py — /suppressions
export interface Suppression {
  id: number
  email: string
  reason: string
  created_at: string
  updated_at: string
  // RevBridge Connect: the API also returns the type and domain of each suppression
  suppression_type?: SuppressionType
  domain_name?: string
  is_active?: boolean
}

export interface SuppressionListResponse {
  suppressions: Suppression[]
  total: number
  page: number
  per_page: number
  total_pages: number
}

export type SuppressionType = 'unsubscribe' | 'bounce' | 'complaint' | 'manual'

// models/webhook_models.py — /webhooks
export interface Webhook {
  id: number
  webhook_id: string
  name: string
  url: string
  events: string[]
  secret_key: string | null
  is_active: boolean
  retry_count: number
  timeout_seconds: number
  headers: Record<string, string> | null
  created_at: string
  updated_at: string
  created_by: string | null
  service_type: string
  domain_name: string | null
  domain_id: number | null
}

export interface WebhookCreateBody {
  name: string
  url: string
  events: string[]
  secret_key?: string | null
  service_type: 'email'
}

export type WebhookUpdateBody = Partial<Pick<WebhookCreateBody, 'name' | 'url' | 'events' | 'secret_key'>>

export interface WebhookEventRecord {
  id: number
  webhook_id: number
  event_type: string
  message_id: string
  status: 'pending' | 'success' | 'failed' | 'retrying' | string
  response_code: number | null
  error_message: string | null
  attempt_count: number
  sent_at: string | null
  created_at: string
}
