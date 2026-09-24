import { beforeEach, describe, expect, it, vi, type Mock } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import Economy, { toSeriesPoints } from '../pages/Economy'
import { SettingsProvider } from '../lib/settings'
import economySource from '../pages/Economy.tsx?raw'
import type {
  EconomicObservation,
  EconomicObservationsResponse,
  EconomicSeriesListResponse
} from '../api/types'

// Captured from production on 2026-09-24. The IMF pair is the reason these are
// captures: IRN carries 46 MEASURED periods (1980–2025) and 6 PROJECTED ones
// (2026–2031), and no hand-written fixture would have got that boundary right.
import seriesList from './fixtures/economy-series-list.json'
import imfWithProjections from './fixtures/economy-imf-irn-with-projections.json'
import imfMeasured from './fixtures/economy-imf-irn-measured.json'
import sciUrban from './fixtures/economy-sci-urban-60.json'

const LIST = seriesList as EconomicSeriesListResponse
const IMF_PROJ = imfWithProjections as EconomicObservationsResponse
const IMF_MEASURED = imfMeasured as EconomicObservationsResponse
const SCI = sciUrban as EconomicObservationsResponse

vi.mock('../api/client', async () => {
  const actual = await vi.importActual<typeof import('../api/client')>('../api/client')
  return { ...actual, api: vi.fn() }
})
const { api } = await import('../api/client')

function mockApi(obs: EconomicObservationsResponse) {
  ;(api as unknown as Mock).mockImplementation((path: string) => {
    if (path.startsWith('/series/')) return Promise.resolve(obs)
    if (path === '/series') return Promise.resolve(LIST)
    return Promise.resolve({})
  })
}

async function renderPage(obs: EconomicObservationsResponse = SCI) {
  mockApi(obs)
  const result = render(
    <SettingsProvider>
      <Economy />
    </SettingsProvider>
  )
  await screen.findByTestId('ec-pit')
  return result
}

beforeEach(() => {
  ;(api as unknown as Mock).mockReset()
})

// ---------------------------------------------------------------------------
// A projection must never read as a measurement. This is the whole page.
// ---------------------------------------------------------------------------

describe('a forecast is not a fact', () => {
  it('splits measured and projected into two separate series', () => {
    const points = toSeriesPoints(IMF_PROJ.observations)
    const measured = IMF_PROJ.observations.filter((o) => !o.is_projection)
    const projected = IMF_PROJ.observations.filter((o) => o.is_projection)
    expect(projected.length).toBe(6)

    // Every projected period is absent from the measured series...
    for (const o of projected) {
      const p = points.find((x) => x.period === o.ref_period_start)
      expect(p?.measured).toBeNull()
      expect(p?.projected).toBe(o.value)
    }
    // ...and every measured period is absent from the projected one, except
    // the single join point where the dashed line picks up.
    const projectedOnMeasuredPeriods = measured.filter(
      (o) => points.find((x) => x.period === o.ref_period_start)?.projected !== null
    )
    expect(projectedOnMeasuredPeriods.length).toBe(1)
  })

  it('joins the dashed line at the last measured value, inventing nothing', () => {
    const points = toSeriesPoints(IMF_PROJ.observations)
    const sorted = [...IMF_PROJ.observations].sort((a, b) =>
      a.ref_period_start.localeCompare(b.ref_period_start)
    )
    const firstProjIdx = sorted.findIndex((o) => o.is_projection)
    const join = sorted[firstProjIdx - 1]
    const point = points.find((p) => p.period === join.ref_period_start)
    // Same value, same period — only the join is drawn.
    expect(point?.projected).toBe(join.value)
    expect(point?.measured).toBe(join.value)
  })

  // A source assertion: one Line with both would present a forecast as history.
  it('draws them with different strokes, never one line', () => {
    expect(economySource).toContain('strokeDasharray="5 4"')
    expect(economySource).toContain('dataKey="projected"')
    expect(economySource).toContain('dataKey="measured"')
  })

  it('withholds projections by default and says how many it is withholding', async () => {
    await renderPage(IMF_MEASURED)
    const note = screen.getByTestId('ec-projection-count')
    // "0 in page" must never read as "there are none".
    expect(note.textContent).toContain('6')
    expect(note.textContent).toMatch(/withheld/i)
    expect(note.textContent).toMatch(/forecast is not a measurement/i)
  })

  it('says plainly when a series has no projections at all', async () => {
    await renderPage(SCI)
    expect(screen.getByTestId('ec-projection-count').textContent).toMatch(
      /carries no projections/i
    )
  })

  it('disables the toggle when there is nothing to project', async () => {
    await renderPage(SCI)
    expect(screen.getByTestId('ec-projections-toggle')).toHaveProperty('disabled', true)
  })
})

describe('toSeriesPoints', () => {
  it('orders oldest first, whatever order the API returned', () => {
    const points = toSeriesPoints(IMF_PROJ.observations)
    for (let i = 1; i < points.length; i++) {
      expect(points[i - 1].period <= points[i].period).toBe(true)
    }
  })

  it('handles a series with no projections without inventing a join', () => {
    const points = toSeriesPoints(SCI.observations)
    expect(points.every((p) => p.projected === null)).toBe(true)
    expect(points.every((p) => p.measured !== null)).toBe(true)
  })

  it('handles an empty series', () => {
    expect(toSeriesPoints([] as EconomicObservation[])).toEqual([])
  })
})

// ---------------------------------------------------------------------------
// Point-in-time and provenance
// ---------------------------------------------------------------------------

describe('the reader is told what kind of number this is', () => {
  it('states the as_of cutoff and what it means', async () => {
    await renderPage(SCI)
    const pit = screen.getByTestId('ec-pit')
    expect(pit.textContent).toMatch(/available/i)
    expect(pit.textContent).toMatch(/never one published afterwards/i)
  })

  it('distinguishes a first-print history from a revised one', async () => {
    await renderPage(SCI)
    expect(screen.getByTestId('ec-pit').textContent).toMatch(/first print/i)

    const revised: EconomicObservationsResponse = { ...SCI, vintages_used: [1, 2, 3] }
    ;(api as unknown as Mock).mockReset()
    mockApi(revised)
    render(
      <SettingsProvider>
        <Economy />
      </SettingsProvider>
    )
    await waitFor(() =>
      expect(
        screen.getAllByTestId('ec-pit').some((n) => /partly revised/i.test(n.textContent ?? ''))
      ).toBe(true)
    )
  })

  it('carries the catalogue caveat onto the page holding the numbers', async () => {
    await renderPage(SCI)
    if (SCI.notes) expect(document.body.textContent).toContain(SCI.notes.slice(0, 40))
    expect(screen.getByText(/Where these numbers come from/i)).toBeTruthy()
  })

  // Two indices on different bases cannot be compared level-for-level, so the
  // page charts one at a time and prints the base beside it.
  it('prints the base period and charts a single series', async () => {
    await renderPage(SCI)
    expect(document.body.textContent).toMatch(/base/i)
    expect(economySource).toMatch(/One series at a time|only one series/i)
  })
})

// The toggle's label said "Hiding measured history only" when projections were
// on, which is not a thing. A button says what clicking it does; the state is
// stated separately.
describe('the projections control says what it does', () => {
  it('offers the action, not a garbled state', async () => {
    await renderPage(IMF_MEASURED)
    const btn = screen.getByTestId('ec-projections-toggle')
    expect(btn.textContent).toMatch(/^Show projections$/)
    expect(screen.getByTestId('ec-projection-state').textContent).toMatch(
      /measured history only/i
    )
  })

  it('states the state separately from the action', async () => {
    await renderPage(IMF_PROJ)
    // The page opens with the toggle off, so the mocked "with projections"
    // payload still renders the off-state label; what matters is that the two
    // elements exist and say different kinds of thing.
    expect(screen.getByTestId('ec-projections-toggle').textContent).toMatch(/projections/i)
    expect(screen.getByTestId('ec-projection-state').textContent).toMatch(/Currently showing/i)
  })
})
