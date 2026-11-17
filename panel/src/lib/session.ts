import type { LoginUser } from './types'

// Panel session: the JWT from POST /auth/login and the user returned with it.
// "Remember me on this browser" stores it in localStorage; otherwise it lives only in this
// tab (sessionStorage) and is gone when the tab is closed.

const KEY = 'connect.session'

export interface Session {
  token: string
  user: SessionUser
  /** JWT exp in seconds (Unix time). Missing = the token does not expire. */
  exp?: number
}

export interface SessionUser {
  userId: string
  email: string
  clientId: string | null
  domainId: string
  domain: string
  role: string
}

interface JwtClaims {
  sub?: string
  username?: string
  client_id?: string | null
  domain_id?: string | number
  domain?: string
  role?: string
  exp?: number
}

let cached: Session | null | undefined

function safeStorage(kind: 'local' | 'session'): Storage | null {
  try {
    return kind === 'local' ? window.localStorage : window.sessionStorage
  } catch {
    return null
  }
}

/** Reads the JWT claims without verifying them (display only; the API does the verification). */
export function decodeJwt(token: string): JwtClaims {
  try {
    const part = token.split('.')[1]
    if (!part) return {}
    const base64 = part.replace(/-/g, '+').replace(/_/g, '/')
    const padded = base64 + '='.repeat((4 - (base64.length % 4)) % 4)
    const json = decodeURIComponent(
      Array.from(atob(padded))
        .map((c) => `%${c.charCodeAt(0).toString(16).padStart(2, '0')}`)
        .join(''),
    )
    return JSON.parse(json) as JwtClaims
  } catch {
    return {}
  }
}

export function sessionFromLogin(token: string, user: LoginUser): Session {
  const claims = decodeJwt(token)
  return {
    token,
    exp: typeof claims.exp === 'number' ? claims.exp : undefined,
    user: {
      userId: user.user_id,
      email: user.email || user.username,
      clientId: user.client_id ?? (claims.client_id as string | null) ?? null,
      domainId: String(user.domain_id ?? claims.domain_id ?? ''),
      domain: user.domain || claims.domain || '',
      role: user.role || claims.role || 'user',
    },
  }
}

function read(): Session | null {
  for (const kind of ['session', 'local'] as const) {
    const store = safeStorage(kind)
    const raw = store?.getItem(KEY)
    if (!raw) continue
    try {
      const parsed = JSON.parse(raw) as Session
      if (parsed && typeof parsed.token === 'string' && parsed.user) return parsed
    } catch {
      store?.removeItem(KEY)
    }
  }
  return null
}

export function getSession(): Session | null {
  if (cached === undefined) cached = read()
  if (cached && cached.exp && cached.exp * 1000 <= Date.now()) {
    clearSession()
    return null
  }
  return cached
}

export function getToken(): string | null {
  return getSession()?.token ?? null
}

export function saveSession(session: Session, remember: boolean) {
  clearSession()
  cached = session
  const store = safeStorage(remember ? 'local' : 'session')
  try {
    store?.setItem(KEY, JSON.stringify(session))
  } catch {
    // No storage (restricted private mode): the session lives in memory only.
  }
}

export function clearSession() {
  cached = null
  safeStorage('local')?.removeItem(KEY)
  safeStorage('session')?.removeItem(KEY)
}
