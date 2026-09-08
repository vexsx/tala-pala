import { beforeEach, describe, expect, it, vi, type Mock } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import Markets from '../pages/Markets'
import { SettingsProvider } from '../lib/settings'
import type {
  MarketPerformanceItem,
  MarketPerformanceResponse,
  NumeraireOption,
  NumerairesResponse
} from '../api/types'

// Same hermetic transport stub the other page tests use.
vi.mock('../api/client', () => ({
  api: vi.fn(() => new Promise(() => undefined)),
  errorMessage: (err: unknown) => (err instanceof Error ? err.message : 'Unexpected error')
}))
import { api } from '../api/client'

const apiMock = api as unknown as Mock

// ---------------------------------------------------------------------------
// FIXTURES
//
// Every payload below is what the Go handler actually marshals. Nothing here is
// invented, and that is the point of this block: the previous version of this
// file described a numéraire as {code, label, series_code:'IR_CPI_ANNUAL'} —
// three field names Go has never emitted and a backing series that does not
// exist — so 51 tests passed green against a page that could not load. A
// fixture that invents the contract is worse than no fixture, because it
// manufactures confidence.
//
// HOW TO RE-DERIVE ANY OF IT
//
//   Registry rows (codes, names, tiers, is_proxy, the `notes` string that Go
//   copies in first): database/migrations/0024_instruments_economic_series.up.sql.
//
//   Wire shape: backend-go/internal/relvalue/numeraires.go (numeraireItem,
//   numeraireListResponse, buildNumeraireList) and
//   backend-go/internal/relvalue/performance.go (numeraireBlock,
//   performanceItem, performanceResponse, buildPerformanceItem,
//   buildPerformanceResponse). Dates are dateLayout = "2006-01-02" — days, not
//   timestamps; `as_of`/`from`/`to` on the response are time.Time and DO carry
//   a timestamp.
//
//   Arithmetic: the deployment in backend-go/internal/relvalue/performance_test.go
//   — testInstruments(), testSeries() (annual points at 2024-01-01, 2025-01-01,
//   2026-01-01) and testCPI() (2024=100, 2025=125, vintage 1) — over the window
//   2024-01-01..2026-01-01 with as_of 2026-01-01. Every number below is checked
//   in TestPerformance_NominalAndCrossNumeraireReturns,
//   TestPerformance_VolatilityAndDrawdown,
//   TestPerformance_RealReturnUsesTheCPICoveredSubWindow and
//   TestNumeraireList_MarksTheUnbackedOnesUnavailable.
// ---------------------------------------------------------------------------

/** relvalue/relvalue.go, numeraireSpecs[0].Note — the IRT entry. */
const IRT_NOTE =
  'Toman (IRT) is the canonical stored unit for every Iranian value in this system; rial (IRR) ' +
  'is a display-only x10 and never appears in these figures. An IRT-quoted asset needs no ' +
  "conversion at all. A USD-quoted asset is multiplied by USD_IRT, so its IRT figures inherit " +
  "that series' proxy status."

/** relvalue/relvalue.go, numeraireSpecs[1].Note — the USD entry. */
const USD_NOTE =
  'An IRT-quoted asset is divided by USD_IRT; a USD-quoted asset is already in USD and is ' +
  'passed through untouched rather than multiplied and divided back. USD_IRT is the ' +
  'free-market (USDT/toman) proxy, not an official rate.'

/** relvalue/relvalue.go, numeraireSpecs[2].Note — the GOLD entry. */
const GOLD_NOTE =
  "The asset's value in toman divided by the toman price of one gram of 18k gold. The result " +
  'is a count of 18-karat grams -- not troy ounces and not fine gold. IR_GOLD_18K measured in ' +
  'itself is 1.000 on every day by construction, which is a property of the unit, not a finding.'

/** Registry notes, migration 0024. buildNumeraireBlock appends these after the spec note. */
const USD_IRT_REGISTRY_NOTE =
  'Collected from the 24/7 USDT/toman market (BitMax) as the documented free-market proxy. Its ' +
  'small premium over cash dollars is genuine market information, not error.'
const GOLD_18K_REGISTRY_NOTE = 'Primary source Hamrah Gold; quotes 24/7 every day.'
const XAUUSD_REGISTRY_NOTE =
  'Collected from Yahoo ticker GC=F — COMEX gold futures, used as the XAUUSD proxy ' +
  '(app/providers/yahoo.py). A front-month future carries contango/roll and its own settlement ' +
  "calendar; it is NOT the London spot fix. LBMA's official price is licensed and not freely " +
  'available.'
const KAHRABA_REGISTRY_NOTE = 'Configured in TSETMC_FUNDS; no observations collected yet.'

/**
 * GET /markets/numeraires — relvalue.numeraireListResponse marshalled by
 * buildNumeraireList(testInstruments(), testSeries(), 2026-01-01).
 *
 * The identifier is `key`; the backing instrument is `series` and is null for
 * the identity numéraire; `notes` is an ARRAY; `label_en`/`label_fa` repeat
 * `name_en`/`name_fa` deliberately (numeraires.go says so in a comment) and
 * `default` is published so the client does not compile its own copy of Go's
 * defaultNumeraire.
 */
const NUMERAIRES: NumerairesResponse = {
  as_of: '2026-01-01T00:00:00Z',
  items: [
    {
      key: 'IRT',
      unit: 'toman',
      name_en: 'Iranian toman',
      name_fa: 'تومان',
      series: null,
      quality_tier: null,
      is_proxy: null,
      coverage_from: null,
      coverage_to: null,
      observations: 0,
      notes: [IRT_NOTE],
      label_en: 'Iranian toman',
      label_fa: 'تومان',
      available: true,
      unavailable_reason: null,
      is_identity: true
    },
    {
      key: 'USD',
      unit: 'USD',
      name_en: 'US dollar',
      name_fa: 'دلار',
      series: 'USD_IRT',
      quality_tier: 'proxy',
      is_proxy: true,
      coverage_from: '2024-01-01',
      coverage_to: '2026-01-01',
      observations: 3,
      notes: [USD_NOTE, USD_IRT_REGISTRY_NOTE],
      label_en: 'US dollar',
      label_fa: 'دلار',
      available: true,
      unavailable_reason: null,
      is_identity: false
    },
    {
      key: 'GOLD',
      unit: 'grams of 18k gold',
      name_en: 'Grams of 18k gold',
      name_fa: 'گرم طلای ۱۸ عیار',
      series: 'IR_GOLD_18K',
      quality_tier: 'official_mirror',
      is_proxy: false,
      coverage_from: '2024-01-01',
      coverage_to: '2026-01-01',
      observations: 3,
      notes: [GOLD_NOTE, GOLD_18K_REGISTRY_NOTE],
      label_en: 'Grams of 18k gold',
      label_fa: 'گرم طلای ۱۸ عیار',
      available: true,
      unavailable_reason: null,
      is_identity: false
    }
  ],
  count: 3,
  default: 'IRT',
  warnings: []
}

/**
 * The same endpoint on a deployment that has never collected IR_GOLD_18K —
 * buildNumeraireList with that series absent, exactly the case pinned by
 * TestNumeraireList_MarksTheUnbackedOnesUnavailable. GOLD is LISTED and refused
 * with the reason rather than dropped, because an operator hunting a missing
 * menu entry has to find it.
 */
const NUMERAIRES_GOLD_UNBACKED: NumerairesResponse = {
  ...NUMERAIRES,
  items: [
    NUMERAIRES.items[0],
    NUMERAIRES.items[1],
    {
      ...NUMERAIRES.items[2],
      coverage_from: null,
      coverage_to: null,
      observations: 0,
      available: false,
      unavailable_reason:
        'IR_GOLD_18K carries no stored observation in this deployment, so no value can be ' +
        'converted into GOLD.'
    }
  ]
}

/** The keys the server actually recognises. A request naming anything else is a 400. */
const REGISTRY_KEYS = NUMERAIRES.items.map((n) => n.key)

// --- /markets/performance ---------------------------------------------------

/**
 * One row. Every field relvalue.performanceItem marshals is spelled out at each
 * call site rather than defaulted, so a field Go adds or renames shows up here
 * as a compile error instead of a silent null.
 */
type Row = MarketPerformanceItem

/** numeraire=IRT. Toman-quoted assets pass through untouched. */
const IRT_ROWS: Row[] = [
  {
    code: 'IR_GOLD_FUND_KAHRABA',
    name_en: 'Kahraba gold ETF',
    name_fa: 'صندوق کهربا',
    domain: 'fund',
    quote_currency: 'IRT',
    unit: 'unit',
    quality_tier: 'official_mirror',
    is_proxy: false,
    is_derived: false,
    start_value: null,
    end_value: null,
    nominal_return_pct: null,
    usd_return_pct: null,
    usd_return_from: null,
    usd_return_to: null,
    usd_return_observations: 0,
    gold_return_pct: null,
    gold_return_from: null,
    gold_return_to: null,
    gold_return_observations: 0,
    real_return_pct: null,
    real_return_from: null,
    real_return_to: null,
    observation_volatility_pct: null,
    max_drawdown_pct: null,
    observations: 0,
    coverage_from: null,
    coverage_to: null,
    notes: [
      KAHRABA_REGISTRY_NOTE,
      'This deployment has stored no observation for this instrument, so every figure below is ' +
        'withheld rather than estimated.'
    ]
  },
  {
    // 40 -> 80 toman. Deflated 2024..2025 it is 50/40 against CPI 125/100 —
    // exactly 0.00%, which is a MEASURED zero and must not render as a dash.
    code: 'USD_IRT',
    name_en: 'US dollar, free market',
    name_fa: 'دلار آزاد',
    domain: 'fx',
    quote_currency: 'IRT',
    unit: 'usd',
    quality_tier: 'proxy',
    is_proxy: true,
    is_derived: false,
    start_value: 40,
    end_value: 80,
    nominal_return_pct: 100,
    // USD_IRT is the series that DEFINES the dollar here, so in USD it is 1.000
    // every day: null with a reason, never 0.00%.
    usd_return_pct: null,
    usd_return_from: null,
    usd_return_to: null,
    usd_return_observations: 0,
    gold_return_pct: -50,
    gold_return_from: '2024-01-01',
    gold_return_to: '2026-01-01',
    gold_return_observations: 3,
    real_return_pct: 0,
    real_return_from: '2024-01-01',
    real_return_to: '2025-01-01',
    observation_volatility_pct: 17.455644,
    max_drawdown_pct: 0,
    observations: 3,
    coverage_from: '2024-01-01',
    coverage_to: '2026-01-01',
    notes: [
      USD_IRT_REGISTRY_NOTE,
      'no USD return: USD_IRT is the series that DEFINES this unit, so measured in USD it is ' +
        '1.000 on every day by construction. The cell is null rather than 0.00%, which would be ' +
        'indistinguishable from an asset that went nowhere.'
    ]
  },
  {
    // A dollar-quoted asset in toman: 1*40 = 40 -> 3*80 = 240.
    code: 'XAUUSD',
    name_en: 'Gold, COMEX front month (spot proxy)',
    name_fa: 'انس طلا',
    domain: 'global',
    quote_currency: 'USD',
    unit: 'ozt',
    quality_tier: 'proxy',
    is_proxy: true,
    is_derived: false,
    start_value: 40,
    end_value: 240,
    nominal_return_pct: 500,
    usd_return_pct: 200,
    usd_return_from: '2024-01-01',
    usd_return_to: '2026-01-01',
    usd_return_observations: 3,
    gold_return_pct: 50,
    gold_return_from: '2024-01-01',
    gold_return_to: '2026-01-01',
    gold_return_observations: 3,
    real_return_pct: 100,
    real_return_from: '2024-01-01',
    real_return_to: '2025-01-01',
    observation_volatility_pct: 2.886551,
    max_drawdown_pct: 0,
    observations: 3,
    coverage_from: '2024-01-01',
    coverage_to: '2026-01-01',
    notes: [XAUUSD_REGISTRY_NOTE]
  },
  {
    // 500 -> 2000 toman; in dollars 12.5 -> 25; in its own unit, nothing.
    code: 'IR_GOLD_18K',
    name_en: 'Iranian 18k gold, per gram',
    name_fa: 'طلای ۱۸ عیار',
    domain: 'gold',
    quote_currency: 'IRT',
    unit: 'gram',
    quality_tier: 'official_mirror',
    is_proxy: false,
    is_derived: false,
    start_value: 500,
    end_value: 2000,
    nominal_return_pct: 300,
    usd_return_pct: 100,
    usd_return_from: '2024-01-01',
    usd_return_to: '2026-01-01',
    usd_return_observations: 3,
    gold_return_pct: null,
    gold_return_from: null,
    gold_return_to: null,
    gold_return_observations: 0,
    real_return_pct: 60,
    real_return_from: '2024-01-01',
    real_return_to: '2025-01-01',
    observation_volatility_pct: 0,
    max_drawdown_pct: 0,
    observations: 3,
    coverage_from: '2024-01-01',
    coverage_to: '2026-01-01',
    notes: [
      GOLD_18K_REGISTRY_NOTE,
      'no GOLD return: IR_GOLD_18K is the series that DEFINES this unit, so measured in grams ' +
        'of 18k gold it is 1.000 on every day by construction. The cell is null rather than ' +
        '0.00%, which would be indistinguishable from an asset that went nowhere.'
    ]
  }
]

/** numeraire=USD. Every toman value is divided by USD_IRT before anything else. */
const USD_ROWS: Row[] = [
  {
    ...IRT_ROWS[2],
    // XAUUSD is already in dollars and is passed through: 1 -> 3.
    start_value: 1,
    end_value: 3,
    nominal_return_pct: 200,
    real_return_pct: 60,
    observation_volatility_pct: 20.342194
  },
  {
    ...IRT_ROWS[3],
    // 500/40 = 12.5 -> 2000/80 = 25 dollars.
    start_value: 12.5,
    end_value: 25,
    nominal_return_pct: 100,
    real_return_pct: 28,
    observation_volatility_pct: 17.455644
  }
]

/** numeraire=GOLD. Every value becomes a count of 18k grams. */
const GOLD_ROWS: Row[] = [
  {
    ...IRT_ROWS[2],
    // 1*40/500 = 0.08 -> 3*80/2000 = 0.12 grams.
    start_value: 0.08,
    end_value: 0.12,
    nominal_return_pct: 50,
    real_return_pct: 0,
    observation_volatility_pct: 2.886551
  },
  {
    // Gold measured in gold is 1.000 by construction, so every figure derived
    // from it is null with the reason — not 0.00%.
    ...IRT_ROWS[3],
    start_value: 1,
    end_value: 1,
    nominal_return_pct: null,
    real_return_pct: null,
    real_return_from: null,
    real_return_to: null,
    observation_volatility_pct: null,
    max_drawdown_pct: null
  }
]

/** The numeraire_series block Go echoes on every performance response. */
function numeraireBlock(key: string): NumeraireOption {
  const item = NUMERAIRES.items.find((n) => n.key === key)
  if (!item) throw new Error(`${key} is not in the numéraire vocabulary`)
  const { available, unavailable_reason, is_identity, label_en, label_fa, ...block } = item
  void available
  void unavailable_reason
  void is_identity
  void label_en
  void label_fa
  return block
}

function performance(numeraire: string, period: string): MarketPerformanceResponse {
  const rows = numeraire === 'USD' ? USD_ROWS : numeraire === 'GOLD' ? GOLD_ROWS : IRT_ROWS
  return {
    period,
    numeraire,
    numeraire_series: numeraireBlock(numeraire),
    from: '2024-01-01T00:00:00Z',
    to: '2026-01-01T00:00:00Z',
    cpi_series: 'WB_CPI_IRN',
    cpi_coverage_to: '2025-01-01',
    // Verbatim shape of Go's cpiProvenanceBlock
    // (backend-go/internal/relvalue/performance.go). Do NOT invent fields here:
    // the previous round of this page shipped a blocker precisely because a
    // fixture described a payload the server never emits, and 51 tests passed
    // green against a page that could not load.
    cpi_provenance: {
      code: 'WB_CPI_IRN',
      name_en: 'Iran consumer price index (World Bank, 2010=100)',
      name_fa: 'شاخص قیمت مصرف‌کننده ایران (بانک جهانی)',
      measure: 'index',
      unit: 'index',
      frequency: 'A',
      calendar: 'gregorian',
      base_period: '2010=100',
      provider_code: 'worldbank',
      provider_series_id: 'FP.CPI.TOTL',
      quality_tier: 'official_mirror',
      splice_policy: 'none',
      seasonal_adjustment: 'nsa',
      notes:
        "World Bank rebase to 2010=100. This is NOT the Statistical Centre of Iran's own " +
        '1400=100 index and the two are not comparable level-for-level.',
      coverage_to: '2025-01-01'
    },
    as_of: '2026-01-01T00:00:00Z',
    items: rows,
    count: rows.length,
    warnings: [
      'Excluded as RATES rather than prices: US10Y. They are quoted in percent, so a "return" ' +
        'on them would be a percent change of a percentage -- a meaningless number, not an ' +
        'approximate one.'
    ]
  }
}

// ---------------------------------------------------------------------------
// Transport
// ---------------------------------------------------------------------------

/**
 * The stub refuses a numéraire outside the vocabulary the way Go does — with a
 * 400 — instead of quietly serving a table. A page asking for
 * `numeraire=undefined` or `numeraire=US dollar` must FAIL a test, not pass one.
 */
function mockApi(numeraires: unknown = NUMERAIRES): void {
  apiMock.mockImplementation((path: string) => {
    if (path.startsWith('/markets/numeraires')) return Promise.resolve(numeraires)
    if (path.startsWith('/markets/performance')) {
      const query = new URLSearchParams(path.slice(path.indexOf('?') + 1))
      const key = query.get('numeraire') ?? ''
      if (!REGISTRY_KEYS.includes(key)) {
        return Promise.reject(
          new Error(`numeraire must be one of ${REGISTRY_KEYS.join(', ')}, got "${key}"`)
        )
      }
      return Promise.resolve(performance(key, query.get('period') ?? '1y'))
    }
    return Promise.resolve({})
  })
}

async function renderPage(numeraires: unknown = NUMERAIRES): Promise<void> {
  mockApi(numeraires)
  render(
    <SettingsProvider>
      <Markets />
    </SettingsProvider>
  )
  await screen.findByTestId('mkt-row-IR_GOLD_18K')
}

function row(code: string): HTMLElement {
  return screen.getByTestId(`mkt-row-${code}`) as HTMLElement
}

function cells(code: string): HTMLElement[] {
  return within(row(code)).getAllByRole('cell') as HTMLElement[]
}

/** Column indices, in render order. */
const VALUE = 1
const NOMINAL = 2
const REAL = 3
const USD_COL = 4
const GOLD_COL = 5
const VOL = 6
const DRAWDOWN = 7
const OBS = 8

function rowOrder(): string[] {
  return Array.from(document.querySelectorAll('tbody tr[data-testid]')).map((el) =>
    (el as HTMLElement).dataset.testid!.replace('mkt-row-', '')
  )
}

function paths(): string[] {
  return apiMock.mock.calls.map((c: unknown[]) => String(c[0]))
}

/** Every numéraire this page has actually asked the server for. */
function requestedNumeraires(): string[] {
  return paths()
    .filter((p) => p.startsWith('/markets/performance'))
    .map((p) => new URLSearchParams(p.slice(p.indexOf('?') + 1)).get('numeraire') ?? '')
}

function selector(): HTMLSelectElement {
  return screen.getByLabelText(/Num/) as HTMLSelectElement
}

beforeEach(() => {
  window.localStorage.clear()
  apiMock.mockReset()
})

// ---------------------------------------------------------------------------
// The assertion that would have caught the blocker
// ---------------------------------------------------------------------------

describe('Markets — the page may only ask for a numéraire the server published', () => {
  it('never requests a key that is absent from the registry', async () => {
    await renderPage()
    fireEvent.change(selector(), { target: { value: 'GOLD' } })
    await waitFor(() => expect(requestedNumeraires()).toContain('GOLD'))
    fireEvent.change(selector(), { target: { value: 'USD' } })
    await waitFor(() => expect(requestedNumeraires()).toContain('USD'))

    expect(requestedNumeraires().length).toBeGreaterThan(0)
    for (const key of requestedNumeraires()) {
      expect(REGISTRY_KEYS).toContain(key)
    }
    // The two shapes the old page actually produced.
    expect(requestedNumeraires()).not.toContain('undefined')
    expect(requestedNumeraires()).not.toContain('US dollar')
  })

  it('gives every option an explicit value, so it cannot fall back to its label', async () => {
    await renderPage()
    const options = Array.from(selector().options)
    expect(options.length).toBe(3)
    for (const option of options) {
      expect(REGISTRY_KEYS).toContain(option.value)
      // Without value={n.key} the option value IS the visible text.
      expect(option.value).not.toBe(option.textContent)
    }
    expect(options.map((o) => o.value)).toEqual(['IRT', 'USD', 'GOLD'])
    expect(options.map((o) => o.textContent)).toEqual([
      'Iranian toman',
      'US dollar',
      'Grams of 18k gold'
    ])
  })

  it('selecting the dollar asks for USD, not for the words on the option', async () => {
    await renderPage()
    fireEvent.change(selector(), { target: { value: 'USD' } })
    await waitFor(() =>
      expect(paths()).toContain('/markets/performance?period=1y&numeraire=USD')
    )
    expect(paths().some((p) => p.includes('US%20dollar'))).toBe(false)
  })

  it('requests the default window and numéraire on mount', async () => {
    await renderPage()
    expect(paths()).toContain('/markets/numeraires')
    expect(paths()).toContain('/markets/performance?period=1y&numeraire=IRT')
  })

  it('issues a new query for the chosen period', async () => {
    await renderPage()
    fireEvent.click(screen.getByRole('button', { name: '3m' }))
    await waitFor(() => expect(paths()).toContain('/markets/performance?period=3m&numeraire=IRT'))
  })
})

describe('Markets — the fallback is defensible, never undefined', () => {
  it("takes the server's published default when the current choice is not offered", async () => {
    // A deployment whose vocabulary does not contain the client's starting
    // choice at all: the reconcile must land on `default`, not on undefined.
    await renderPage({
      ...NUMERAIRES,
      items: NUMERAIRES.items.filter((n) => n.key !== 'IRT'),
      default: 'GOLD',
      count: 2
    })
    await waitFor(() => expect(requestedNumeraires()).toContain('GOLD'))
    expect(requestedNumeraires()).not.toContain('undefined')
    expect(selector().value).toBe('GOLD')
  })

  it('falls back to the first AVAILABLE key when there is no usable default', async () => {
    // No `default` at all (a deployment predating that field), and IRT withheld.
    await renderPage({
      as_of: '2026-01-01T00:00:00Z',
      items: [
        { ...NUMERAIRES.items[0], available: false, unavailable_reason: 'withheld here' },
        NUMERAIRES.items[1],
        NUMERAIRES.items[2]
      ],
      count: 3,
      warnings: []
    })
    await waitFor(() => expect(requestedNumeraires()).toContain('USD'))
    expect(requestedNumeraires()).not.toContain('undefined')
    expect(selector().value).toBe('USD')
  })

  it('ignores a default the deployment cannot back', async () => {
    await renderPage({ ...NUMERAIRES_GOLD_UNBACKED, default: 'GOLD' })
    await waitFor(() => expect(requestedNumeraires().length).toBeGreaterThan(0))
    for (const key of requestedNumeraires()) expect(key).not.toBe('GOLD')
  })

  it('keeps one numéraire in effect when the registry does not answer', async () => {
    apiMock.mockImplementation((path: string) => {
      if (path.startsWith('/markets/numeraires')) return Promise.reject(new Error('registry down'))
      if (path.startsWith('/markets/performance')) return Promise.resolve(performance('IRT', '1y'))
      return Promise.resolve({})
    })
    render(
      <SettingsProvider>
        <Markets />
      </SettingsProvider>
    )
    await screen.findByTestId('mkt-row-IR_GOLD_18K')
    expect(await screen.findByText(/The numéraire registry did not answer/)).toBeInTheDocument()
    expect(selector().disabled).toBe(true)
    expect(requestedNumeraires()).toEqual(['IRT'])
  })
})

// ---------------------------------------------------------------------------
// The Value column follows the numéraire, not the instrument
// ---------------------------------------------------------------------------

describe('Markets — the Value column is denominated in the SELECTED numéraire', () => {
  it('shows toman, and names the unit in the heading', async () => {
    await renderPage()
    expect(cells('IR_GOLD_18K')[VALUE].textContent).toBe('2,000')
    expect(screen.getByRole('columnheader', { name: /Value/ }).textContent).toContain('تومان')
  })

  it('shows dollars for numéraire USD — not the instrument’s own quote currency', async () => {
    await renderPage()
    fireEvent.change(selector(), { target: { value: 'USD' } })
    await waitFor(() => expect(cells('IR_GOLD_18K')[VALUE].textContent).toBe('$25.00'))
    // IR_GOLD_18K is quoted in IRT. Formatting by quote_currency printed "25"
    // under a dollar heading and called it toman.
    expect(row('IR_GOLD_18K').textContent).not.toContain('تومان')
    expect(screen.getByRole('columnheader', { name: /Value/ }).textContent).toContain('USD')
    // XAUUSD really is dollar-quoted and reads the same way — the column is
    // uniform because the numéraire, not the row, decides.
    expect(cells('XAUUSD')[VALUE].textContent).toBe('$3.00')
  })

  it('shows grams for numéraire GOLD, keeping the fractions a gram count needs', async () => {
    await renderPage()
    fireEvent.change(selector(), { target: { value: 'GOLD' } })
    await waitFor(() => expect(cells('XAUUSD')[VALUE].textContent).toBe('0.12'))
    // XAUUSD is USD-quoted: the old rule rendered this gram count as "$0.12".
    expect(cells('XAUUSD')[VALUE].textContent).not.toContain('$')
    // Gold in gold is 1.000 by construction — and rounding grams to whole
    // numbers would print 0 for anything under half a gram.
    expect(cells('IR_GOLD_18K')[VALUE].textContent).toBe('1')
    expect(screen.getByRole('columnheader', { name: /Value/ }).textContent).toContain('g 18k')
  })
})

describe('Markets — the rial toggle is a toman conversion and fires only on toman', () => {
  it('multiplies a toman value by ten in the rial view', async () => {
    window.localStorage.setItem('igp_unit', 'IRR')
    await renderPage()
    expect(cells('IR_GOLD_18K')[VALUE].textContent).toBe('20,000')
    expect(screen.getByRole('columnheader', { name: /Value/ }).textContent).toContain('ریال')
  })

  it('leaves a dollar value alone in the rial view', async () => {
    window.localStorage.setItem('igp_unit', 'IRR')
    await renderPage()
    fireEvent.change(selector(), { target: { value: 'USD' } })
    // The bug this pins: ×10 applied to $25 produced "250" — a rial conversion
    // of a number that was never toman.
    await waitFor(() => expect(cells('IR_GOLD_18K')[VALUE].textContent).toBe('$25.00'))
    expect(cells('IR_GOLD_18K')[VALUE].textContent).not.toBe('250')
    expect(screen.getByRole('columnheader', { name: /Value/ }).textContent).not.toContain('ریال')
  })

  it('leaves a gram count alone in the rial view', async () => {
    window.localStorage.setItem('igp_unit', 'IRR')
    await renderPage()
    fireEvent.change(selector(), { target: { value: 'GOLD' } })
    // One gram of gold is one gram of gold in every currency view.
    await waitFor(() => expect(cells('IR_GOLD_18K')[VALUE].textContent).toBe('1'))
    expect(cells('IR_GOLD_18K')[VALUE].textContent).not.toBe('10')
  })
})

// ---------------------------------------------------------------------------
// Nulls
// ---------------------------------------------------------------------------

describe('Markets — a null metric is a dash, never a zero and never blank', () => {
  it('dashes a definitional identity instead of printing 0.00%', async () => {
    await renderPage()
    // IR_GOLD_18K measured in grams of 18k gold is 1.000 every day.
    const cell = cells('IR_GOLD_18K')[GOLD_COL]
    expect(cell.textContent).toBe('—')
    expect(cell.querySelector('[data-absent="true"]')).not.toBeNull()
    expect(within(cell).getByTitle(/1\.000 on every day by construction/)).toBeInTheDocument()
  })

  it('still prints a genuinely measured zero as a number', async () => {
    await renderPage()
    // USD_IRT rose 25% against a CPI that rose 25%: real return exactly 0.
    expect(cells('USD_IRT')[REAL].textContent).toBe('0.00%')
    expect(cells('IR_GOLD_18K')[VOL].textContent).toBe('0.00%')
    expect(cells('IR_GOLD_18K')[DRAWDOWN].textContent).toBe('0.00%')
  })

  it('withholds every figure for an instrument with no stored observation', async () => {
    await renderPage()
    for (const index of [VALUE, NOMINAL, REAL, USD_COL, GOLD_COL, VOL, DRAWDOWN]) {
      const cell = cells('IR_GOLD_FUND_KAHRABA')[index]
      expect(cell.textContent).toBe('—')
      expect(cell.querySelector('[data-absent="true"]')).not.toBeNull()
    }
    // Go marshals `observations` as a plain int: nothing survived, and 0 is the
    // measurement rather than the absence of one.
    expect(cells('IR_GOLD_FUND_KAHRABA')[OBS].textContent).toBe('0')
  })

  it('leaves no cell blank in any row', async () => {
    await renderPage()
    for (const code of ['IR_GOLD_18K', 'USD_IRT', 'XAUUSD', 'IR_GOLD_FUND_KAHRABA']) {
      for (const cell of cells(code)) {
        expect(cell.textContent?.trim()).not.toBe('')
      }
    }
  })

  it('reads the volatility Go actually emits, on either side of its rename', async () => {
    await renderPage()
    expect(cells('USD_IRT')[VOL].textContent).toBe('17.46%')

    // A deployment still emitting the old name must not blank the column.
    apiMock.mockReset()
    window.localStorage.clear()
    apiMock.mockImplementation((path: string) => {
      if (path.startsWith('/markets/numeraires')) return Promise.resolve(NUMERAIRES)
      if (path.startsWith('/markets/performance')) {
        const served = performance('IRT', '1y')
        return Promise.resolve({
          ...served,
          items: served.items.map(({ observation_volatility_pct, ...rest }) => ({
            ...rest,
            daily_volatility_pct: observation_volatility_pct
          }))
        })
      }
      return Promise.resolve({})
    })
    render(
      <SettingsProvider>
        <Markets />
      </SettingsProvider>
    )
    await waitFor(() => expect(screen.getAllByTestId('mkt-row-USD_IRT').length).toBe(2))
    const legacy = screen.getAllByTestId('mkt-row-USD_IRT')[1]
    expect((within(legacy).getAllByRole('cell')[VOL] as HTMLElement).textContent).toBe('17.46%')
  })
})

// ---------------------------------------------------------------------------
// Notes
// ---------------------------------------------------------------------------

describe('Markets — the notes Go sends are shown, not folded away', () => {
  it('renders the selected numéraire’s notes as visible text', async () => {
    await renderPage()
    // An ARRAY, and both entries are on the page — the spec note and the
    // registry's caveat about the backing instrument.
    fireEvent.change(selector(), { target: { value: 'USD' } })
    expect(await screen.findByText(USD_NOTE)).toBeInTheDocument()
    expect(screen.getByText(USD_IRT_REGISTRY_NOTE)).toBeInTheDocument()
    expect(screen.getByText(/is backed by/).textContent).toContain('USD_IRT')
  })

  it('says the hub needs no backing series rather than showing an empty clause', async () => {
    await renderPage()
    expect(screen.getByText(/is the identity conversion and needs no backing series/)).toBeInTheDocument()
    expect(screen.getByText(IRT_NOTE)).toBeInTheDocument()
  })

  it('lists a withheld numéraire with its reason, and refuses to let it be chosen', async () => {
    await renderPage(NUMERAIRES_GOLD_UNBACKED)
    const gold = Array.from(selector().options).find((o) => o.value === 'GOLD')
    expect(gold).toBeDefined()
    expect(gold!.disabled).toBe(true)
    expect(
      screen.getByText(/IR_GOLD_18K carries no stored observation in this deployment/)
    ).toBeInTheDocument()
  })

  it('carries the per-row reason on the dash and in an expandable note row', async () => {
    await renderPage()
    const dash = within(cells('IR_GOLD_FUND_KAHRABA')[VALUE]).getByTitle(/stored no observation/)
    expect(dash.textContent).toBe('—')

    fireEvent.click(
      within(row('IR_GOLD_FUND_KAHRABA')).getByRole('button', { name: 'Why? (2)' })
    )
    expect(await screen.findByText(KAHRABA_REGISTRY_NOTE)).toBeInTheDocument()
  })

  it('surfaces the registry warning about what was left OUT of the table', async () => {
    await renderPage()
    expect(screen.getByText(/Excluded as RATES rather than prices: US10Y/)).toBeInTheDocument()
  })
})

// ---------------------------------------------------------------------------
// Everything the page already got right
// ---------------------------------------------------------------------------

describe('Markets — provenance', () => {
  it('marks a proxy asset and leaves an official mirror unmarked', async () => {
    await renderPage()
    expect(within(row('USD_IRT')).getByText('PROXY')).toBeInTheDocument()
    expect(within(row('USD_IRT')).getByText('proxy')).toBeInTheDocument()
    expect(within(row('IR_GOLD_18K')).queryByText('PROXY')).toBeNull()
    expect(within(row('IR_GOLD_18K')).getByText('official mirror')).toBeInTheDocument()
  })

  it('isolates the Persian name so it cannot reorder the Latin run', async () => {
    await renderPage()
    const fa = within(row('IR_GOLD_18K')).getByText('طلای ۱۸ عیار')
    expect(fa.className).toContain('bidi-fa')
    expect(fa.getAttribute('dir')).toBe('rtl')
  })
})

describe('Markets — sorting', () => {
  it('defaults to real return, highest first, with un-measured rows last', async () => {
    await renderPage()
    expect(rowOrder()).toEqual(['XAUUSD', 'IR_GOLD_18K', 'USD_IRT', 'IR_GOLD_FUND_KAHRABA'])
  })

  it('re-orders on a header click and toggles direction on the second', async () => {
    await renderPage()
    fireEvent.click(screen.getByRole('button', { name: /Nominal/ }))
    expect(rowOrder()).toEqual(['XAUUSD', 'IR_GOLD_18K', 'USD_IRT', 'IR_GOLD_FUND_KAHRABA'])

    fireEvent.click(screen.getByRole('button', { name: /Nominal/ }))
    expect(rowOrder()).toEqual(['USD_IRT', 'IR_GOLD_18K', 'XAUUSD', 'IR_GOLD_FUND_KAHRABA'])
  })

  it('keeps a null out of the ascending ranking instead of sorting it as zero', async () => {
    await renderPage()
    // Ascending real return. A null read as 0 would lead, ahead of the measured
    // 0.00% that USD_IRT genuinely has.
    fireEvent.click(screen.getByRole('button', { name: /Real \(CPI\)/ }))
    expect(rowOrder()).toEqual(['USD_IRT', 'IR_GOLD_18K', 'XAUUSD', 'IR_GOLD_FUND_KAHRABA'])
  })

  it('announces the sorted column to assistive technology', async () => {
    await renderPage()
    fireEvent.click(screen.getByRole('button', { name: /Nominal/ }))
    expect(screen.getByRole('columnheader', { name: /Nominal/ }).getAttribute('aria-sort')).toBe(
      'descending'
    )
    fireEvent.click(screen.getByRole('button', { name: /Nominal/ }))
    expect(screen.getByRole('columnheader', { name: /Nominal/ }).getAttribute('aria-sort')).toBe(
      'ascending'
    )
  })
})

describe('Markets — ranking callouts', () => {
  it('names the metric each ranking is computed on', async () => {
    await renderPage()
    expect(screen.getByText('Best purchasing-power preservation')).toBeInTheDocument()
    expect(screen.getByText(/Ranked on real \(CPI-deflated\) return/)).toBeInTheDocument()
    expect(
      screen.getByText(/Ranked on maximum peak-to-trough drawdown inside the window/)
    ).toBeInTheDocument()
  })

  it('counts how many assets actually carry the ranked metric', async () => {
    await renderPage()
    // Three of four have a real return; the fund has no observation at all.
    expect(screen.getAllByText(/3 of 4 assets have one over this window/).length).toBe(2)
  })

  it('reports a lag in the past tense and promises nothing about the future', async () => {
    await renderPage()
    // Every measured real return here is >= 0, so there is nothing to call a lag.
    expect(
      screen.getByText(/Every measured asset kept pace with CPI over this window/)
    ).toBeInTheDocument()
    expect(document.body.textContent).not.toMatch(/due to catch up|catch up|should rebound/i)
  })

  it('states the lag as a measurement when one exists', async () => {
    mockApi()
    apiMock.mockImplementation((path: string) => {
      if (path.startsWith('/markets/numeraires')) return Promise.resolve(NUMERAIRES)
      if (path.startsWith('/markets/performance')) {
        const served = performance('IRT', '1y')
        return Promise.resolve({
          ...served,
          items: served.items.map((i) =>
            i.code === 'USD_IRT' ? { ...i, real_return_pct: -12.5 } : i
          )
        })
      }
      return Promise.resolve({})
    })
    render(
      <SettingsProvider>
        <Markets />
      </SettingsProvider>
    )
    expect(await screen.findByText(/It has lagged CPI by 12\.50% over this window/)).toBeInTheDocument()
  })

  it('refuses to crown a row when nothing was measured', async () => {
    apiMock.mockImplementation((path: string) => {
      if (path.startsWith('/markets/numeraires')) return Promise.resolve(NUMERAIRES)
      if (path.startsWith('/markets/performance')) {
        const served = performance('IRT', '1y')
        return Promise.resolve({ ...served, items: [IRT_ROWS[0]], count: 1 })
      }
      return Promise.resolve({})
    })
    render(
      <SettingsProvider>
        <Markets />
      </SettingsProvider>
    )
    await screen.findByTestId('mkt-row-IR_GOLD_FUND_KAHRABA')
    expect(
      screen.getAllByText(
        'No asset has a CPI-deflated return over this window, so nothing is ranked on it.'
      ).length
    ).toBe(2)
  })
})

describe('Markets — CPI provenance', () => {
  it('names the deflator and the date its coverage stops', async () => {
    await renderPage()
    const line = screen.getByText(/Real returns are deflated by/)
    expect(line.textContent).toContain('WB_CPI_IRN')
    expect(line.textContent).toContain('whose coverage ends')
  })

  it('says plainly when no CPI series is configured', async () => {
    apiMock.mockImplementation((path: string) => {
      if (path.startsWith('/markets/numeraires')) return Promise.resolve(NUMERAIRES)
      if (path.startsWith('/markets/performance')) {
        return Promise.resolve({
          ...performance('IRT', '1y'),
          cpi_series: null,
          cpi_coverage_to: null
        })
      }
      return Promise.resolve({})
    })
    render(
      <SettingsProvider>
        <Markets />
      </SettingsProvider>
    )
    expect(
      await screen.findByText(
        'No CPI series is configured for this deployment, so real returns are not computed.'
      )
    ).toBeInTheDocument()
  })
})
