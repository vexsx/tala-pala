import { useCallback, useEffect, useRef, useState, type ReactNode, type RefObject } from 'react'
import type { CandleCoverage } from '../api/types'
import {
  CUSTOM_INTERVALS,
  PRESET_INTERVALS,
  intervalLabel,
  isSupported,
  type IntervalId
} from './intervals'
import { STATIC_CATALOG, type ChartSymbolGroup } from './catalog'
import { parseChartSymbol, type ChartSymbol } from './symbols'

/**
 * Fullscreen for the chart shell.
 *
 * The Fullscreen API is preferred; when it is unavailable or refused (iOS
 * Safari on iPhone still has no element fullscreen) the caller's CSS class
 * takes over, which is why the class is applied in both paths. Escape leaves
 * either one — the browser handles it natively, and the key listener covers the
 * CSS fallback.
 */
export function useChartFullscreen(targetRef: RefObject<HTMLElement>): {
  fullscreen: boolean
  toggle: () => void
  exit: () => void
} {
  const [fullscreen, setFullscreen] = useState(false)
  const fullscreenRef = useRef(false)
  fullscreenRef.current = fullscreen

  const exit = useCallback(() => {
    if (document.fullscreenElement) void document.exitFullscreen().catch(() => undefined)
    setFullscreen(false)
  }, [])

  const toggle = useCallback(() => {
    const el = targetRef.current
    if (!el) return
    if (fullscreenRef.current) {
      exit()
      return
    }
    setFullscreen(true)
    if (el.requestFullscreen) {
      void el.requestFullscreen().catch(() => undefined)
    }
  }, [exit, targetRef])

  useEffect(() => {
    const onChange = () => {
      // Only follow the browser out of fullscreen; the CSS fallback has no
      // fullscreenElement to begin with and must not be cancelled by this.
      if (!document.fullscreenElement && document.fullscreenEnabled) setFullscreen(false)
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && fullscreenRef.current) exit()
    }
    document.addEventListener('fullscreenchange', onChange)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('fullscreenchange', onChange)
      document.removeEventListener('keydown', onKey)
    }
  }, [exit])

  return { fullscreen, toggle, exit }
}

export interface ChartToolbarProps {
  symbol: ChartSymbol
  onSymbolChange: (symbol: ChartSymbol) => void
  /**
   * The picker's groups (catalog.ts). Defaults to the static catalog — the
   * two gold symbols and the headline Tehran indices — which is also what the
   * page shows while the lists load.
   */
  groups?: ChartSymbolGroup[]
  /** The current symbol's name, for the placeholder shown until it is listed. */
  symbolLabel?: string
  interval: IntervalId
  onIntervalChange: (interval: IntervalId) => void
  coverage: CandleCoverage | null
  fullscreen: boolean
  onToggleFullscreen: () => void
  /** Logarithmic price axis; the toggle is hidden when no handler is given. */
  logScale?: boolean
  onToggleLogScale?: () => void
  /**
   * Slots for the two parallel layers. Pass whatever trigger/menu element the
   * layer owns; the toolbar only reserves the position and never inspects it.
   *   <ChartToolbar indicatorsSlot={<IndicatorsMenu …/>} drawSlot={<DrawMenu …/>} />
   * `children` is a third, free-form slot rendered after the two.
   */
  indicatorsSlot?: ReactNode
  drawSlot?: ReactNode
  children?: ReactNode
}

export function ChartToolbar({
  symbol,
  onSymbolChange,
  groups = STATIC_CATALOG,
  symbolLabel,
  interval,
  onIntervalChange,
  coverage,
  fullscreen,
  onToggleFullscreen,
  logScale = false,
  onToggleLogScale,
  indicatorsSlot,
  drawSlot,
  children
}: ChartToolbarProps) {
  const [customOpen, setCustomOpen] = useState(false)
  const customRef = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    if (!customOpen) return
    const onDown = (e: MouseEvent) => {
      if (!customRef.current?.contains(e.target as Node)) setCustomOpen(false)
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setCustomOpen(false)
    }
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [customOpen])

  const intervalButton = (id: IntervalId) => {
    const support = isSupported(id, coverage)
    const active = id === interval
    return (
      <button
        key={id}
        type="button"
        className={active ? 'active' : ''}
        aria-label={`${intervalLabel(id)} candles`}
        aria-pressed={active}
        disabled={!support.ok}
        title={support.ok ? undefined : support.reason}
        onClick={() => onIntervalChange(id)}
      >
        {intervalLabel(id)}
      </button>
    )
  }

  const customActive = CUSTOM_INTERVALS.includes(interval)
  // A stored symbol the lists have not named yet (or no longer name) still
  // needs an option, or the <select> silently shows the first entry while
  // the chart draws something else.
  const listed = groups.some((g) => g.options.some((o) => o.symbol === symbol))

  return (
    <div className="tchart-toolbar">
      <select
        className="tchart-select"
        aria-label="Symbol"
        value={symbol}
        onChange={(e) => {
          const next = parseChartSymbol(e.target.value)
          if (next !== null) onSymbolChange(next)
        }}
      >
        {!listed && <option value={symbol}>{symbolLabel ?? symbol}</option>}
        {groups.map((g) => (
          <optgroup key={g.id} label={g.label}>
            {g.options.map((o) => (
              <option
                key={o.symbol}
                value={o.symbol}
                // Shown, never hidden: "refused" must not look like "absent".
                disabled={o.disabled !== undefined}
                title={o.title}
              >
                {o.label}
              </option>
            ))}
          </optgroup>
        ))}
      </select>

      <div className="toggle-group tchart-strip" role="group" aria-label="Timeframe">
        {PRESET_INTERVALS.map(intervalButton)}
      </div>

      <div className="tchart-custom" ref={customRef}>
        <button
          type="button"
          className={`btn btn-sm ${customActive ? '' : 'btn-ghost'}`}
          aria-label="Custom timeframe"
          aria-expanded={customOpen}
          aria-haspopup="true"
          onClick={() => setCustomOpen((v) => !v)}
        >
          {customActive ? intervalLabel(interval) : 'Custom'} ▾
        </button>
        {customOpen && (
          <div className="tchart-menu" role="menu" aria-label="Custom timeframes">
            {CUSTOM_INTERVALS.map((id) => {
              const support = isSupported(id, coverage)
              return (
                <button
                  key={id}
                  type="button"
                  // menuitemradio, not menuitem: exactly one timeframe is
                  // selected, and aria-pressed has no meaning inside a menu.
                  role="menuitemradio"
                  className="tchart-menu-item"
                  aria-label={`${intervalLabel(id)} candles`}
                  aria-checked={id === interval}
                  disabled={!support.ok}
                  title={support.ok ? undefined : support.reason}
                  onClick={() => {
                    onIntervalChange(id)
                    setCustomOpen(false)
                  }}
                >
                  {intervalLabel(id)}
                </button>
              )
            })}
          </div>
        )}
      </div>

      {onToggleLogScale && (
        <button
          type="button"
          className={`btn btn-sm ${logScale ? '' : 'btn-ghost'}`}
          aria-label="Logarithmic price scale"
          aria-pressed={logScale}
          title="Equal distances are equal percentage moves — the only way decades of an index stay readable."
          onClick={onToggleLogScale}
        >
          Log
        </button>
      )}

      <span className="tchart-spacer" />

      {indicatorsSlot}
      {drawSlot}
      {children}

      <button
        type="button"
        className="btn btn-sm btn-ghost"
        aria-label={fullscreen ? 'Exit fullscreen' : 'Enter fullscreen'}
        aria-pressed={fullscreen}
        onClick={onToggleFullscreen}
      >
        {fullscreen ? 'Exit fullscreen' : 'Fullscreen'}
      </button>
    </div>
  )
}
