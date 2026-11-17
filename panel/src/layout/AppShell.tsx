import { useEffect, useRef, useState } from 'react'
import { NavLink, Navigate, Outlet, useLocation } from 'react-router-dom'
import { BookOpen, Globe, KeyRound, LayoutDashboard, LogOut, Mail, MailX, Menu, Monitor, Moon, Sun, Webhook } from 'lucide-react'
import { useAuth } from '../lib/auth'
import { useI18n, type Locale } from '../i18n'
import { useTheme, type ThemeMode } from '../lib/theme'
import { Drawer } from '../components/Drawer'
import { IconButton } from '../components/Button'
import type { MessageKey } from '../i18n/pt'

const NAV: { to: string; label: MessageKey; icon: typeof Mail }[] = [
  { to: '/', label: 'nav.overview', icon: LayoutDashboard },
  { to: '/emails', label: 'nav.emails', icon: Mail },
  { to: '/keys', label: 'nav.keys', icon: KeyRound },
  { to: '/domains', label: 'nav.domains', icon: Globe },
  { to: '/suppressions', label: 'nav.suppressions', icon: MailX },
  { to: '/webhooks', label: 'nav.webhooks', icon: Webhook },
  { to: '/docs', label: 'nav.docs', icon: BookOpen },
]

export function BrandMark({ className = '' }: { className?: string }) {
  const { t } = useI18n()
  return (
    <span className={`inline-flex items-center gap-2.5 ${className}`}>
      <img src={`${import.meta.env.BASE_URL}favicon.svg`} alt="" className="h-5 w-auto" width={28} height={20} />
      <span className="text-[0.9375rem] font-semibold tracking-tight text-ink">{t('app.name')}</span>
    </span>
  )
}

function ThemeSwitch() {
  const { t } = useI18n()
  const [mode, setMode] = useTheme()
  const options: { value: ThemeMode; label: string; Icon: typeof Sun }[] = [
    { value: 'system', label: t('theme.system'), Icon: Monitor },
    { value: 'light', label: t('theme.light'), Icon: Sun },
    { value: 'dark', label: t('theme.dark'), Icon: Moon },
  ]
  return (
    <fieldset>
      <legend className="sr-only">{t('shell.theme')}</legend>
      <div className="inline-flex gap-0.5 rounded-lg bg-subtle p-0.5">
        {options.map(({ value, label, Icon }) => (
          <label key={value} title={label}>
            <input
              type="radio"
              name="theme"
              value={value}
              checked={mode === value}
              onChange={() => setMode(value)}
              className="peer sr-only"
            />
            <span className="inline-flex h-8 w-9 cursor-pointer items-center justify-center rounded-md text-muted transition-colors duration-150 hover:text-ink peer-checked:bg-surface peer-checked:text-ink peer-checked:shadow-sm peer-focus-visible:outline peer-focus-visible:outline-2 peer-focus-visible:outline-accent">
              <Icon className="h-4 w-4" strokeWidth={1.5} aria-hidden="true" />
              <span className="sr-only">{label}</span>
            </span>
          </label>
        ))}
      </div>
    </fieldset>
  )
}

function LanguageSwitch() {
  const { t, locale, setLocale } = useI18n()
  return (
    <label className="relative inline-flex">
      <span className="sr-only">{t('shell.language')}</span>
      <select
        value={locale}
        onChange={(event) => setLocale(event.target.value as Locale)}
        className="h-9 cursor-pointer appearance-none rounded-lg bg-subtle px-2.5 text-[0.8125rem] font-medium text-ink hover:bg-line"
      >
        <option value="pt-BR">{t('lang.pt')}</option>
        <option value="en">{t('lang.en')}</option>
      </select>
    </label>
  )
}

function SidebarContent({ onNavigate, showBrand = true }: { onNavigate?: () => void; showBrand?: boolean }) {
  const { t } = useI18n()
  const { user, logout } = useAuth()
  return (
    <div className="flex h-full flex-col">
      {showBrand && (
        <div className="flex h-16 items-center px-5">
          <BrandMark />
        </div>
      )}
      <nav aria-label={t('nav.main')} className="flex-1 overflow-y-auto px-3 py-2">
        <ul className="flex flex-col gap-0.5">
          {NAV.map(({ to, label, icon: Icon }) => (
            <li key={to}>
              <NavLink
                to={to}
                end={to === '/'}
                onClick={onNavigate}
                className={({ isActive }) =>
                  `group flex h-10 items-center gap-3 rounded-lg px-3 text-[0.875rem] transition-colors duration-150 lg:h-9 lg:text-[0.8125rem] ${
                    isActive ? 'bg-subtle font-medium text-ink' : 'text-muted hover:bg-subtle/70 hover:text-ink'
                  }`
                }
              >
                {({ isActive }) => (
                  <>
                    <Icon
                      className={`h-4 w-4 shrink-0 ${isActive ? 'text-primary-text' : ''}`}
                      strokeWidth={1.5}
                      aria-hidden="true"
                    />
                    {t(label)}
                  </>
                )}
              </NavLink>
            </li>
          ))}
        </ul>
      </nav>
      <div className="flex flex-col gap-3 border-t border-line px-4 py-4">
        {user && (
          <div className="min-w-0 px-1">
            <p className="sr-only">{t('shell.account')}</p>
            <p className="truncate text-[0.8125rem] font-medium text-ink" title={user.email}>
              {user.email}
            </p>
            <p className="truncate text-xs text-muted" title={user.domain}>
              {user.domain}
            </p>
          </div>
        )}
        <div className="flex items-center justify-between gap-2">
          <ThemeSwitch />
          <LanguageSwitch />
        </div>
        <button
          type="button"
          onClick={() => logout('signedOut')}
          className="flex h-9 items-center gap-3 rounded-lg px-3 text-[0.8125rem] text-muted transition-colors duration-150 hover:bg-subtle hover:text-ink"
        >
          <LogOut className="h-4 w-4" strokeWidth={1.5} aria-hidden="true" />
          {t('shell.signOut')}
        </button>
      </div>
    </div>
  )
}

export function AppShell() {
  const { t } = useI18n()
  const location = useLocation()
  const [menuOpen, setMenuOpen] = useState(false)
  const firstRender = useRef(true)

  // On page change, move focus to the title (so screen readers announce where the user is).
  useEffect(() => {
    if (firstRender.current) {
      firstRender.current = false
      return
    }
    setMenuOpen(false)
    const title = document.getElementById('page-title')
    title?.focus({ preventScroll: false })
    window.scrollTo({ top: 0 })
  }, [location.pathname])

  return (
    <div className="min-h-dvh bg-canvas lg:flex">
      <a
        href="#main"
        className="sr-only z-50 rounded-lg bg-surface px-4 py-2 text-sm font-medium text-ink shadow-pop focus:not-sr-only focus:fixed focus:left-4 focus:top-4"
      >
        {t('shell.skip')}
      </a>

      <aside className="sticky top-0 hidden h-dvh w-[260px] shrink-0 border-r border-line lg:block">
        <SidebarContent />
      </aside>

      <div className="min-w-0 flex-1">
        <header className="sticky top-0 z-30 flex h-14 items-center gap-1 border-b border-line bg-canvas px-2 lg:hidden">
          <IconButton label={t('nav.openMenu')} onClick={() => setMenuOpen(true)} aria-expanded={menuOpen}>
            <Menu className="h-5 w-5" strokeWidth={1.5} aria-hidden="true" />
          </IconButton>
          <BrandMark />
        </header>

        <main id="main" tabIndex={-1} className="mx-auto w-full max-w-workspace px-4 pb-16 pt-8 outline-none sm:px-6 lg:px-10 lg:pt-10">
          <Outlet />
        </main>
      </div>

      <Drawer
        open={menuOpen}
        onClose={() => setMenuOpen(false)}
        title={<BrandMark />}
        side="left"
        widthClass="w-[18rem] max-w-[85vw]"
      >
        <div className="-mx-5 -my-5 h-[calc(100%+2.5rem)]">
          <SidebarContent onNavigate={() => setMenuOpen(false)} showBrand={false} />
        </div>
      </Drawer>
    </div>
  )
}

export function RequireAuth() {
  const { user } = useAuth()
  const location = useLocation()
  if (!user) return <Navigate to="/login" replace state={{ from: location.pathname }} />
  return <AppShell />
}
