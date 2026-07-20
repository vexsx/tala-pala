import { useSettings } from '../lib/settings'

/** Sun/moon theme switch (pure unicode, no icon library). */
export default function ThemeToggle() {
  const { theme, setTheme } = useSettings()
  const next = theme === 'dark' ? 'light' : 'dark'
  return (
    <button
      type="button"
      className="btn btn-ghost btn-sm theme-toggle"
      onClick={() => setTheme(next)}
      title={`Switch to ${next} theme`}
      aria-label={`Switch to ${next} theme`}
      data-testid="theme-toggle"
    >
      <span aria-hidden="true">{theme === 'dark' ? '☀' : '☾'}</span>
    </button>
  )
}
