import { beforeEach, describe, expect, it } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { SettingsProvider, THEME_KEY } from '../lib/settings'
import ThemeToggle from '../components/ThemeToggle'

function renderToggle() {
  return render(
    <SettingsProvider>
      <ThemeToggle />
    </SettingsProvider>
  )
}

describe('ThemeToggle', () => {
  beforeEach(() => {
    window.localStorage.clear()
    document.documentElement.removeAttribute('data-theme')
  })

  it('defaults to dark and applies data-theme on mount', () => {
    renderToggle()
    expect(document.documentElement.dataset.theme).toBe('dark')
    expect(window.localStorage.getItem(THEME_KEY)).toBe('dark')
    expect(screen.getByTestId('theme-toggle')).toHaveAccessibleName('Switch to light theme')
  })

  it('toggling switches data-theme and persists to localStorage', () => {
    renderToggle()
    fireEvent.click(screen.getByTestId('theme-toggle'))
    expect(document.documentElement.dataset.theme).toBe('light')
    expect(window.localStorage.getItem(THEME_KEY)).toBe('light')
    expect(screen.getByTestId('theme-toggle')).toHaveAccessibleName('Switch to dark theme')

    fireEvent.click(screen.getByTestId('theme-toggle'))
    expect(document.documentElement.dataset.theme).toBe('dark')
    expect(window.localStorage.getItem(THEME_KEY)).toBe('dark')
  })

  it('boots with the persisted light theme', () => {
    window.localStorage.setItem(THEME_KEY, 'light')
    renderToggle()
    expect(document.documentElement.dataset.theme).toBe('light')
    expect(screen.getByTestId('theme-toggle')).toHaveAccessibleName('Switch to dark theme')
  })
})
