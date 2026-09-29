import { beforeEach, describe, expect, it, vi, type Mock } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import RelativeValue from '../pages/RelativeValue'
import { SettingsProvider } from '../lib/settings'
// The page's own source, so the research-register rule cannot quietly drift.
import relativeValueSource from '../pages/RelativeValue.tsx?raw'
import type {
  PercentileBasis,
  RelativeValuePoint,
  RelativeValueResponse
} from '../api/types'

vi.mock('../api/client', () => ({
  api: vi.fn(() => new Promise(() => undefined)),
  errorMessage: (err: unknown) => (err instanceof Error ? err.message : 'Unexpected error')
}))
import { api } from '../api/client'

const apiMock = api as unknown as Mock

const INSTRUMENTS = {
  items: [
    {
      code: 'IR_GOLD_18K',
      kind: 'market',
      name_en: 'Tehran 18k gold',
      name_fa: 'طلای ۱۸ عیار',
      domain: 'gold',
      quote_currency: 'IRT',
      unit: 'gram',
      decimals: 0,
      calendar_class: 'iran_market',
      quality_tier: 'official_mirror',
      is_proxy: false,
      is_derived: false,
      enabled: true,
      notes: ''
    },
    {
      code: 'USD_IRT',
      kind: 'market',
      name_en: 'US dollar (free market)',
      name_fa: 'دلار آزاد',
      domain: 'fx',
      quote_currency: 'IRT',
      unit: 'unit',
      decimals: 0,
      calendar_class: 'always_open',
      quality_tier: 'proxy',
      is_proxy: true,
      is_derived: false,
      enabled: true,
      notes: 'Collected from the USDT/toman market as a documented free-market proxy.'
    },
    {
      code: 'XAUUSD',
      kind: 'market',
      name_en: 'Global gold',
      name_fa: 'طلای جهانی',
      domain: 'global',
      quote_currency: 'USD',
      unit: 'ounce',
      decimals: 2,
      calendar_class: 'global_market',
      quality_tier: 'commercial',
      is_proxy: false,
      is_derived: false,
      enabled: true,
      notes: ''
    }
  ],
  count: 3
}

/** A registry row as GET /instruments marshals it (economic/handlers.go). */
function registryRow(
  code: string,
  domain: string,
  name_en: string,
  extra: Partial<(typeof INSTRUMENTS.items)[number]> = {}
): (typeof INSTRUMENTS.items)[number] {
  return {
    code,
    kind: 'market_price',
    name_en,
    name_fa: '',
    domain,
    quote_currency: 'IRT',
    unit: 'unit',
    decimals: 0,
    calendar_class: 'tehran_bazaar',
    quality_tier: 'official_mirror',
    is_proxy: false,
    is_derived: false,
    enabled: true,
    notes: '',
    ...extra
  }
}

/**
 * The registry after migration 0031, in the order /instruments returns it
 * (kind, domain, code) — including the three kinds of row the endpoint cannot
 * pair: an economic series (no daily closes), and two rates in percent.
 */
const REGISTRY_0031 = {
  items: [
    registryRow('SCI_CPI_URBAN', 'macro', 'Urban CPI (SCI)', {
      kind: 'economic_series',
      quote_currency: 'INDEX'
    }),
    INSTRUMENTS.items[1],
    registryRow('IR_GOLD_FUND_FLOW', 'fund', 'Gold-fund flow ratio', {
      kind: 'index',
      quote_currency: 'PCT'
    }),
    registryRow('IR_GOLD_FUND_AYAR', 'fund', 'Ayar gold ETF'),
    registryRow('US10Y', 'global', 'US 10-year Treasury yield', { quote_currency: 'PCT' }),
    INSTRUMENTS.items[2],
    registryRow('IR_COIN_BAHAR', 'gold', 'Bahar Azadi gold coin'),
    registryRow('IR_COIN_EMAMI', 'gold', 'Emami gold coin'),
    INSTRUMENTS.items[0],
    registryRow('IR_SILVER_999', 'silver', 'Silver 999 (gram, Tehran)', {
      quality_tier: 'commercial'
    })
  ],
  count: 10
}

/** Three years of monthly points is enough shape for the chart, not the maths. */
const SERIES: RelativeValuePoint[] = Array.from({ length: 12 }, (_, i) => ({
  t: 1441756800 + i * 30 * 86400,
  a_indexed: 100 + i * 5,
  b_indexed: 100 + i * 6,
  ratio: 1 - i * 0.004,
  gap_pct: -i * 0.4
}))

const INSUFFICIENT: PercentileBasis = {
  window_days: 365,
  overlapping_windows: 880,
  independent_windows: 3,
  min_independent_windows: 5,
  sufficient: false,
  note: 'overlapping windows are not independent observations'
}

function response(overrides: Partial<RelativeValueResponse> = {}): RelativeValueResponse {
  return {
    a: {
      code: 'IR_GOLD_18K',
      name_en: 'Tehran 18k gold',
      name_fa: 'طلای ۱۸ عیار',
      quality_tier: 'official_mirror',
      is_proxy: false,
      unit: 'gram'
    },
    b: {
      code: 'USD_IRT',
      name_en: 'US dollar (free market)',
      name_fa: 'دلار آزاد',
      quality_tier: 'proxy',
      is_proxy: true,
      unit: 'unit'
    },
    base_date: '2015-09-09T00:00:00Z',
    from: '2015-09-09T00:00:00Z',
    to: '2026-09-09T00:00:00Z',
    as_of: '2026-09-09T00:00:00Z',
    series: SERIES,
    a_growth_pct: 620.0,
    b_growth_pct: 780.0,
    gap_pct: -17.98,
    gap_percentile: null,
    percentile_basis: INSUFFICIENT,
    ratio_drawdown_pct: -12.0,
    warnings: [],
    ...overrides
  }
}

function mockApi(payload: RelativeValueResponse = response(), registry: unknown = INSTRUMENTS): void {
  apiMock.mockImplementation((path: string) => {
    if (path.startsWith('/instruments')) return Promise.resolve(registry)
    if (path.startsWith('/relative-value')) return Promise.resolve(payload)
    return Promise.resolve({})
  })
}

async function renderPage(
  payload: RelativeValueResponse = response(),
  registry: unknown = INSTRUMENTS
): Promise<void> {
  mockApi(payload, registry)
  render(
    <SettingsProvider>
      <RelativeValue />
    </SettingsProvider>
  )
  await screen.findByTestId('rv-headline')
}

function paths(): string[] {
  return apiMock.mock.calls.map((c: unknown[]) => String(c[0]))
}

beforeEach(() => {
  window.localStorage.clear()
  apiMock.mockReset()
})

describe('RelativeValue — the percentile travels with its evidence', () => {
  it('states the shortfall in words rather than hiding the field', async () => {
    await renderPage()

    const block = screen.getByTestId('rv-percentile')
    expect(block.textContent).toContain('Percentile not reported')
    expect(block.textContent).toContain('3 independent windows; 5 needed.')
    expect(block.textContent).toContain('overlapping windows are not independent observations')
    expect(block.className).toContain('rv-percentile-insufficient')
  })

  it('shows no percentile number in place of the missing one', async () => {
    await renderPage()

    const block = screen.getByTestId('rv-percentile')
    expect(block.textContent).not.toContain('percentile 0')
    expect(block.textContent).not.toMatch(/sits at percentile/)
    // The counts that ARE reported stay visible; the ranking itself does not.
    expect(block.textContent).toContain('880 overlapping windows')
    expect(block.textContent).toContain('Rolling window: 365 days.')
  })

  it('reports the percentile once the gate passes, still with its basis', async () => {
    await renderPage(
      response({
        gap_percentile: 12.5,
        percentile_basis: { ...INSUFFICIENT, independent_windows: 7, sufficient: true }
      })
    )

    const block = screen.getByTestId('rv-percentile')
    expect(block.textContent).toContain('sits at percentile')
    expect(within(block).getByText('12.5')).toBeInTheDocument()
    expect(block.textContent).toContain('7 independent windows; 5 needed.')
    expect(block.className).not.toContain('rv-percentile-insufficient')
  })

  it('withholds a number when the gate passed but no percentile came with it', async () => {
    await renderPage(
      response({
        gap_percentile: null,
        percentile_basis: { ...INSUFFICIENT, independent_windows: 7, sufficient: true }
      })
    )

    const block = screen.getByTestId('rv-percentile')
    expect(block.textContent).toContain('Percentile not reported')
    expect(block.textContent).toContain(
      'The evidence gate passed but no percentile accompanied it, so none is shown.'
    )
  })

  it('says so when no basis accompanied the response at all', async () => {
    await renderPage(response({ percentile_basis: null, gap_percentile: null }))

    const block = screen.getByTestId('rv-percentile')
    expect(block.textContent).toContain('Percentile not reported')
    expect(block.textContent).toContain('The response carried no percentile basis')
  })
})

describe('RelativeValue — research register', () => {
  it('states the measured gap in the past tense', async () => {
    await renderPage()
    expect(screen.getByTestId('rv-headline').textContent).toBe(
      'Tehran 18k gold has lagged US dollar (free market) by 17.98% over this window.'
    )
  })

  it('says "led" for a positive gap, with the same shape', async () => {
    await renderPage(response({ gap_pct: 21.5 }))
    expect(screen.getByTestId('rv-headline').textContent).toBe(
      'Tehran 18k gold has led US dollar (free market) by 21.50% over this window.'
    )
  })

  it('reports no gap rather than inventing one', async () => {
    await renderPage(response({ gap_pct: null }))
    expect(screen.getByTestId('rv-headline').textContent).toContain('No gap is reported')
    expect(screen.getByTestId('rv-headline').textContent).not.toContain('0.00%')
  })

  it('never suggests the gap resolves', () => {
    for (const pattern of [
      /catch\s*up/i,
      /\bis due\b/i,
      /\bwill\b/i,
      /\bshould\b/i,
      /\bmust\b/i,
      /\bexpect/i,
      /\bbuy\b/i,
      /\bsell\b/i,
      /\bundervalued\b/i,
      /\bovervalued\b/i
    ]) {
      expect(relativeValueSource).not.toMatch(pattern)
    }
  })
})

describe('RelativeValue — provenance', () => {
  it('marks the proxy leg and not the official-mirror one', async () => {
    await renderPage()

    const legs = document.querySelectorAll('.rv-leg')
    const [legA, legB] = Array.from(legs) as HTMLElement[]
    expect(within(legA).queryByText('PROXY')).toBeNull()
    expect(within(legA).getByText('official mirror')).toBeInTheDocument()
    expect(within(legB).getByText('PROXY')).toBeInTheDocument()
    expect(within(legB).getByText('proxy')).toBeInTheDocument()
  })

  it('isolates each Persian name from the surrounding Latin run', async () => {
    await renderPage()
    const fa = screen.getByText('دلار آزاد')
    expect(fa.className).toContain('bidi-fa')
    expect(fa.getAttribute('dir')).toBe('rtl')
  })
})

describe('RelativeValue — selectors', () => {
  it('requests the default pair and window on mount', async () => {
    await renderPage()
    expect(paths()).toContain('/relative-value?a=IR_GOLD_18K&b=USD_IRT&period=1y')
    expect(paths()).toContain('/instruments?enabled=true')
  })

  it('issues a new query when either side changes', async () => {
    await renderPage()
    fireEvent.change(screen.getByLabelText('Asset A'), { target: { value: 'XAUUSD' } })
    await waitFor(() =>
      expect(paths()).toContain('/relative-value?a=XAUUSD&b=USD_IRT&period=1y')
    )
    fireEvent.change(screen.getByLabelText('Asset B'), { target: { value: 'IR_GOLD_18K' } })
    await waitFor(() =>
      expect(paths()).toContain('/relative-value?a=XAUUSD&b=IR_GOLD_18K&period=1y')
    )
  })

  it('issues a new query for the chosen window', async () => {
    await renderPage()
    fireEvent.click(screen.getByRole('button', { name: '5y' }))
    await waitFor(() =>
      expect(paths()).toContain('/relative-value?a=IR_GOLD_18K&b=USD_IRT&period=5y')
    )
  })

  it('draws its options from the instrument registry, grouped by asset class', async () => {
    await renderPage()
    const select = screen.getByLabelText('Asset A') as HTMLSelectElement
    expect(Array.from(select.options).map((o) => o.value)).toEqual([
      'IR_GOLD_18K',
      'USD_IRT',
      'XAUUSD'
    ])
    const groups = Array.from(select.querySelectorAll('optgroup')).map((g) => g.label)
    expect(groups).toEqual(['Gold & coins', 'Currency', 'Global markets'])
  })

  it('offers only what the endpoint can pair, gold first and silver beside it', async () => {
    await renderPage(response(), REGISTRY_0031)
    const select = screen.getByLabelText('Asset A') as HTMLSelectElement

    // Groups in the fixed domain order; rows keep the registry's order inside
    // each group.
    const groups = Array.from(select.querySelectorAll('optgroup')).map((g) => ({
      label: g.label,
      codes: Array.from(g.querySelectorAll('option')).map((o) => o.value)
    }))
    expect(groups).toEqual([
      { label: 'Gold & coins', codes: ['IR_COIN_BAHAR', 'IR_COIN_EMAMI', 'IR_GOLD_18K'] },
      { label: 'Silver', codes: ['IR_SILVER_999'] },
      { label: 'Commodity funds', codes: ['IR_GOLD_FUND_AYAR'] },
      { label: 'Currency', codes: ['USD_IRT'] },
      { label: 'Global markets', codes: ['XAUUSD'] }
    ])
    // An economic series has no daily closes to pair and a rate is a 400.
    const offered = Array.from(select.options).map((o) => o.value)
    for (const code of ['SCI_CPI_URBAN', 'US10Y', 'IR_GOLD_FUND_FLOW']) {
      expect(offered).not.toContain(code)
    }
    // Both pickers carry the same vocabulary.
    const selectB = screen.getByLabelText('Asset B') as HTMLSelectElement
    expect(Array.from(selectB.options).map((o) => o.value)).toEqual(offered)
  })

  it('keeps a domain it has no name for under the raw domain, not out of the list', async () => {
    const registry = {
      items: [...REGISTRY_0031.items, registryRow('IR_SAFFRON_999', 'saffron', 'Saffron')],
      count: 11
    }
    await renderPage(response(), registry)
    const select = screen.getByLabelText('Asset A') as HTMLSelectElement
    const last = Array.from(select.querySelectorAll('optgroup')).pop() as HTMLOptGroupElement
    expect(last.label).toBe('saffron')
    expect(within(last).getByText('Saffron')).toBeInTheDocument()
  })

  it('falls back to the shipped symbol list, ungrouped, when the registry is empty', async () => {
    await renderPage(response(), { items: [], count: 0 })
    const select = screen.getByLabelText('Asset A') as HTMLSelectElement
    expect(select.querySelectorAll('optgroup')).toHaveLength(0)
    expect(Array.from(select.options).map((o) => o.value)).toContain('IR_GOLD_18K')
  })

  it('asks for two different assets instead of comparing a pair with itself', async () => {
    await renderPage()
    fireEvent.change(screen.getByLabelText('Asset B'), { target: { value: 'IR_GOLD_18K' } })
    expect(await screen.findByText('Pick two different assets')).toBeInTheDocument()
    expect(paths()).not.toContain('/relative-value?a=IR_GOLD_18K&b=IR_GOLD_18K&period=1y')
  })
})

describe('RelativeValue — indexed growth', () => {
  it('labels the base the two series are indexed to', async () => {
    await renderPage()
    expect(
      screen.getByText('Indexed growth (both series = 100 at the base date)')
    ).toBeInTheDocument()
    expect(screen.getByText(/Indexed to 100 at/).textContent).toContain('1394/06/18')
  })

  it('explains an empty pair instead of drawing a flat line', async () => {
    await renderPage(response({ series: [] }))
    expect(screen.getByText('No overlapping history for this pair')).toBeInTheDocument()
  })
})

describe('RelativeValue — quick pairs', () => {
  function chips(): HTMLButtonElement[] {
    const group = screen.queryByRole('group', { name: 'Quick pairs' })
    return group ? (within(group).getAllByRole('button') as HTMLButtonElement[]) : []
  }

  it('offers the common pairs once the registry can serve both legs', async () => {
    await renderPage(response(), REGISTRY_0031)
    expect(chips().map((c) => c.textContent)).toEqual([
      '18k gold vs silver 999',
      'Emami vs Bahar Azadi coin',
      'Emami coin vs 18k gold',
      'Silver 999 vs US dollar',
      'Ayar gold fund vs 18k gold'
    ])
  })

  it('sets both legs at once and asks for exactly that pair', async () => {
    await renderPage(response(), REGISTRY_0031)
    const chip = screen.getByRole('button', { name: 'Emami vs Bahar Azadi coin' })
    expect(chip.getAttribute('aria-pressed')).toBe('false')

    fireEvent.click(chip)

    await waitFor(() =>
      expect(paths()).toContain('/relative-value?a=IR_COIN_EMAMI&b=IR_COIN_BAHAR&period=1y')
    )
    expect((screen.getByLabelText('Asset A') as HTMLSelectElement).value).toBe('IR_COIN_EMAMI')
    expect((screen.getByLabelText('Asset B') as HTMLSelectElement).value).toBe('IR_COIN_BAHAR')
    expect(chip.getAttribute('aria-pressed')).toBe('true')
    expect(chip.title).toBe('A: Emami gold coin · B: Bahar Azadi gold coin')
  })

  it('shows no chip for a leg this deployment cannot serve', async () => {
    // The original three-row registry: no silver, no Bahar coin, no fund.
    await renderPage()
    expect(chips()).toEqual([])
    expect(screen.queryByText('Quick pairs')).toBeNull()
  })

  it('labels a pair by its two legs and nothing more', () => {
    const labels = relativeValueSource.match(/label: '[^']+'/g) ?? []
    expect(labels.length).toBe(5)
    for (const label of labels) {
      expect(label).toMatch(/^label: '[^']+ vs [^']+'$/)
    }
  })
})
