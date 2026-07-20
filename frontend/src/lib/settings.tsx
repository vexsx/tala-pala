import { createContext, useContext, useEffect, useState, type ReactNode } from 'react'
import type { CalendarMode, DisplayUnit } from './format'

export type ThemeMode = 'dark' | 'light'

export interface Settings {
  unit: DisplayUnit
  calendar: CalendarMode
  theme: ThemeMode
  setUnit: (unit: DisplayUnit) => void
  setCalendar: (calendar: CalendarMode) => void
  setTheme: (theme: ThemeMode) => void
}

const UNIT_KEY = 'igp_unit'
const CALENDAR_KEY = 'igp_calendar'
export const THEME_KEY = 'igp_theme'

// Sensible defaults so components (and tests) can render without a provider.
const SettingsContext = createContext<Settings>({
  unit: 'IRT',
  calendar: 'jalali',
  theme: 'dark',
  setUnit: () => undefined,
  setCalendar: () => undefined,
  setTheme: () => undefined
})

function readStored<T extends string>(key: string, allowed: T[], fallback: T): T {
  try {
    const raw = window.localStorage.getItem(key)
    if (raw && (allowed as string[]).includes(raw)) return raw as T
  } catch {
    // localStorage unavailable — ignore
  }
  return fallback
}

function persist(key: string, value: string): void {
  try {
    window.localStorage.setItem(key, value)
  } catch {
    // ignore
  }
}

export function SettingsProvider({ children }: { children: ReactNode }) {
  const [unit, setUnit] = useState<DisplayUnit>(() => readStored(UNIT_KEY, ['IRT', 'IRR'], 'IRT'))
  const [calendar, setCalendar] = useState<CalendarMode>(() =>
    readStored(CALENDAR_KEY, ['jalali', 'gregorian'], 'jalali')
  )
  const [theme, setTheme] = useState<ThemeMode>(() =>
    readStored(THEME_KEY, ['dark', 'light'], 'dark')
  )

  useEffect(() => {
    persist(UNIT_KEY, unit)
  }, [unit])

  useEffect(() => {
    persist(CALENDAR_KEY, calendar)
  }, [calendar])

  useEffect(() => {
    persist(THEME_KEY, theme)
    // index.html applies the stored theme before first paint; this keeps the
    // attribute in sync when the user toggles at runtime.
    document.documentElement.dataset.theme = theme
  }, [theme])

  return (
    <SettingsContext.Provider value={{ unit, calendar, theme, setUnit, setCalendar, setTheme }}>
      {children}
    </SettingsContext.Provider>
  )
}

export function useSettings(): Settings {
  return useContext(SettingsContext)
}
