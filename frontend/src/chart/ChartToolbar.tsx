import { useCallback, useEffect, useRef, useState, type ReactNode, type RefObject } from 'react'
import type { CandleCoverage } from '../api/types'
import { SYMBOL_LABELS, type Symbol_ } from '../api/types'
import {
  CUSTOM_INTERVALS,
  PRESET_INTERVALS,
  intervalLabel,
  isSupported,
  type IntervalId
} from './intervals'
import { CHART_SYMBOLS, type ChartSymbol } from './prefs'

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
  interval: IntervalId
  onIntervalChange: (interval: IntervalId) => void
  coverage: CandleCoverage | null
  fullscreen: boolean
  onToggleFullscreen: () => void
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
  interval,
  onIntervalChange,
  coverage,
  fullscreen,
  onToggleFullscreen,
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

  return (
    <div className="tchart-toolbar">
      <select
        className="tchart-select"
        aria-label="Symbol"
        value={symbol}
        onChange={(e) => onSymbolChange(e.target.value as ChartSymbol)}
      >
        {CHART_SYMBOLS.map((s) => (
          <option key={s} value={s}>
            {SYMBOL_LABELS[s as Symbol_] ?? s}
          </option>
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
