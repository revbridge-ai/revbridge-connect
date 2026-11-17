import { useCallback, useEffect, useState } from 'react'

// Theme: "system" follows the operating system; "light"/"dark" are pinned via data-theme.
export type ThemeMode = 'system' | 'light' | 'dark'

const KEY = 'connect.theme'

function readMode(): ThemeMode {
  try {
    const saved = localStorage.getItem(KEY)
    if (saved === 'light' || saved === 'dark') return saved
  } catch {
    // no storage available
  }
  return 'system'
}

function apply(mode: ThemeMode) {
  const root = document.documentElement
  if (mode === 'system') root.removeAttribute('data-theme')
  else root.setAttribute('data-theme', mode)
}

export function useTheme(): [ThemeMode, (mode: ThemeMode) => void] {
  const [mode, setModeState] = useState<ThemeMode>(readMode)

  useEffect(() => {
    apply(mode)
  }, [mode])

  const setMode = useCallback((next: ThemeMode) => {
    setModeState(next)
    try {
      if (next === 'system') localStorage.removeItem(KEY)
      else localStorage.setItem(KEY, next)
    } catch {
      // no storage: the preference lasts only for this session
    }
  }, [])

  return [mode, setMode]
}
