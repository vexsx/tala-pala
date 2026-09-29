import { describe, expect, it } from 'vitest'
import type { BourseIndexItem, InstrumentItem, StockRosterItem } from '../api/types'
import {
  FORECAST_SYMBOLS,
  TSE_INDEX,
  indicatorSupport,
  isTehranSymbol,
  overlaySupport,
  parseChartSymbol,
  registerRegistryQuotes,
  seriesModeFor,
  symbolIntervals,
  symbolKind,
  symbolQuote,
  tehranCode
} from '../chart/symbols'
import {
  STATIC_CATALOG,
  buildCatalog,
  catalogIndex,
  isChartableInstrument,
  type ChartSymbolGroup
} from '../chart/catalog'
import { readSymbol, writeSymbol } from '../chart/prefs'
import { formatChartPrice } from '../chart/OhlcHeader'

/**
 * The chart's symbol vocabulary — the frontend twin of
 * backend-go/internal/prices/chart_symbols.go — and the picker built on it.
 */

const TEDPIX = TSE_INDEX.TEDPIX
const FOOLAD = 'EQ:46348559193224090'

describe('parseChartSymbol', () => {
  it('accepts the three shapes and normalizes them', () => {
    expect(parseChartSymbol('IR_GOLD_18K')).toBe('IR_GOLD_18K')
    expect(parseChartSymbol(' ir_silver_999 ')).toBe('IR_SILVER_999')
    expect(parseChartSymbol('idx:32097828799138957')).toBe(TEDPIX)
    expect(parseChartSymbol(FOOLAD)).toBe(FOOLAD)
  })

  it('refuses anything that is neither shape', () => {
    for (const junk of ['', '   ', 'IDX:', 'IDX:abc', 'EQ:12345', 'FUND:32097828799138957', 'فولاد', '<script>', '1ABC']) {
      expect(parseChartSymbol(junk)).toBeNull()
    }
    expect(parseChartSymbol(null)).toBeNull()
    expect(parseChartSymbol(undefined)).toBeNull()
  })

  it('knows each shape’s kind, code and timeframes', () => {
    expect(symbolKind('XAUUSD')).toBe('ticks')
    expect(symbolKind(TEDPIX)).toBe('tse_index')
    expect(symbolKind(FOOLAD)).toBe('tse_equity')
    expect(isTehranSymbol(FOOLAD)).toBe(true)
    expect(tehranCode(TEDPIX)).toBe('32097828799138957')
    expect(tehranCode('XAUUSD')).toBeNull()
    // Tick symbols take their timeframes from the data; Tehran ones are daily.
    expect(symbolIntervals('IR_GOLD_18K')).toBeNull()
    expect(symbolIntervals(TEDPIX)).toEqual(['1d'])
    expect(symbolIntervals(FOOLAD)).toEqual(['1d'])
  })
})

describe('the stored symbol', () => {
  it('comes back when it is a Tehran index', () => {
    writeSymbol(TEDPIX)
    expect(readSymbol()).toBe(TEDPIX)
  })

  it('falls back to 18k gold when what is stored is not a symbol', () => {
    for (const junk of ['IDX:abc', '"><img>', '']) {
      window.localStorage.setItem('igp_chart_symbol', junk)
      expect(readSymbol()).toBe('IR_GOLD_18K')
    }
  })
})

describe('what may be drawn over which symbol', () => {
  it('draws a forecast only where the models forecast', () => {
    expect(FORECAST_SYMBOLS).toEqual(['IR_GOLD_18K', 'XAUUSD'])
    expect(overlaySupport('XAUUSD', 'forecast').ok).toBe(true)
    for (const s of [TEDPIX, FOOLAD, 'USD_IRT', 'IR_SILVER_999']) {
      const support = overlaySupport(s, 'forecast')
      expect(support.ok).toBe(false)
      if (!support.ok) expect(support.reason).toContain('18k gold and XAU/USD only')
    }
  })

  it('never places gold and macro headlines on a Tehran chart', () => {
    expect(overlaySupport('IR_GOLD_18K', 'events').ok).toBe(true)
    const support = overlaySupport(TEDPIX, 'events')
    expect(support.ok).toBe(false)
    if (!support.ok) expect(support.reason).toMatch(/causes/)
  })

  it('refuses the high/low indicators on a close-only series, with the reason', () => {
    for (const kind of ['supertrend', 'psar', 'ichimoku', 'pivots'] as const) {
      const support = indicatorSupport(kind, ['close'])
      expect(support.ok).toBe(false)
      if (!support.ok) expect(support.reason).toContain('high and low')
      // A series that has them, and a tick response that does not say, are fine.
      expect(indicatorSupport(kind, ['open', 'high', 'low', 'close']).ok).toBe(true)
      expect(indicatorSupport(kind, null).ok).toBe(true)
    }
    for (const kind of ['ma', 'sma', 'bollinger', 'sr', 'rsi', 'macd'] as const) {
      expect(indicatorSupport(kind, ['close']).ok).toBe(true)
    }
  })

  it('draws a close-only series as a line', () => {
    expect(seriesModeFor(TEDPIX, ['close'])).toBe('line')
    expect(seriesModeFor(TEDPIX, null)).toBe('line')
    expect(seriesModeFor(FOOLAD, ['open', 'high', 'low', 'close'])).toBe('candles')
    expect(seriesModeFor('IR_GOLD_18K', null)).toBe('candles')
  })
})

describe('units', () => {
  it('writes each kind of symbol in its own unit', () => {
    expect(symbolQuote(TEDPIX)).toBe('points')
    expect(symbolQuote(FOOLAD)).toBe('IRR')
    expect(symbolQuote('XAUUSD')).toBe('USD')
    expect(symbolQuote('IR_GOLD_18K')).toBe('IRT')
    // An index level ignores the toman/rial toggle; a share's rials are never ×10.
    expect(formatChartPrice(3_713_955.9, TEDPIX, 'IRR')).toBe('3,713,956')
    expect(formatChartPrice(272.7, TEDPIX, 'IRR')).toBe('272.7')
    expect(formatChartPrice(2_881, FOOLAD, 'IRR')).toBe('2,881')
    expect(formatChartPrice(7.25, FOOLAD, 'IRT')).toBe('7.25')
    expect(formatChartPrice(8_120_000, 'IR_GOLD_18K', 'IRR')).toBe('81,200,000')
  })

  it('learns a registry code’s currency from the registry', () => {
    expect(symbolQuote('PLATINUM_USD_TEST')).toBe('IRT')
    registerRegistryQuotes([instrument('PLATINUM_USD_TEST', 'global', 'market_price', 'USD')])
    expect(symbolQuote('PLATINUM_USD_TEST')).toBe('USD')
    expect(formatChartPrice(1020.5, 'PLATINUM_USD_TEST', 'IRR')).toBe('$1,020.50')
  })
})

// ---------------------------------------------------------------------------
// The picker
// ---------------------------------------------------------------------------

function instrument(
  code: string,
  domain: string,
  kind = 'market_price',
  quote = 'IRT',
  extra: Partial<InstrumentItem> = {}
): InstrumentItem {
  return {
    code,
    kind,
    name_en: code,
    name_fa: '',
    domain,
    quote_currency: quote,
    unit: 'unit',
    decimals: 0,
    calendar_class: 'always_open',
    quality_tier: 'official_mirror',
    is_proxy: false,
    is_derived: false,
    enabled: true,
    notes: '',
    ...extra
  }
}

const REGISTRY: InstrumentItem[] = [
  instrument('IR_GOLD_18K', 'gold', 'market_price', 'IRT', { name_en: 'Iranian 18k gold, per gram' }),
  instrument('IR_COIN_EMAMI', 'gold'),
  instrument('IR_SILVER_999', 'silver', 'market_price', 'IRT', { name_en: 'Silver 999 (gram, Tehran)', name_fa: 'نقره ۹۹۹ (گرم)' }),
  instrument('IR_GOLD_FUND_AYAR', 'fund'),
  instrument('IR_SILVER_FUND_SILVER', 'fund'),
  instrument('USD_IRT', 'fx', 'fx', 'IRT', { name_en: 'US dollar, free market', is_proxy: true }),
  instrument('XAUUSD', 'global', 'market_price', 'USD', { name_en: 'Gold, COMEX front month (spot proxy)', is_proxy: true }),
  instrument('BRENT_OIL', 'global', 'market_price', 'USD'),
  // Not prices: a flow ratio, a yield, a dollar index, a monthly series.
  instrument('IR_GOLD_FUND_FLOW', 'fund', 'index', 'PCT'),
  instrument('US10Y', 'global', 'market_price', 'PCT'),
  instrument('DXY', 'global', 'index', 'INDEX'),
  instrument('CPI_IR', 'macro', 'economic_series', 'INDEX')
]

function index(
  code: string,
  kind: string,
  market: string,
  order: number,
  extra: Partial<BourseIndexItem> = {}
): BourseIndexItem {
  return {
    ins_code: code,
    name_fa: `fa-${code}`,
    name_en: `en-${code}`,
    market,
    kind,
    sector_code: '',
    weighting: 'unstated',
    return_basis: 'unstated',
    display_order: order,
    first_date: '2008-01-01',
    last_date: '2026-09-28',
    value_count: 4000,
    check: { status: 'validated' } as BourseIndexItem['check'],
    last: { date: '2026-09-28', value: 1000 },
    ...extra
  } as BourseIndexItem
}

const INDICES: BourseIndexItem[] = [
  index('43685683301327984', 'headline', 'farabourse', 200, { name_en: 'IFX, Farabourse all-share' }),
  index('32097828799138957', 'headline', 'bourse', 10, { name_en: 'TEDPIX, all-share (price and dividends)' }),
  index('71704845530629737', 'market', 'bourse', 70, {
    check: { status: 'refused', refusal_reason: 'the newest value is 10.0 times the live figure' } as BourseIndexItem['check']
  }),
  index('49579049405614711', 'segment', 'bourse', 50),
  index('34408080767216529', 'sector', 'bourse', 1010, { sector_code: '01', name_en: 'Agriculture' }),
  index('11111111111111111', 'sector', 'farabourse', 2010),
  index('22222222222222222', 'sector', 'bourse', 1020, { last: null })
]

function stock(symbol: string, code: string, extra: Partial<StockRosterItem> = {}): StockRosterItem {
  return {
    ins_code: code,
    symbol,
    name_fa: `نام ${symbol}`,
    market: 'bourse',
    board: '',
    sector_code: '27',
    sector_fa: 'فلزات اساسی',
    isin: '',
    bar_count: 4000,
    enabled: true,
    adjustment: {
      version: 'priceYesterday-chain-v1',
      status: 'validated',
      actions_applied: 3,
      reopenings: 0,
      adjusted_servable: true
    },
    ...extra
  }
}

const STOCKS: StockRosterItem[] = [
  stock('فولاد', '46348559193224090'),
  stock('کچاد', '18027801615184692', {
    enabled: false,
    adjustment: {
      version: 'v1',
      status: 'refused',
      actions_applied: 47,
      reopenings: 0,
      refusal_reason: '-60.4% survives adjustment',
      adjusted_servable: false
    }
  }),
  stock('شستا', '2400322364771558', {
    adjustment: {
      version: 'v1',
      status: 'refused',
      actions_applied: 5,
      reopenings: 0,
      refusal_reason: 'gap',
      adjusted_servable: false
    }
  })
]

function labels(groups: ChartSymbolGroup[]): string[] {
  return groups.map((g) => g.label)
}

describe('the picker', () => {
  it('renders the gold symbols and the headline Tehran indices before anything answers', () => {
    expect(labels(STATIC_CATALOG)).toEqual(['Gold & coins', 'Global markets', 'Tehran · All-share'])
    const all = catalogIndex(STATIC_CATALOG)
    expect(all.get('IR_GOLD_18K')).toBeTruthy()
    expect(all.get('XAUUSD')).toBeTruthy()
    for (const s of Object.values(TSE_INDEX)) expect(all.get(s)?.disabled).toBeUndefined()
    expect(all.get(TEDPIX)?.short).toBe('TEDPIX')
  })

  it('groups everything in the order a reader scans it', () => {
    const groups = buildCatalog({ instruments: REGISTRY, indices: INDICES, stocks: STOCKS })
    expect(labels(groups)).toEqual([
      'Gold & coins',
      'Silver',
      'Commodity funds',
      'Currency',
      'Global markets',
      'Tehran · All-share',
      'Tehran · Boards & segments',
      'Bourse · Sectors',
      'Farabourse · Sectors',
      'Tehran · Shares'
    ])
    const all = catalogIndex(groups)
    // Headlines by display order: TEDPIX before IFX.
    const headline = groups.find((g) => g.id === 'tse:headline')!
    expect(headline.options.map((o) => o.symbol)).toEqual([TEDPIX, 'IDX:43685683301327984'])
    expect(all.get('IDX:34408080767216529')?.label).toBe('01 · Agriculture — fa-34408080767216529')
    expect(all.get('IR_SILVER_999')?.label).toBe('Silver 999 (gram, Tehran) — نقره ۹۹۹ (گرم)')
    // A proxy says so on the option when its name does not.
    expect(all.get('USD_IRT')?.label).toBe('US dollar, free market (proxy)')
    expect(all.get('XAUUSD')?.label).toBe('Gold, COMEX front month (spot proxy)')
  })

  it('offers only registry rows that are prices', () => {
    const offered = REGISTRY.filter(isChartableInstrument).map((i) => i.code)
    expect(offered).not.toContain('IR_GOLD_FUND_FLOW')
    expect(offered).not.toContain('US10Y')
    expect(offered).not.toContain('DXY')
    expect(offered).not.toContain('CPI_IR')
    expect(isChartableInstrument(instrument('X_DISABLED', 'gold', 'market_price', 'IRT', { enabled: false }))).toBe(false)
  })

  it('shows what cannot be charted, disabled, with the reason', () => {
    const all = catalogIndex(buildCatalog({ instruments: REGISTRY, indices: INDICES, stocks: STOCKS }))
    const refusedIndex = all.get('IDX:71704845530629737')!
    expect(refusedIndex.disabled).toContain('10.0 times the live figure')
    expect(refusedIndex.label).toMatch(/\(correction refused\)$/)
    expect(all.get('IDX:22222222222222222')?.disabled).toContain('No session')
    // کچاد is disabled BECAUSE its adjustment was refused: the measured reason wins.
    expect(all.get('EQ:18027801615184692')?.disabled).toContain('-60.4% survives adjustment')
    expect(all.get('EQ:2400322364771558')?.disabled).toContain('adjustment was not validated: gap')
    const parked = catalogIndex(
      buildCatalog({ instruments: null, indices: null, stocks: [stock('ذوب', '1234567', { enabled: false })] })
    )
    expect(parked.get('EQ:1234567')?.disabled).toBe('Disabled in the roster.')
    expect(all.get(FOOLAD)?.disabled).toBeUndefined()
    expect(all.get(FOOLAD)?.short).toBe('فولاد')
    expect(all.get(TEDPIX)?.disabled).toBeUndefined()
  })

  it('keeps the gold symbols when the registry leaves them out', () => {
    const groups = buildCatalog({ instruments: [instrument('IR_SILVER_999', 'silver')], indices: null, stocks: null })
    const all = catalogIndex(groups)
    expect(all.get('IR_GOLD_18K')).toBeTruthy()
    expect(all.get('XAUUSD')).toBeTruthy()
    expect(all.get('IR_SILVER_999')).toBeTruthy()
  })

  it('names a registry domain it has never heard of rather than dropping it', () => {
    const groups = buildCatalog({ instruments: [instrument('IR_COPPER', 'metals')], indices: null, stocks: null })
    expect(labels(groups)).toContain('metals')
  })
})
