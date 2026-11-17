import { getToken } from './session'

// Same origin; Caddy (deployed installation) and the Vite proxy (development) strip the /api prefix.
export const API_BASE = (import.meta.env.VITE_API_BASE ?? '/api').replace(/\/$/, '')

type Query = Record<string, string | number | boolean | null | undefined>

export interface RequestOptions {
  method?: 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE'
  query?: Query
  body?: unknown
  signal?: AbortSignal
  /** false = do not send the token and do not treat a 401 as an expired session (login). */
  auth?: boolean
}

/** API call error. status 0 = the API did not respond (network, proxy, server down). */
export class ApiError extends Error {
  status: number
  detail: unknown
  code?: string

  constructor(status: number, message: string, detail?: unknown, code?: string) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
    this.code = code
  }
}

let unauthorizedHandler: (() => void) | null = null

/** The AuthProvider registers what to do when the API answers 401 (expired session). */
export function onUnauthorized(handler: (() => void) | null) {
  unauthorizedHandler = handler
}

export function buildUrl(path: string, query?: Query): string {
  const url = `${API_BASE}${path.startsWith('/') ? path : `/${path}`}`
  if (!query) return url
  const params = new URLSearchParams()
  for (const [key, value] of Object.entries(query)) {
    if (value === undefined || value === null || value === '') continue
    params.set(key, String(value))
  }
  const qs = params.toString()
  return qs ? `${url}?${qs}` : url
}

// Turns FastAPI's "detail" (text, object or validation error list) into one sentence.
function messageFromDetail(detail: unknown, fallback: string): { message: string; code?: string } {
  if (typeof detail === 'string' && detail.trim()) return { message: detail }
  if (Array.isArray(detail)) {
    const parts = detail
      .map((item) => {
        if (item && typeof item === 'object' && 'msg' in item) {
          const loc = Array.isArray((item as { loc?: unknown[] }).loc)
            ? ((item as { loc: unknown[] }).loc.filter((p) => p !== 'body').join('.') as string)
            : ''
          const msg = String((item as { msg: unknown }).msg)
          return loc ? `${loc}: ${msg}` : msg
        }
        return null
      })
      .filter(Boolean)
    if (parts.length) return { message: parts.join('; ') }
  }
  if (detail && typeof detail === 'object') {
    const obj = detail as Record<string, unknown>
    const code = typeof obj.error === 'string' ? obj.error : undefined
    if (typeof obj.message === 'string') return { message: obj.message, code }
    if (code) return { message: code, code }
  }
  return { message: fallback }
}

export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = 'GET', query, body, signal, auth = true } = options
  const headers: Record<string, string> = { Accept: 'application/json' }
  if (body !== undefined) headers['Content-Type'] = 'application/json'
  if (auth) {
    const token = getToken()
    if (token) headers.Authorization = `Bearer ${token}`
  }

  let response: Response
  try {
    response = await fetch(buildUrl(path, query), {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      signal,
      credentials: 'same-origin',
    })
  } catch (err) {
    if (err instanceof DOMException && err.name === 'AbortError') throw err
    throw new ApiError(0, 'network', err)
  }

  if (response.status === 204) return undefined as T

  const text = await response.text()
  let payload: unknown = null
  if (text) {
    try {
      payload = JSON.parse(text)
    } catch {
      payload = text
    }
  }

  if (!response.ok) {
    const detail = payload && typeof payload === 'object' && 'detail' in payload ? (payload as { detail: unknown }).detail : payload
    const { message, code } = messageFromDetail(detail, response.statusText || `HTTP ${response.status}`)
    if (response.status === 401 && auth && unauthorizedHandler) unauthorizedHandler()
    throw new ApiError(response.status, message, detail, code)
  }

  return payload as T
}

export function isApiError(err: unknown): err is ApiError {
  return err instanceof ApiError
}

export function isAbort(err: unknown): boolean {
  return err instanceof DOMException && err.name === 'AbortError'
}
