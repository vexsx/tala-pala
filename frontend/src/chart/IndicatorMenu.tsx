import { useEffect, useRef, useState } from 'react'
import {
  INDICATOR_DEFS,
  MA_METHODS,
  MA_SOURCES,
  PRESETS,
  addInstance,
  applyPreset,
  indicatorDef,
  instanceLabel,
  makeInstance,
  matchingPreset,
  nextMovingAverage,
  removeInstance,
  setOverlay,
  type ChartIndicatorState,
  type IndicatorInstance,
  type IndicatorKind,
  type OverlayToggles
} from './indicators/registry'

/**
 * The Indicators menu — presets, the catalogue, and the three chart overlays.
 *
 * It is a dropdown rather than a permanent strip because the strip it replaces
 * pushed the candles down a row on a phone. The trigger reports the count so
 * the board is legible without opening it.
 */

export interface IndicatorMenuProps {
  state: ChartIndicatorState
  onChange: (next: ChartIndicatorState) => void
}

const OVERLAY_LABELS: Array<{ key: keyof OverlayToggles; label: string; hint: string }> = [
  {
    key: 'forecast',
    label: 'Forecast',
    hint: 'The model’s point estimate and its interval, drawn past the last candle. An estimate, not a path.'
  },
  {
    key: 'events',
    label: 'News events',
    hint: 'Markers for headlines the collectors stored, placed only where a publication time exists.'
  },
  {
    key: 'trend',
    label: 'Trend alignment',
    hint: 'The server’s 1D / 4H / 1H read. The chart never derives it from its own lines.'
  }
]

export function IndicatorMenu({ state, onChange }: IndicatorMenuProps) {
  const [open, setOpen] = useState(false)
  const [refusal, setRefusal] = useState<string | null>(null)
  const wrapRef = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    if (!open) return
    const onDown = (e: MouseEvent) => {
      if (!wrapRef.current?.contains(e.target as Node)) setOpen(false)
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false)
    }
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  const active = matchingPreset(state)
  const count = state.instances.length

  const has = (kind: IndicatorKind) => state.instances.some((i) => i.kind === kind)

  const toggleKind = (kind: IndicatorKind) => {
    setRefusal(null)
    if (kind === 'ma') {
      const instance = nextMovingAverage(state.instances)
      if (instance === null) {
        setRefusal('Every default moving average is already on the chart. Edit one instead.')
        return
      }
      const next = addInstance(state, instance)
      if (!next.ok) {
        setRefusal(next.message)
        return
      }
      onChange(next.value)
      return
    }
    const existing = state.instances.find((i) => i.kind === kind)
    if (existing) {
      onChange(removeInstance(state, existing.id))
      return
    }
    const made = makeInstance(kind)
    if (!made.ok) {
      setRefusal(made.message)
      return
    }
    const next = addInstance(state, made.value)
    if (!next.ok) {
      setRefusal(next.message)
      return
    }
    onChange(next.value)
  }

  return (
    <div className="tchart-custom" ref={wrapRef}>
      <button
        type="button"
        className={`btn btn-sm ${count > 0 ? '' : 'btn-ghost'}`}
        aria-label="Indicators"
        aria-expanded={open}
        aria-haspopup="true"
        onClick={() => setOpen((v) => !v)}
      >
        Indicators{count > 0 ? ` (${count})` : ''} ▾
      </button>

      {open && (
        <div className="tchart-menu tchart-imenu" role="menu" aria-label="Indicator options">
          <div className="tchart-imenu-head" id="indicator-presets">
            Presets
          </div>
          <div className="tchart-imenu-presets" role="group" aria-labelledby="indicator-presets">
            {PRESETS.map((p) => (
              <button
                key={p.id}
                type="button"
                role="menuitemradio"
                className="tchart-menu-item"
                aria-checked={active === p.id}
                aria-label={`${p.label} preset`}
                title={p.hint}
                onClick={() => {
                  setRefusal(null)
                  onChange(applyPreset(p.id))
                }}
              >
                {p.label}
              </button>
            ))}
          </div>

          <div className="tchart-imenu-head">Indicators</div>
          {INDICATOR_DEFS.map((def) => {
            const on = has(def.kind)
            return (
              <button
                key={def.kind}
                type="button"
                role={def.kind === 'ma' ? 'menuitem' : 'menuitemcheckbox'}
                className="tchart-menu-item tchart-imenu-item"
                aria-checked={def.kind === 'ma' ? undefined : on}
                aria-label={def.kind === 'ma' ? 'Add moving average' : def.label}
                title={def.hint}
                onClick={() => toggleKind(def.kind)}
              >
                <span className="tchart-imenu-mark" aria-hidden="true">
                  {def.kind === 'ma' ? '+' : on ? '✓' : ''}
                </span>
                <span>{def.kind === 'ma' ? 'Add moving average' : def.label}</span>
                {def.server && (
                  <span className="badge badge-off tchart-imenu-badge" title="Computed by the server">
                    server
                  </span>
                )}
              </button>
            )
          })}

          <div className="tchart-imenu-head">Overlays</div>
          {OVERLAY_LABELS.map((overlay) => (
            <button
              key={overlay.key}
              type="button"
              role="menuitemcheckbox"
              className="tchart-menu-item tchart-imenu-item"
              aria-checked={state.overlays[overlay.key]}
              aria-label={overlay.label}
              title={overlay.hint}
              onClick={() => {
                setRefusal(null)
                onChange(setOverlay(state, overlay.key, !state.overlays[overlay.key]))
              }}
            >
              <span className="tchart-imenu-mark" aria-hidden="true">
                {state.overlays[overlay.key] ? '✓' : ''}
              </span>
              <span>{overlay.label}</span>
            </button>
          ))}

          {refusal !== null && (
            <p className="tchart-imenu-refusal small" role="alert">
              {refusal}
            </p>
          )}
        </div>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Settings
// ---------------------------------------------------------------------------

export interface IndicatorSettingsFormProps {
  instance: IndicatorInstance
  /** Called with a validated replacement. Never called with a bad setting. */
  onApply: (next: IndicatorInstance) => string | null
  onClose: () => void
}

/**
 * The per-indicator settings form.
 *
 * Validation lives in registry.ts, not here: a refusal has to read the same
 * whether it came from this form, from a preset or from a stored preference.
 * The form never applies an invalid value — it says why and leaves the chart
 * exactly as it was, because a chart that blanks itself on a typo is worse
 * than one that argues.
 */
export function IndicatorSettingsForm({
  instance,
  onApply,
  onClose
}: IndicatorSettingsFormProps) {
  const [method, setMethod] = useState(instance.method ?? 'ema')
  const [source, setSource] = useState(instance.source ?? 'close')
  const [period, setPeriod] = useState(String(instance.period ?? ''))
  const [fast, setFast] = useState(String(instance.fast ?? ''))
  const [slow, setSlow] = useState(String(instance.slow ?? ''))
  const [signal, setSignal] = useState(String(instance.signal ?? ''))
  const [message, setMessage] = useState<string | null>(null)

  const def = indicatorDef(instance.kind)
  if (def !== null && !def.configurable) {
    return (
      <div className="tchart-settings">
        <p className="muted small">
          {instanceLabel(instance)} is computed by the server for the buckets on screen, so it has
          no local settings. Changing it here would put a second, disagreeing calculation on the
          chart.
        </p>
        <button type="button" className="btn btn-ghost btn-sm" onClick={onClose}>
          Close
        </button>
      </div>
    )
  }

  const submit = (e: React.FormEvent) => {
    e.preventDefault()
    const made =
      instance.kind === 'macd'
        ? makeInstance('macd', { fast, slow, signal }, instance.visible)
        : instance.kind === 'rsi'
          ? makeInstance('rsi', { period }, instance.visible)
          : makeInstance('ma', { method, period, source }, instance.visible)
    if (!made.ok) {
      setMessage(made.message)
      return
    }
    const refusal = onApply(made.value)
    if (refusal !== null) {
      setMessage(refusal)
      return
    }
    onClose()
  }

  return (
    <form className="tchart-settings" onSubmit={submit} aria-label={`Edit ${instanceLabel(instance)}`}>
      {instance.kind === 'ma' && (
        <>
          <label className="tchart-field">
            <span className="muted small">Method</span>
            <select
              className="tchart-select"
              aria-label="Method"
              value={method}
              onChange={(e) => setMethod(e.target.value as typeof method)}
            >
              {MA_METHODS.map((m) => (
                <option key={m} value={m}>
                  {m.toUpperCase()}
                </option>
              ))}
            </select>
          </label>
          <label className="tchart-field">
            <span className="muted small">Source</span>
            <select
              className="tchart-select"
              aria-label="Source"
              value={source}
              onChange={(e) => setSource(e.target.value as typeof source)}
            >
              {MA_SOURCES.map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </select>
          </label>
        </>
      )}

      {instance.kind !== 'macd' && (
        <label className="tchart-field">
          <span className="muted small">Period</span>
          <input
            className="tchart-select"
            aria-label="Period"
            inputMode="numeric"
            value={period}
            onChange={(e) => setPeriod(e.target.value)}
          />
        </label>
      )}

      {instance.kind === 'macd' && (
        <>
          <label className="tchart-field">
            <span className="muted small">Fast</span>
            <input
              className="tchart-select"
              aria-label="Fast period"
              inputMode="numeric"
              value={fast}
              onChange={(e) => setFast(e.target.value)}
            />
          </label>
          <label className="tchart-field">
            <span className="muted small">Slow</span>
            <input
              className="tchart-select"
              aria-label="Slow period"
              inputMode="numeric"
              value={slow}
              onChange={(e) => setSlow(e.target.value)}
            />
          </label>
          <label className="tchart-field">
            <span className="muted small">Signal</span>
            <input
              className="tchart-select"
              aria-label="Signal period"
              inputMode="numeric"
              value={signal}
              onChange={(e) => setSignal(e.target.value)}
            />
          </label>
        </>
      )}

      <div className="tchart-settings-actions">
        <button type="submit" className="btn btn-sm">
          Apply
        </button>
        <button type="button" className="btn btn-ghost btn-sm" onClick={onClose}>
          Cancel
        </button>
      </div>

      {message !== null && (
        <p className="tchart-imenu-refusal small" role="alert">
          {message}
        </p>
      )}
    </form>
  )
}
