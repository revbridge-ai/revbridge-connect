import { useEffect, useRef, useState, type FormEvent } from 'react'
import { Navigate, useLocation } from 'react-router-dom'
import { Eye, EyeOff, Info } from 'lucide-react'
import { useAuth } from '../lib/auth'
import { useI18n } from '../i18n'
import { ApiError } from '../lib/api'
import { EMAIL_RE } from '../lib/labels'
import { Button } from '../components/Button'
import { describedBy } from '../components/Form'
import { BrandMark } from '../layout/AppShell'

interface LocationState {
  from?: string
  notice?: 'expired' | 'signedOut'
}

export function LoginPage() {
  const { t } = useI18n()
  const { user, login } = useAuth()
  const location = useLocation()
  const state = (location.state ?? {}) as LocationState
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [remember, setRemember] = useState(false)
  const [showPassword, setShowPassword] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [fieldErrors, setFieldErrors] = useState<{ email?: string; password?: string }>({})
  const [formError, setFormError] = useState<string | null>(null)
  const emailRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    document.title = `${t('login.title')} · ${t('app.name')}`
  }, [t])

  if (user) {
    const from = state.from && state.from !== '/login' ? state.from : '/'
    return <Navigate to={from} replace />
  }

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    const errors: typeof fieldErrors = {}
    if (!EMAIL_RE.test(email.trim())) errors.email = t('login.emailInvalid')
    if (password.length < 6) errors.password = t('login.passwordShort')
    setFieldErrors(errors)
    setFormError(null)
    if (errors.email || errors.password) {
      if (errors.email) emailRef.current?.focus()
      return
    }

    setSubmitting(true)
    try {
      await login(email.trim(), password, remember)
      // Navigation happens on the next render, once the session exists.
    } catch (err) {
      setFormError(loginError(err))
      setSubmitting(false)
    }
  }

  // POST /auth/login errors, translated into what the user can do about them.
  const loginError = (err: unknown): string => {
    if (!(err instanceof ApiError)) return t('errors.generic')
    if (err.status === 0) return t('errors.network')
    if (err.status === 401) return t('login.invalid')
    if (err.status === 429) {
      const minutes =
        err.detail && typeof err.detail === 'object' && 'retry_after_minutes' in err.detail
          ? Number((err.detail as { retry_after_minutes: unknown }).retry_after_minutes) || 15
          : 15
      return t('login.tooMany', { minutes })
    }
    if (err.status === 403) return t('login.noDomain')
    if (err.status === 404) return t('errors.notMounted')
    if (err.status >= 500) {
      // Generic 500 texts ("Internal Server Error", or the API's own "Erro interno do servidor")
      // get the friendly message; a more specific one is shown as is.
      return err.message && !/interno|internal/i.test(err.message) ? err.message : t('errors.server')
    }
    return err.message || t('errors.generic')
  }

  const inputClass = (invalid?: boolean) =>
    `h-12 w-full rounded-[10px] border bg-surface px-3.5 text-base text-ink sm:text-sm ${
      invalid ? 'border-danger-text' : 'border-field'
    }`

  return (
    <main className="flex min-h-dvh items-start justify-center bg-canvas px-4 py-12 sm:items-center sm:py-16">
      <div className="w-full max-w-[400px]">
        <BrandMark />
        <h1 className="mt-10 text-[1.9375rem] font-semibold leading-tight tracking-[-0.02em] text-ink sm:text-4xl">
          {t('login.title')}
        </h1>
        <p className="mt-2 text-[0.9375rem] leading-relaxed text-muted">{t('login.subtitle')}</p>

        {state.notice === 'expired' && (
          <p role="status" className="mt-6 flex gap-2.5 rounded-[10px] bg-info/10 px-3.5 py-3 text-sm text-ink">
            <Info className="mt-0.5 h-4 w-4 shrink-0 text-info-text" strokeWidth={1.75} aria-hidden="true" />
            {t('login.expired')}
          </p>
        )}
        {state.notice === 'signedOut' && (
          <p role="status" className="mt-6 text-sm text-muted">
            {t('login.signedOut')}
          </p>
        )}

        <form onSubmit={submit} noValidate className="mt-8 flex flex-col gap-5">
          <div className="flex flex-col gap-1.5">
            <label htmlFor="login-email" className="text-[0.8125rem] font-medium text-ink">
              {t('login.email')}
            </label>
            <input
              ref={emailRef}
              id="login-email"
              type="email"
              autoComplete="username"
              inputMode="email"
              autoFocus
              value={email}
              onChange={(event) => setEmail(event.target.value)}
              aria-invalid={!!fieldErrors.email || undefined}
              aria-describedby={describedBy('login-email', undefined, fieldErrors.email)}
              className={inputClass(!!fieldErrors.email)}
            />
            {fieldErrors.email && (
              <p id="login-email-error" className="text-xs font-medium text-danger-text">
                {fieldErrors.email}
              </p>
            )}
          </div>

          <div className="flex flex-col gap-1.5">
            <label htmlFor="login-password" className="text-[0.8125rem] font-medium text-ink">
              {t('login.password')}
            </label>
            <div className="relative">
              <input
                id="login-password"
                type={showPassword ? 'text' : 'password'}
                autoComplete="current-password"
                value={password}
                onChange={(event) => setPassword(event.target.value)}
                aria-invalid={!!fieldErrors.password || undefined}
                aria-describedby={describedBy('login-password', undefined, fieldErrors.password)}
                className={`${inputClass(!!fieldErrors.password)} pr-12`}
                maxLength={100}
              />
              <button
                type="button"
                onClick={() => setShowPassword((v) => !v)}
                aria-label={showPassword ? t('login.hidePassword') : t('login.showPassword')}
                aria-pressed={showPassword}
                className="absolute right-1 top-1 inline-flex h-10 w-10 items-center justify-center rounded-lg text-muted hover:bg-subtle hover:text-ink"
              >
                {showPassword ? (
                  <EyeOff className="h-4 w-4" strokeWidth={1.75} aria-hidden="true" />
                ) : (
                  <Eye className="h-4 w-4" strokeWidth={1.75} aria-hidden="true" />
                )}
              </button>
            </div>
            {fieldErrors.password && (
              <p id="login-password-error" className="text-xs font-medium text-danger-text">
                {fieldErrors.password}
              </p>
            )}
          </div>

          <label htmlFor="login-remember" className="flex cursor-pointer items-start gap-2.5">
            <input
              id="login-remember"
              type="checkbox"
              checked={remember}
              onChange={(event) => setRemember(event.target.checked)}
              className="mt-0.5 h-4 w-4 cursor-pointer accent-primary"
            />
            <span className="flex flex-col">
              <span className="text-sm text-ink">{t('login.remember')}</span>
              <span className="text-xs text-muted">{t('login.rememberHint')}</span>
            </span>
          </label>

          {formError && (
            <p role="alert" className="rounded-[10px] border border-danger/30 bg-danger/5 px-3.5 py-3 text-sm text-danger-text">
              {formError}
            </p>
          )}

          <Button type="submit" size="auth" loading={submitting} className="w-full">
            {submitting ? t('login.submitting') : t('login.submit')}
          </Button>
        </form>

        <p className="mt-8 text-xs leading-relaxed text-muted">{t('login.firstAccess')}</p>
      </div>
    </main>
  )
}
