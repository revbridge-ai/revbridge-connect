import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react'
import { en } from './en'
import { pt, type MessageKey } from './pt'
import { makeFormatters, type Formatters } from '../lib/format'

export type Locale = 'pt-BR' | 'en'
type Vars = Record<string, string | number>

const STORAGE_KEY = 'connect.locale'
const dictionaries = { 'pt-BR': pt, en } as const

interface I18nValue {
  locale: Locale
  setLocale: (locale: Locale) => void
  t: (key: MessageKey, vars?: Vars) => string
  /** Picks "<base>.one" or "<base>.other" according to the count. */
  tn: (base: string, count: number, vars?: Vars) => string
  /** Key built at runtime (e.g. an event label coming from the API); null when it does not exist. */
  tMaybe: (key: string, vars?: Vars) => string | null
  fmt: Formatters
}

const I18nContext = createContext<I18nValue | null>(null)

function initialLocale(): Locale {
  try {
    const saved = localStorage.getItem(STORAGE_KEY)
    if (saved === 'en' || saved === 'pt-BR') return saved
  } catch {
    // no storage available: fall back to the browser language
  }
  // English by default; Portuguese when the browser asks for it
  const lang = typeof navigator !== 'undefined' ? (navigator.language || '').toLowerCase() : ''
  return lang.startsWith('pt') ? 'pt-BR' : 'en'
}

function interpolate(template: string, vars?: Vars): string {
  if (!vars) return template
  return template.replace(/\{(\w+)\}/g, (match, name: string) => (name in vars ? String(vars[name]) : match))
}

export function I18nProvider({ children }: { children: ReactNode }) {
  const [locale, setLocaleState] = useState<Locale>(initialLocale)

  useEffect(() => {
    document.documentElement.lang = locale
  }, [locale])

  const setLocale = useCallback((next: Locale) => {
    setLocaleState(next)
    try {
      localStorage.setItem(STORAGE_KEY, next)
    } catch {
      // no storage: the preference lasts only for this session
    }
  }, [])

  const value = useMemo<I18nValue>(() => {
    const dict = dictionaries[locale] as Record<string, string>
    const lookup = (key: string) => dict[key] ?? (pt as Record<string, string>)[key]
    const t = (key: MessageKey, vars?: Vars) => interpolate(lookup(key) ?? key, vars)
    const tMaybe = (key: string, vars?: Vars) => {
      const found = lookup(key)
      return found === undefined ? null : interpolate(found, vars)
    }
    const plural = new Intl.PluralRules(locale)
    const tn = (base: string, count: number, vars?: Vars) => {
      const rule = plural.select(count) === 'one' ? 'one' : 'other'
      return tMaybe(`${base}.${rule}`, { count, ...vars }) ?? String(count)
    }
    return { locale, setLocale, t, tn, tMaybe, fmt: makeFormatters(locale) }
  }, [locale, setLocale])

  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>
}

export function useI18n(): I18nValue {
  const ctx = useContext(I18nContext)
  if (!ctx) throw new Error('useI18n must be used inside <I18nProvider>')
  return ctx
}
