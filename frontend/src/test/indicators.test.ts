import { describe, expect, it } from 'vitest'
import type { CandleOverlays, ChartCandle, NewsItem, Prediction } from '../api/types'
import {
  DEFAULT_PRESET,
  MAX_PERIOD,
  addInstance,
  applyPreset,
  buildPlots,
  coldInstances,
  deserializeState,
  emaSeries,
  hasInstance,
  instanceLabel,
  macdSeries,
  makeInstance,
  matchingPreset,
  nextMovingAverage,
  parseSpec,
  removeInstance,
  replaceInstance,
  rsiSeries,
  serializeState,
  serverOverlayFields,
  serverOverlays,
  setOverlay,
  setVisibility,
  smaSeries,
  validateMacdSettings,
  validateMaSettings,
  validatePeriod,
  valueAt,
  type ChartIndicatorState,
  type IndicatorInstance
} from '../chart/indicators/registry'
import { forecastPlots, forecastPoints, isTrendSymbol, placeEvents } from '../chart/overlays'

const DAY = 86_400
const BASE_T = Date.parse('2026-08-01T00:00:00Z') / 1000

function candle(index: number, close: number): ChartCandle {
  return {
    t: BASE_T + index * DAY,
    open: close,
    high: close,
    low: close,
    close,
    volume: null,
    ticks: 8,
    confirmed: true,
    synthetic: false
  }
}

function series(closes: number[]): ChartCandle[] {
  return closes.map((c, i) => candle(i, c))
}

// ---------------------------------------------------------------------------
// Moving averages
// ---------------------------------------------------------------------------

describe('SMA', () => {
  it('is null until the window is full, then the plain mean', () => {
    expect(smaSeries([2, 4, 6, 8], 3)).toEqual([null, null, 4, 6])
  })

  it('refuses to invent a value for a period longer than the data', () => {
    expect(smaSeries([1, 2], 5)).toEqual([null, null])
  })
})

describe('EMA — SMA-seeded, matching the server', () => {
  /**
   * Hand arithmetic, seeded with the SMA of the first `period` closes exactly
   * as backend-go/internal/indicators/indicators.go::EMA and
   * prediction-python/app/models/trend_alignment.py::ema do it.
   *
   *   period 4, k = 2/(4+1) = 0.4
   *   seed  = (2 + 4 + 6 + 8) / 4        = 5      -> index 3
   *   i = 4 : (10 - 5) * 0.4 + 5         = 7
   *   i = 5 : (12 - 7) * 0.4 + 7         = 9
   */
  it('matches hand-computed SMA-seeded values', () => {
    expect(emaSeries([2, 4, 6, 8, 10, 12], 4)).toEqual([null, null, null, 5, 7, 9])
  })

  it('matches a second hand-computed case', () => {
    // period 3, k = 0.5, seed = (10+11+12)/3 = 11
    //   i = 3 : (13 - 11) * 0.5 + 11 = 12
    //   i = 4 : (14 - 12) * 0.5 + 12 = 13
    expect(emaSeries([10, 11, 12, 13, 14], 3)).toEqual([null, null, 11, 12, 13])
  })

  it('is NOT the pandas first-value seeding, which would disagree with the trend card', () => {
    // Seeding from values[0] instead of the SMA gives 9.23328 at the last
    // index. If this ever passes, the chart's EMA220 and the trend-alignment
    // card's EMA220 are two different numbers for the same market.
    const out = emaSeries([2, 4, 6, 8, 10, 12], 4)
    expect(out[5]).toBe(9)
    expect(out[5]).not.toBeCloseTo(9.23328, 5)
  })

  it('leaves the whole series null when there is less history than the period', () => {
    expect(emaSeries([1, 2, 3], 220)).toEqual([null, null, null])
  })
})

// ---------------------------------------------------------------------------
// Oscillators
// ---------------------------------------------------------------------------

describe('RSI (Wilder)', () => {
  it('matches hand-computed Wilder smoothing', () => {
    // period 2 over [10, 12, 11, 13, 12]
    //   seed  : avgGain 1, avgLoss 0.5 -> rs 2   -> 66.667  (index 2)
    //   i = 3 : avgGain 1.5, avgLoss 0.25 -> rs 6 -> 85.714
    //   i = 4 : avgGain 0.75, avgLoss 0.625 -> rs 1.2 -> 54.545
    const out = rsiSeries([10, 12, 11, 13, 12], 2)
    expect(out[0]).toBeNull()
    expect(out[1]).toBeNull()
    expect(out[2]).toBeCloseTo(66.6667, 3)
    expect(out[3]).toBeCloseTo(85.7143, 3)
    expect(out[4]).toBeCloseTo(54.5455, 3)
  })

  it('reports 100 for an unbroken rally rather than dividing by zero', () => {
    expect(rsiSeries([10, 11, 12, 13], 2)[3]).toBe(100)
  })

  it('needs period + 1 closes before it says anything', () => {
    expect(rsiSeries([10, 11], 2)).toEqual([null, null])
  })
})

describe('MACD', () => {
  const closes = [10, 11, 13, 12, 14, 15, 17, 16, 18, 20, 19, 21]

  it('is the difference of two SMA-seeded EMAs', () => {
    const m = macdSeries(closes, 3, 6, 3)
    const fast = emaSeries(closes, 3)
    const slow = emaSeries(closes, 6)
    for (let i = 0; i < closes.length; i++) {
      if (fast[i] === null || slow[i] === null) {
        expect(m.line[i]).toBeNull()
      } else {
        expect(m.line[i]).toBeCloseTo((fast[i] as number) - (slow[i] as number), 10)
      }
    }
  })

  it('makes the histogram the gap between line and signal, or nothing', () => {
    const m = macdSeries(closes, 3, 6, 3)
    for (let i = 0; i < closes.length; i++) {
      if (m.line[i] === null || m.signal[i] === null) {
        expect(m.hist[i]).toBeNull()
      } else {
        expect(m.hist[i]).toBeCloseTo((m.line[i] as number) - (m.signal[i] as number), 10)
      }
    }
  })
})

// ---------------------------------------------------------------------------
// Validation
// ---------------------------------------------------------------------------

describe('period validation refuses rather than crashing the chart', () => {
  const bad: Array<[string, unknown]> = [
    ['a fraction', 14.5],
    ['zero', 0],
    ['a negative', -3],
    ['an absurdly large period', 1_000_000],
    ['text', 'abc'],
    ['empty', ''],
    ['NaN', NaN],
    ['Infinity', Infinity],
    ['null', null],
    ['undefined', undefined]
  ]

  for (const [name, value] of bad) {
    it(`refuses ${name} with a message`, () => {
      const result = validatePeriod(value)
      expect(result.ok).toBe(false)
      if (!result.ok) expect(result.message.length).toBeGreaterThan(0)
    })
  }

  it('names the offence rather than saying "invalid"', () => {
    const fraction = validatePeriod(14.5)
    expect(fraction.ok).toBe(false)
    if (!fraction.ok) expect(fraction.message).toMatch(/whole number/i)

    const huge = validatePeriod(MAX_PERIOD + 1)
    expect(huge.ok).toBe(false)
    if (!huge.ok) expect(huge.message).toMatch(new RegExp(String(MAX_PERIOD)))

    const zero = validatePeriod(0)
    expect(zero.ok).toBe(false)
    if (!zero.ok) expect(zero.message).toMatch(/at least/i)
  })

  it('accepts a whole number of bars, including one typed as a string', () => {
    expect(validatePeriod(26)).toEqual({ ok: true, value: 26 })
    expect(validatePeriod(' 48 ')).toEqual({ ok: true, value: 48 })
    expect(validatePeriod(MAX_PERIOD)).toEqual({ ok: true, value: MAX_PERIOD })
  })
})

describe('moving-average settings', () => {
  it('accepts EMA and SMA over the close', () => {
    expect(validateMaSettings({ method: 'ema', period: 26, source: 'close' })).toEqual({
      ok: true,
      value: { method: 'ema', period: 26, source: 'close' }
    })
    expect(validateMaSettings({ method: 'sma', period: 20 }).ok).toBe(true)
  })

  it('refuses a method the chart cannot draw', () => {
    const result = validateMaSettings({ method: 'wma', period: 26 })
    expect(result.ok).toBe(false)
    if (!result.ok) expect(result.message).toMatch(/EMA or SMA/)
  })

  it('refuses a source the API does not serve', () => {
    const result = validateMaSettings({ method: 'ema', period: 26, source: 'hlc3' })
    expect(result.ok).toBe(false)
    if (!result.ok) expect(result.message).toMatch(/close/i)
  })

  it('refuses a bad period before it ever reaches the chart', () => {
    expect(validateMaSettings({ method: 'ema', period: 0 }).ok).toBe(false)
    expect(makeInstance('ma', { method: 'ema', period: 14.5 }).ok).toBe(false)
  })
})

describe('MACD settings', () => {
  it('accepts the classic 12/26/9', () => {
    expect(validateMacdSettings({ fast: 12, slow: 26, signal: 9 })).toEqual({
      ok: true,
      value: { fast: 12, slow: 26, signal: 9 }
    })
  })

  it('refuses a fast period that is not faster than the slow one', () => {
    const result = validateMacdSettings({ fast: 26, slow: 26, signal: 9 })
    expect(result.ok).toBe(false)
    if (!result.ok) expect(result.message).toMatch(/shorter than the slow/i)
  })

  it('names which of the three periods is wrong', () => {
    const result = validateMacdSettings({ fast: 12, slow: 26, signal: -1 })
    expect(result.ok).toBe(false)
    if (!result.ok) expect(result.message).toMatch(/Signal period/)
  })
})

// ---------------------------------------------------------------------------
// Instances, specs and persistence
// ---------------------------------------------------------------------------

function instance(kind: Parameters<typeof makeInstance>[0], settings = {}): IndicatorInstance {
  const made = makeInstance(kind, settings)
  if (!made.ok) throw new Error(made.message)
  return made.value
}

describe('instances and specs', () => {
  it('round-trips through its spec string', () => {
    for (const spec of ['ma:ema:220', 'ma:sma:20', 'rsi:14', 'macd:12:26:9', 'bollinger', 'sr']) {
      const parsed = parseSpec(spec)
      expect(parsed).not.toBeNull()
      expect(parsed?.id).toBe(spec)
      expect(parsed?.visible).toBe(true)
    }
  })

  it('carries the hidden flag through persistence', () => {
    const hidden = parseSpec('!ma:ema:26')
    expect(hidden?.visible).toBe(false)
    expect(hidden?.id).toBe('ma:ema:26')
  })

  it('drops a spec this build cannot draw instead of guessing', () => {
    expect(parseSpec('volume')).toBeNull()
    expect(parseSpec('ma:wma:26')).toBeNull()
    expect(parseSpec('ma:ema:0')).toBeNull()
    expect(parseSpec('')).toBeNull()
  })

  it('labels an instance the way a trader reads it', () => {
    expect(instanceLabel(instance('ma', { method: 'ema', period: 220 }))).toBe('EMA 220')
    expect(instanceLabel(instance('ma', { method: 'sma', period: 20 }))).toBe('SMA 20')
    expect(instanceLabel(instance('rsi', { period: 14 }))).toBe('RSI 14')
    expect(instanceLabel(instance('macd'))).toBe('MACD 12/26/9')
    expect(instanceLabel(instance('supertrend'))).toBe('SuperTrend')
  })

  it('refuses to add the same indicator twice', () => {
    const state: ChartIndicatorState = applyPreset('clean')
    const first = addInstance(state, instance('ma', { method: 'ema', period: 26 }))
    expect(first.ok).toBe(true)
    if (!first.ok) return
    const again = addInstance(first.value, instance('ma', { method: 'ema', period: 26 }))
    expect(again.ok).toBe(false)
    if (!again.ok) expect(again.message).toMatch(/already on the chart/)
  })

  it('offers a free period when the obvious one is taken', () => {
    const state = applyPreset('alignment')
    const next = nextMovingAverage(state.instances)
    expect(next).not.toBeNull()
    expect(state.instances.some((i) => i.id === next?.id)).toBe(false)
  })

  it('replaces settings in place and keeps the row order', () => {
    const state = applyPreset('alignment')
    const target = state.instances[1]
    const result = replaceInstance(state, target.id, instance('ma', { method: 'sma', period: 33 }))
    expect(result.ok).toBe(true)
    if (!result.ok) return
    expect(result.value.instances[1].id).toBe('ma:sma:33')
    expect(result.value.instances.map((i) => i.id)[0]).toBe('ma:ema:26')
  })

  it('refuses a replacement that would collide with another row', () => {
    const state = applyPreset('alignment')
    const result = replaceInstance(
      state,
      'ma:ema:26',
      instance('ma', { method: 'ema', period: 48 })
    )
    expect(result.ok).toBe(false)
  })

  it('removes and hides without losing the rest of the board', () => {
    const state = applyPreset('trend')
    expect(removeInstance(state, 'supertrend').instances.map((i) => i.id)).toEqual([
      'sma',
      'pivots',
      'sr'
    ])
    const hidden = setVisibility(state, 'sma', false)
    expect(hidden.instances.find((i) => i.id === 'sma')?.visible).toBe(false)
    expect(hidden.instances).toHaveLength(state.instances.length)
  })
})

describe('persistence', () => {
  it('round-trips the whole board through the string array prefs.ts stores', () => {
    const state = setOverlay(applyPreset('alignment'), 'forecast', true)
    const stored = serializeState(setVisibility(state, 'supertrend', false))
    expect(stored.every((s) => typeof s === 'string' && s.length > 0)).toBe(true)

    const back = deserializeState(stored)
    expect(back).not.toBeNull()
    expect(back?.instances.map((i) => i.id)).toEqual([
      'ma:ema:26',
      'ma:ema:48',
      'ma:ema:220',
      'supertrend'
    ])
    expect(back?.instances.find((i) => i.id === 'supertrend')?.visible).toBe(false)
    expect(back?.overlays).toEqual({ forecast: true, events: false, trend: true })
  })

  it('tells an empty board apart from a user who has never chosen', () => {
    // Unversioned or absent storage means "never chosen" -> the caller falls
    // back to the default preset. A versioned empty board stays empty.
    expect(deserializeState([])).toBeNull()
    expect(deserializeState(['sma', 'supertrend'])).toBeNull()

    const clean = deserializeState(serializeState(applyPreset('clean')))
    expect(clean).not.toBeNull()
    expect(clean?.instances).toEqual([])
  })

  it('skips a stored spec this build no longer understands', () => {
    const back = deserializeState(['v1', 'sma', 'volume', 'ma:ema:26', 'sma'])
    expect(back?.instances.map((i) => i.id)).toEqual(['sma', 'ma:ema:26'])
  })
})

// ---------------------------------------------------------------------------
// Presets
// ---------------------------------------------------------------------------

describe('presets apply the documented sets', () => {
  it('Clean draws nothing', () => {
    const state = applyPreset('clean')
    expect(state.instances).toEqual([])
    expect(state.overlays).toEqual({ forecast: false, events: false, trend: false })
  })

  it('Trend is the default and stays readable', () => {
    expect(DEFAULT_PRESET).toBe('trend')
    const state = applyPreset('trend')
    expect(state.instances.map((i) => i.id)).toEqual(['sma', 'supertrend', 'pivots', 'sr'])
    // Not everything: no oscillator panes and no overlays on a first visit.
    expect(state.instances.some((i) => i.kind === 'rsi' || i.kind === 'macd')).toBe(false)
    expect(Object.values(state.overlays).some(Boolean)).toBe(false)
  })

  it('Trend alignment is EMA 26 / 48 / 220 + SuperTrend and turns the server read on', () => {
    const state = applyPreset('alignment')
    expect(state.instances.map((i) => i.id)).toEqual([
      'ma:ema:26',
      'ma:ema:48',
      'ma:ema:220',
      'supertrend'
    ])
    expect(state.instances.slice(0, 3).every((i) => i.method === 'ema')).toBe(true)
    expect(state.overlays.trend).toBe(true)
  })

  it('Momentum is the two oscillator panes', () => {
    expect(applyPreset('momentum').instances.map((i) => i.id)).toEqual(['rsi:14', 'macd:12:26:9'])
  })

  it('Full technical is everything the server serves plus both panes', () => {
    const state = applyPreset('full')
    expect(state.instances.map((i) => i.id)).toEqual([
      'sma',
      'bollinger',
      'supertrend',
      'psar',
      'ichimoku',
      'pivots',
      'sr',
      'rsi:14',
      'macd:12:26:9'
    ])
  })

  it('recognises the preset a board matches, and reports a custom board as none', () => {
    expect(matchingPreset(applyPreset('momentum'))).toBe('momentum')
    expect(matchingPreset(setOverlay(applyPreset('momentum'), 'events', true))).toBeNull()
    const custom = removeInstance(applyPreset('trend'), 'sma')
    expect(matchingPreset(custom)).toBeNull()
  })

  it('never enables every overlay at once', () => {
    for (const id of ['clean', 'trend', 'alignment', 'momentum', 'full'] as const) {
      const overlays = applyPreset(id).overlays
      expect(Object.values(overlays).filter(Boolean).length).toBeLessThanOrEqual(1)
    }
  })
})

// ---------------------------------------------------------------------------
// Plots
// ---------------------------------------------------------------------------

const CANDLES = series([10, 11, 13, 12, 14, 15, 17, 16, 18, 20, 19, 21])

function overlays(partial: Partial<CandleOverlays>): CandleOverlays {
  const nulls = () => CANDLES.map(() => null)
  return {
    sma_20: nulls(),
    sma_50: nulls(),
    bollinger_upper: nulls(),
    bollinger_mid: nulls(),
    bollinger_lower: nulls(),
    supertrend: nulls(),
    supertrend_dir: CANDLES.map(() => 1),
    psar: nulls(),
    ichimoku_tenkan: nulls(),
    ichimoku_kijun: nulls(),
    ichimoku_senkou_a: nulls(),
    ichimoku_senkou_b: nulls(),
    ...partial
  }
}

const CTX = {
  candles: CANDLES,
  overlays: overlays({ sma_20: CANDLES.map((c) => c.close), supertrend: CANDLES.map(() => 9) }),
  overlayTimes: CANDLES.map((c) => c.t)
}

describe('buildPlots', () => {
  it('plots a computed moving average against the candle times', () => {
    const plots = buildPlots([instance('ma', { method: 'sma', period: 3 })], CTX)
    expect(plots).toHaveLength(1)
    expect(plots[0].owner).toBe('layer')
    expect(plots[0].paneKey).toBeNull()
    // The first two buckets have no 3-bar mean, so they carry no point at all.
    expect(plots[0].data[0].time).toBe(CANDLES[2].t)
    expect(plots[0].data[0].value).toBeCloseTo((10 + 11 + 13) / 3, 10)
  })

  it('leaves the server to draw the server series, and only describes them', () => {
    const plots = buildPlots([instance('sma'), instance('supertrend')], CTX)
    expect(plots.every((p) => p.owner === 'chart')).toBe(true)
    // sma_50 is all nulls in this fixture, so it contributes no points.
    const sma20 = plots.find((p) => p.key === 'sma:sma_20')
    expect(sma20?.data).toHaveLength(CANDLES.length)
    expect(sma20?.data[0].time).toBe(CANDLES[0].t)
  })

  it('matches server values to the bucket times they were computed against', () => {
    // A window that starts later than the candles: values must land on the
    // buckets the API named, never on array position 0.
    const late = { ...CTX, overlayTimes: CANDLES.slice(4).map((c) => c.t) }
    const shifted = {
      ...late,
      overlays: overlays({ sma_20: CANDLES.slice(4).map((c) => c.close) })
    }
    const plots = buildPlots([instance('sma')], shifted)
    expect(plots[0].data[0].time).toBe(CANDLES[4].t)
  })

  it('gives each oscillator its own pane', () => {
    const rsi = instance('rsi', { period: 3 })
    const macd = instance('macd', { fast: 3, slow: 6, signal: 3 })
    const plots = buildPlots([rsi, macd], CTX)
    expect(plots.find((p) => p.instanceId === rsi.id)?.paneKey).toBe(rsi.id)
    const macdPlots = plots.filter((p) => p.instanceId === macd.id)
    expect(macdPlots).toHaveLength(3)
    expect(new Set(macdPlots.map((p) => p.paneKey))).toEqual(new Set([macd.id]))
    expect(macdPlots.some((p) => p.shape === 'histogram')).toBe(true)
  })

  it('never produces a volume plot — this data source has no volume', () => {
    const plots = buildPlots(applyPreset('full').instances, CTX)
    expect(plots.some((p) => /volume/i.test(p.key) || /volume/i.test(p.label))).toBe(false)
  })

  it('reports an indicator with no history as cold rather than silently blank', () => {
    const slow = instance('ma', { method: 'ema', period: 220 })
    const plots = buildPlots([slow], CTX)
    expect(plots[0].data).toEqual([])
    expect(coldInstances([slow], plots).map((i) => i.id)).toEqual([slow.id])
  })

  it('does not call a levels-only indicator cold — it has no series to warm up', () => {
    const pivots = instance('pivots')
    expect(coldInstances([pivots], buildPlots([pivots], CTX))).toEqual([])
  })
})

describe('server overlay selection', () => {
  it('asks the chart for exactly the fields the visible instances need', () => {
    const state = applyPreset('trend')
    expect(serverOverlayFields(state.instances)).toEqual(['sma_20', 'sma_50', 'supertrend'])
  })

  it('drops a hidden indicator from the overlays the chart is given', () => {
    const state = setVisibility(applyPreset('trend'), 'sma', false)
    expect(serverOverlayFields(state.instances)).toEqual(['supertrend'])
    const picked = serverOverlays(state.instances, CTX.overlays)
    expect(Object.keys(picked ?? {})).toEqual(['supertrend'])
  })

  it('returns null when the response carried no overlays at all', () => {
    expect(serverOverlays(applyPreset('trend').instances, null)).toBeNull()
  })

  it('answers whether a levels indicator is on', () => {
    expect(hasInstance(applyPreset('trend').instances, 'pivots')).toBe(true)
    expect(hasInstance(applyPreset('momentum').instances, 'pivots')).toBe(false)
  })
})

describe('valueAt', () => {
  const plot = buildPlots([instance('ma', { method: 'sma', period: 3 })], CTX)[0]

  it('reads the value at the crosshair bucket', () => {
    expect(valueAt(plot, CANDLES[2].t)).toBeCloseTo((10 + 11 + 13) / 3, 10)
  })

  it('falls back to the latest value when the pointer is off the chart', () => {
    expect(valueAt(plot, null)).toBe(plot.data[plot.data.length - 1].value)
  })

  it('returns nothing — never a neighbouring bar — for a bucket it has no value for', () => {
    expect(valueAt(plot, CANDLES[0].t)).toBeNull()
    expect(valueAt(plot, BASE_T - DAY)).toBeNull()
  })
})

// ---------------------------------------------------------------------------
// Forecast overlay
// ---------------------------------------------------------------------------

function prediction(overrides: Partial<Prediction>): Prediction {
  return {
    id: 1,
    horizon: '1d',
    target_time: '2026-08-20T00:00:00Z',
    point_forecast: 8_200_000,
    lower_bound: 8_000_000,
    upper_bound: 8_400_000,
    expected_change_pct: 1,
    direction: 'up',
    confidence: 0.6,
    model_name: 'test',
    actual_value: null,
    ...overrides
  }
}

describe('forecast overlay', () => {
  const last = CANDLES[CANDLES.length - 1]

  it('draws only forecasts that land after the last candle', () => {
    const points = forecastPoints(
      [
        prediction({ target_time: new Date((last.t - DAY) * 1000).toISOString() }),
        prediction({ id: 2, target_time: new Date((last.t + DAY) * 1000).toISOString() })
      ],
      last
    )
    // One bridge point on the last candle, then the single future target.
    expect(points.map((p) => p.time)).toEqual([last.t, last.t + DAY])
  })

  it('bridges from the last close so the estimate does not float', () => {
    const points = forecastPoints(
      [prediction({ target_time: new Date((last.t + DAY) * 1000).toISOString() })],
      last
    )
    expect(points[0]).toEqual({
      time: last.t,
      point: last.close,
      lower: last.close,
      upper: last.close
    })
  })

  it('merges horizons on the same target into the widest interval anyone claimed', () => {
    const t = new Date((last.t + DAY) * 1000).toISOString()
    const points = forecastPoints(
      [
        prediction({ target_time: t, point_forecast: 100, lower_bound: 90, upper_bound: 110 }),
        prediction({ id: 2, target_time: t, point_forecast: 120, lower_bound: 80, upper_bound: 130 })
      ],
      last
    )
    expect(points[1]).toEqual({ time: last.t + DAY, point: 110, lower: 80, upper: 130 })
  })

  it('drops a row with no parseable target or no point estimate', () => {
    const points = forecastPoints(
      [
        prediction({ target_time: 'not a date' }),
        prediction({
          id: 2,
          target_time: new Date((last.t + DAY) * 1000).toISOString(),
          point_forecast: undefined,
          predicted_value: undefined
        })
      ],
      last
    )
    expect(points).toEqual([])
  })

  it('draws nothing at all when no forecast survives', () => {
    expect(forecastPlots(forecastPoints([], last))).toEqual([])
  })

  it('draws the point estimate and both interval edges, tagged EST on the axis', () => {
    const plots = forecastPlots(
      forecastPoints(
        [prediction({ target_time: new Date((last.t + DAY) * 1000).toISOString() })],
        last
      )
    )
    expect(plots.map((p) => p.key)).toEqual([
      'forecast:upper',
      'forecast:lower',
      'forecast:point'
    ])
    const point = plots[2]
    expect(point.axisLabel).toBe('EST')
    // Its own colour and a heavy dash: not confusable with an indicator.
    expect(point.color).toBe('forecast')
    expect(point.style).toBe('largeDashed')
    expect(plots[0].color).toBe('forecast-band')
  })
})

// ---------------------------------------------------------------------------
// Event overlay
// ---------------------------------------------------------------------------

function newsItem(overrides: Partial<NewsItem>): NewsItem {
  return {
    id: 1,
    source_code: 'x',
    source_name: 'Example',
    title: 'Headline',
    url: '',
    published_at: null,
    published_at_estimated: false,
    available_at: '2026-08-05T00:00:00Z',
    urgency: 'normal',
    tags: [],
    entities: [],
    independent_source_count: 1,
    duplicate_count: 0,
    ...overrides
  }
}

describe('event overlay', () => {
  it('is empty for an empty feed — which is what production actually returns', () => {
    expect(placeEvents([], CANDLES, DAY)).toEqual({ events: [], undated: 0, outside: 0 })
  })

  it('never places a marker for a headline with no publication time', () => {
    const placement = placeEvents([newsItem({ published_at: null })], CANDLES, DAY)
    expect(placement.events).toEqual([])
    expect(placement.undated).toBe(1)
  })

  it('never places a marker for an unparseable publication time', () => {
    const placement = placeEvents([newsItem({ published_at: 'yesterday' })], CANDLES, DAY)
    expect(placement.events).toEqual([])
    expect(placement.undated).toBe(1)
  })

  it('snaps a dated headline to the bucket that contains it', () => {
    const inside = new Date((CANDLES[3].t + 3_600) * 1000).toISOString()
    const placement = placeEvents(
      [newsItem({ published_at: inside, urgency: 'urgent' })],
      CANDLES,
      DAY
    )
    expect(placement.events).toHaveLength(1)
    expect(placement.events[0].time).toBe(CANDLES[3].t)
    expect(placement.events[0].urgent).toBe(true)
  })

  it('counts headlines outside the loaded window instead of clamping them to an edge', () => {
    const before = new Date((CANDLES[0].t - DAY) * 1000).toISOString()
    const after = new Date((CANDLES[CANDLES.length - 1].t + 5 * DAY) * 1000).toISOString()
    const placement = placeEvents(
      [newsItem({ published_at: before }), newsItem({ id: 2, published_at: after })],
      CANDLES,
      DAY
    )
    expect(placement.events).toEqual([])
    expect(placement.outside).toBe(2)
  })

  it('places nothing when there are no candles to place against', () => {
    const placement = placeEvents(
      [newsItem({ published_at: '2026-08-05T00:00:00Z' })],
      [],
      DAY
    )
    expect(placement.events).toEqual([])
  })
})

describe('trend overlay symbol guard', () => {
  it('accepts only the two symbols the endpoint serves', () => {
    expect(isTrendSymbol('IR_GOLD_18K')).toBe(true)
    expect(isTrendSymbol('XAUUSD')).toBe(true)
    expect(isTrendSymbol('USD_IRT')).toBe(false)
  })
})
