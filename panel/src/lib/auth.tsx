import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react'
import { useNavigate } from 'react-router-dom'
import { ApiError, onUnauthorized } from './api'
import { authApi } from './endpoints'
import { clearSession, getSession, saveSession, sessionFromLogin, type Session, type SessionUser } from './session'

interface AuthValue {
  user: SessionUser | null
  login: (email: string, password: string, remember: boolean) => Promise<void>
  logout: (reason?: 'signedOut' | 'expired') => void
}

const AuthContext = createContext<AuthValue | null>(null)

export function AuthProvider({ children }: { children: ReactNode }) {
  const navigate = useNavigate()
  const [session, setSession] = useState<Session | null>(() => getSession())

  const logout = useCallback(
    (reason: 'signedOut' | 'expired' = 'signedOut') => {
      clearSession()
      setSession(null)
      navigate('/login', { replace: true, state: { notice: reason } })
    },
    [navigate],
  )

  // Any 401 from an authenticated route ends the session (expired or revoked token).
  useEffect(() => {
    onUnauthorized(() => {
      if (getSession()) logout('expired')
    })
    return () => onUnauthorized(null)
  }, [logout])

  // When the panel opens with a saved session, check the token once against /auth/me.
  // A network failure does not end the session: pages show the error and retry.
  useEffect(() => {
    if (!session) return
    const controller = new AbortController()
    authApi.me(controller.signal).catch((err: unknown) => {
      if (err instanceof ApiError && err.status === 401) logout('expired')
    })
    return () => controller.abort()
    // On mount only: a fresh login has already been validated by the API itself.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // Token with exp: sign out on its own at expiry instead of waiting for the next 401.
  useEffect(() => {
    if (!session?.exp) return
    const ms = session.exp * 1000 - Date.now()
    if (ms <= 0) {
      logout('expired')
      return
    }
    const timer = window.setTimeout(() => logout('expired'), Math.min(ms, 2 ** 31 - 1))
    return () => window.clearTimeout(timer)
  }, [session, logout])

  const login = useCallback(async (email: string, password: string, remember: boolean) => {
    const res = await authApi.login(email, password)
    const next = sessionFromLogin(res.access_token, res.user)
    saveSession(next, remember)
    setSession(next)
  }, [])

  const value = useMemo<AuthValue>(() => ({ user: session?.user ?? null, login, logout }), [session, login, logout])

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth(): AuthValue {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth must be used inside <AuthProvider>')
  return ctx
}

/** The signed-in user; use only inside protected routes. */
export function useUser(): SessionUser {
  const { user } = useAuth()
  if (!user) throw new Error('useUser called without a session')
  return user
}
