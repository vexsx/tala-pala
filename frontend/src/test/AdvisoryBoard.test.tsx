import { beforeEach, describe, expect, it, vi, type Mock } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { SettingsProvider } from '../lib/settings'
import { confidencePct, formatPct } from '../lib/format'
import type {
  OmittedFactor,
  SignalLevel,
  SignalOverviewItem,
  SignalsOverviewResponse
} from '../api/types'

// ---------------------------------------------------------------------------
// THE FIXTURES ARE THE CONTRACT — AND ONLY THE FIXTURES.
//
// Both JSON files were produced by the Go side marshalling its own structs
// (backend-go/internal/signalsvc/overview.go and handlers.go), so they are the
// bytes the browser actually receives. They are imported, never retyped: an
// earlier round of this work asserted against a hand-written payload that had
// invented the server shape, and 51 tests passed green over a page that could
// not load.
//
// The rule this file now follows, which the previous round did not: EVERY
// expected value is read out of the fixture, and rows are selected by the
// PROPERTY under test rather than by symbol name. Nothing below hardcodes a
// percentage, a confidence, a factor key, a reason sentence or a per-symbol
// count.
//
// That is not tidiness. The last version of this file asserted a 0.49% gold
// spread beside a 2.2% dollar hurdle, a 2.4% coin hurdle and a 0.35% XAUUSD
// hurdle — six per-market figures that prediction-python/app/core/costs.py
// cannot produce. It resolves exactly one observed spread (SPREAD_SOURCES has
// a single entry, IR_GOLD_18K) and every other asset falls back to the SAME
// structural FALLBACK_ROUND_TRIP_COST_PCT with its own reason attached. Tests
// that encode invented variety are testing a page that cannot exist, so the
// uniform case is now covered explicitly, below, under "the production shape".
//
// The typed consts are the second half of the guard. Each fixture is assigned
// to the TypeScript type the component consumes, with no `as` in the way, so a
// field this frontend declares but the server does not send — or sends with
// another type — fails `npm run typecheck` rather than surfacing as a blank
// card in production.
// ---------------------------------------------------------------------------
import overviewFixture from './fixtures/signals-overview.json'
import currentFixture from './fixtures/signals-current.json'

const OVERVIEW: SignalsOverviewResponse = overviewFixture
const ITEMS: SignalOverviewItem[] = OVERVIEW.items

/**
 * /signals/current, the FROZEN shape (signalRow in handlers.go). Declared here
 * rather than imported from api/types.ts because the live payload omits the
 * `created_at` that the legacy `Signal` interface still carries; this mirrors
 * the Go struct field for field.
 */
interface CurrentSignalPayload {
  id: number
  generated_at: string
  signal: SignalLevel | string
  score: number
  confidence: number
  explanation: string
  supporting: string[]
  conflicting: string[]
  risks: string[]
  invalidation: string
  review_at: string | null
  data_fresh: boolean
  inputs: {
    evidence_basis: string
    omitted_factors: OmittedFactor[]
    round_trip_cost_pct: number
    round_trip_cost_basis: string
    round_trip_cost_source: string | null
    round_trip_cost_reason: string
    stale_reason: string | null
    [key: string]: unknown
  }
}

const CURRENT: CurrentSignalPayload = currentFixture

vi.mock('../api/client', () => ({
  api: vi.fn(),
  errorMessage: (err: unknown) => (err instanceof Error ? err.message : 'Unexpected error')
}))
import { api } from '../api/client'
import AdvisoryBoard, { MARK_SIZES, withoutUnsupportedModelClaim } from '../components/AdvisoryBoard'
import Overview from '../pages/Overview'

const apiMock = api as unknown as Mock

/** Deep copy so a per-test mutation cannot leak into the shared fixture. */
function clone(payload: SignalsOverviewResponse): SignalsOverviewResponse {
  return JSON.parse(JSON.stringify(payload)) as SignalsOverviewResponse
}

function item(symbol: string): SignalOverviewItem {
  const found = ITEMS.find((i) => i.symbol === symbol)
  if (!found) throw new Error(`fixture has no item for ${symbol}`)
  return found
}

/** The first fixture row matching a property, or a failure naming what was wanted. */
function pick(what: string, pred: (i: SignalOverviewItem) => boolean): SignalOverviewItem {
  const found = ITEMS.find(pred)
  if (!found) throw new Error(`the fixture carries no row that is ${what}`)
  return found
}

const OBSERVED = pick('costed from an observed spread', (i) => i.cost_basis?.basis === 'observed_spread')
const ASSUMED = ITEMS.filter((i) => i.cost_basis?.basis === 'assumed')
const MODEL_BACKED = ITEMS.filter((i) => i.evidence_basis === 'model_backed')
const TECHNICAL = ITEMS.filter((i) => i.evidence_basis === 'technical_only')
/** The row that lost the most factors, whichever it is. */
const MOST_OMITTED = [...ITEMS].sort(
  (a, b) => b.omitted_factors.length - a.omitted_factors.length
)[0]

/** The rendered form of a fixture value, so no expectation restates a number. */
const hurdleText = (i: SignalOverviewItem): string => formatPct(i.cost_pct, { sign: false })
const confidenceText = (i: SignalOverviewItem): string =>
  `${Math.round(confidencePct(i.confidence) ?? 0)}%`

function serve(payload: SignalsOverviewResponse | null, err?: Error): void {
  apiMock.mockImplementation((path: string) => {
    if (path.startsWith('/signals/overview')) {
      if (err) return Promise.reject(err)
      if (payload === null) return new Promise(() => undefined)
      return Promise.resolve(payload)
    }
    if (path.startsWith('/signals/current')) return Promise.resolve(CURRENT)
    return Promise.resolve({})
  })
}

function renderBoard(payload: SignalsOverviewResponse = OVERVIEW) {
  serve(payload)
  return render(
    <SettingsProvider>
      <AdvisoryBoard />
    </SettingsProvider>
  )
}

/** The rendered card for one symbol. */
async function row(symbol: string): Promise<HTMLElement> {
  return await screen.findByTestId(`advisory-${symbol}`)
}

const costOf = (card: HTMLElement): HTMLElement =>
  card.querySelector('.advisory-cost') as HTMLElement

beforeEach(() => {
  window.localStorage.clear()
  apiMock.mockReset()
})

// ---------------------------------------------------------------------------

describe('The fixture itself can only say what the backend can produce', () => {
  it('resolves exactly one observed spread, for the one symbol with a dealer feed', () => {
    // costs.py SPREAD_SOURCES = {"IR_GOLD_18K": "hamrahgold"} — one entry, and
    // an asset absent from it never even queries the database.
    const observed = ITEMS.filter((i) => i.cost_basis?.basis === 'observed_spread')
    expect(observed.map((i) => i.symbol)).toEqual(['IR_GOLD_18K'])
    expect(observed[0].cost_basis?.source).toBeTruthy()
  })

  it('never lends the observed spread to an asset that was assumed', () => {
    // The one thing costs.py refuses "under any circumstance": reusing gold's
    // measurement for a market it was not measured in.
    for (const i of ASSUMED) {
      expect(i.cost_pct).not.toBe(OBSERVED.cost_pct)
      expect(i.cost_basis?.source).toBeNull()
      expect(i.cost_basis?.reason?.trim()).toBeTruthy()
    }
  })

  it('gives every assumed hurdle the SAME figure, because costs.py has only one', () => {
    // _assumed() returns FALLBACK_ROUND_TRIP_COST_PCT for every asset without a
    // dealer quote — one structural fee-plus-spread-plus-slippage number, with
    // a per-asset REASON attached. A fixture carrying a plausible-looking
    // figure per market is the exact failure the module docstring names: "once
    // it is in the payload it is indistinguishable from the 0.49% that WAS
    // measured".
    expect(ASSUMED.length).toBeGreaterThan(1)
    expect(new Set(ASSUMED.map((i) => i.cost_pct)).size).toBe(1)
  })

  it('gives every fresh technical-only reading the same confidence', () => {
    // compute_signal has no per-horizon confidences to average without a
    // model, so it sets mean_conf = 0.3 outright and publishes
    // clip(mean_conf * (data_fresh ? 1 : 0.3)). Both halves are constants: a
    // technical-only row cannot carry a model-shaped confidence, and a stale
    // one must sit strictly below a fresh one.
    const fresh = TECHNICAL.filter((i) => i.data_fresh)
    expect(fresh.length).toBeGreaterThan(1)
    expect(new Set(fresh.map((i) => i.confidence)).size).toBe(1)
    for (const i of TECHNICAL.filter((i) => !i.data_fresh)) {
      expect(i.confidence).toBeLessThan(fresh[0].confidence)
    }
  })

  it('claims model backing only for symbols a model is trained for', () => {
    // universe.py reads app.models.training.FORECAST_SYMBOLS rather than
    // restating it; both it and overview.go record the same two symbols.
    for (const i of MODEL_BACKED) {
      expect(['IR_GOLD_18K', 'XAUUSD']).toContain(i.symbol)
    }
    expect(MODEL_BACKED.length).toBeGreaterThan(0)
    expect(TECHNICAL.length).toBeGreaterThan(0)
  })

  it('gives every omitted factor a real reason', () => {
    for (const i of ITEMS) {
      for (const f of i.omitted_factors) {
        expect(f.factor).not.toBe('')
        expect(f.reason.trim()).not.toBe('')
      }
    }
  })
})

describe('Advisory board — the evidence basis is the most prominent mark', () => {
  it('renders the evidence badge before the buy/sell word on every row', async () => {
    renderBoard()
    await row(ITEMS[0].symbol)
    for (const i of ITEMS) {
      const card = await row(i.symbol)
      const evidence = card.querySelector('[data-evidence]') as HTMLElement
      const call = within(card).getByTestId('signal-badge')
      expect(evidence).not.toBeNull()
      // Document order is also the order a screen reader announces them in.
      expect(
        evidence.compareDocumentPosition(call) & Node.DOCUMENT_POSITION_FOLLOWING
      ).toBeTruthy()
    }
  })

  it('puts the evidence badge in the head row and demotes the imperative out of it', async () => {
    renderBoard()
    const card = await row(TECHNICAL[0].symbol)
    const head = card.querySelector('.advisory-item-head') as HTMLElement
    expect(head.querySelector('[data-evidence]')).not.toBeNull()
    expect(within(head).queryByTestId('signal-badge')).toBeNull()
  })

  it('sets the evidence badge larger and heavier than the signal badge', async () => {
    // The defect this replaces: SignalBadge shipped 13px/700 in the head row
    // while TECHNICAL-ONLY sat at 11px/600 below it, so the page's loudest
    // mark was the imperative on rows with no model behind them.
    expect(MARK_SIZES.evidencePx).toBeGreaterThan(MARK_SIZES.callPx)
    expect(MARK_SIZES.evidenceWeight).toBeGreaterThan(MARK_SIZES.callWeight)
    // And it is not merely bigger than the chip this card demotes: it clears
    // what `.signal-badge` renders at everywhere else on the site, which is
    // the size that caused the defect.
    expect(MARK_SIZES.evidencePx).toBeGreaterThan(MARK_SIZES.globalSignalBadgePx)

    // The stylesheet reads those numbers off the card root, so the two halves
    // cannot drift: styles.css carries var(--advisory-evidence-size) and this
    // is where the value comes from.
    renderBoard()
    await row(ITEMS[0].symbol)
    const board = document.querySelector('.advisory-board') as HTMLElement
    expect(board.style.getPropertyValue('--advisory-evidence-size')).toBe(
      `${MARK_SIZES.evidencePx}px`
    )
    expect(board.style.getPropertyValue('--advisory-call-size')).toBe(`${MARK_SIZES.callPx}px`)
    expect(board.style.getPropertyValue('--advisory-evidence-weight')).toBe(
      String(MARK_SIZES.evidenceWeight)
    )
    expect(board.style.getPropertyValue('--advisory-call-weight')).toBe(
      String(MARK_SIZES.callWeight)
    )
  })

  it('labels the imperative so "Buy" is a reading, not an instruction', async () => {
    renderBoard()
    const card = await row(TECHNICAL[0].symbol)
    const call = card.querySelector('.advisory-call') as HTMLElement
    expect(within(call).getByText('Reading')).toBeInTheDocument()
    expect(within(call).getByTestId('signal-badge')).toBeInTheDocument()
  })

  it('says in the preamble that the evidence outranks the call, because it does', async () => {
    renderBoard()
    await row(ITEMS[0].symbol)
    expect(
      screen.getByText(/set larger than the buy\/sell word/i)
    ).toBeInTheDocument()
  })
})

describe('Advisory board — model-backed versus technical-only', () => {
  it('marks a technical-only row TECHNICAL-ONLY and never model-backed', async () => {
    const target = TECHNICAL[0]
    renderBoard()
    const card = await row(target.symbol)

    expect(within(card).getByText('TECHNICAL-ONLY')).toBeInTheDocument()
    expect(within(card).queryByText('MODEL-BACKED')).toBeNull()
    expect(within(card).getByText('TECHNICAL-ONLY')).toHaveAttribute(
      'data-evidence',
      'technical_only'
    )
    // The word is not the whole story: the row says what technical-only means.
    // Asserted on the note itself — the scorer's own headline for these assets
    // often uses the same phrase, and a loose text query would pass on that
    // coincidence alone.
    const note = card.querySelector('.advisory-evidence-note')
    expect(note?.textContent).toMatch(/No trained model exists for this asset/)
    expect(note?.textContent).toMatch(/carries no model evidence/i)
  })

  it('marks a model-backed row MODEL-BACKED and never technical-only', async () => {
    const target = MODEL_BACKED[0]
    renderBoard()
    const card = await row(target.symbol)

    expect(within(card).getByText('MODEL-BACKED')).toBeInTheDocument()
    expect(within(card).queryByText('TECHNICAL-ONLY')).toBeNull()
    const note = card.querySelector('.advisory-evidence-note')
    expect(note?.textContent).toMatch(/A trained model forecast was weighed into this reading/)
    expect(note?.textContent).not.toMatch(/no model evidence/i)
  })

  it('gives every asset in the payload a row, and each row an explicit basis', async () => {
    renderBoard()
    await row(ITEMS[0].symbol)
    for (const i of ITEMS) {
      const card = await row(i.symbol)
      const badge = card.querySelector('[data-evidence]')
      expect(badge?.getAttribute('data-evidence')).toBe(i.evidence_basis)
      expect(badge?.textContent?.trim()).not.toBe('')
      expect(card.querySelector('.advisory-evidence-note')?.textContent?.trim()).not.toBe('')
    }
    expect(screen.getAllByTestId(/^advisory-/)).toHaveLength(ITEMS.length)
  })

  it('counts the model-backed readings out of the whole board', async () => {
    renderBoard()
    const counts = await screen.findByText(/readings are model-backed/)
    expect(counts.textContent).toContain(`${MODEL_BACKED.length} of ${ITEMS.length}`)
    expect(counts.textContent).toContain(`${TECHNICAL.length} `)
    for (const i of MODEL_BACKED) expect(counts.textContent).toContain(i.symbol)
  })

  it('shows both names, with the Persian one bidi-isolated', async () => {
    const target = ITEMS[0]
    renderBoard()
    const card = await row(target.symbol)
    expect(within(card).getByText(target.name_en)).toBeInTheDocument()
    const fa = within(card).getByText(target.name_fa)
    expect(fa.className).toContain('bidi-fa')
    expect(fa).toHaveAttribute('dir', 'rtl')
    expect(fa).toHaveAttribute('lang', 'fa')
  })
})

// ---------------------------------------------------------------------------
// The scorer's own boilerplate, quoted from
// prediction-python/app/signals/engine.py because it is the thing under test.
// compute_signal() appends the closing sentence to EVERY explanation, whether
// or not a forecast existed, and Go passes `explanation` through verbatim.
const ENGINE_EXPLANATION =
  'Conditions currently favor buying (score 62/100). Weighted forecast move: +0.31%. ' +
  '2 supporting vs 1 conflicting factors. This is an uncertain, model-based assessment of ' +
  'current conditions — not financial advice, and actual outcomes can differ.'

const ENGINE_STALE_EXPLANATION =
  'Input data is stale, so the engine holds regardless of model output. Conditions will be ' +
  'reassessed when fresh data arrives. This is an uncertain, model-based assessment — not ' +
  'financial advice.'

describe('Advisory board — no row displays a model claim it does not have', () => {
  it('drops the model-based sentence from a technical-only headline', () => {
    const { text, removed } = withoutUnsupportedModelClaim(ENGINE_EXPLANATION, 'technical_only')
    expect(removed).toBe(true)
    expect(text).not.toMatch(/model-based/i)
    // Everything the engine actually measured survives, to the character.
    expect(text).toContain('Conditions currently favor buying (score 62/100).')
    expect(text).toContain('Weighted forecast move: +0.31%.')
    expect(text).toContain('2 supporting vs 1 conflicting factors.')
  })

  it('drops it from the stale variant too, and from an unknown basis', () => {
    expect(withoutUnsupportedModelClaim(ENGINE_STALE_EXPLANATION, 'technical_only').text)
      .not.toMatch(/model-based/i)
    // `unknown` means Go could not read the stored inputs. A model claim is
    // not supported by "we do not know" any more than by "there is no model".
    expect(withoutUnsupportedModelClaim(ENGINE_EXPLANATION, 'unknown').removed).toBe(true)
  })

  it('leaves a model-backed reading\'s wording exactly as the engine wrote it', () => {
    const { text, removed } = withoutUnsupportedModelClaim(ENGINE_EXPLANATION, 'model_backed')
    expect(removed).toBe(false)
    expect(text).toBe(ENGINE_EXPLANATION)
  })

  it('keeps a DENIAL of model evidence, which is the opposite of a claim', () => {
    const denial =
      'No model forecast is available for any horizon, so this reading is technical-only ' +
      'and carries no model evidence.'
    expect(withoutUnsupportedModelClaim(denial, 'technical_only')).toEqual({
      text: denial,
      removed: false
    })
  })

  it('renders none of it on the page, and discloses that it edited the wording', async () => {
    const payload = clone(OVERVIEW)
    const target = payload.items.find((i) => i.evidence_basis === 'technical_only')!
    target.headline = ENGINE_EXPLANATION

    renderBoard(payload)
    const card = await row(target.symbol)
    expect(card.textContent).not.toMatch(/model-based/i)
    expect(within(card).getByText(/Conditions currently favor buying/)).toBeInTheDocument()
    // Not a silent rewrite: the reader is told the sentence was removed.
    const note = card.querySelector('[data-claim-removed]')
    expect(note?.textContent).toMatch(/that sentence was removed rather than reprinted/i)
    // The disclosure must not reprint the claim it removed.
    expect(note?.textContent).not.toMatch(/model-based/i)
  })

  it('passes a model-backed row through untouched, note and all', async () => {
    const payload = clone(OVERVIEW)
    const target = payload.items.find((i) => i.evidence_basis === 'model_backed')!
    target.headline = ENGINE_EXPLANATION

    renderBoard(payload)
    const card = await row(target.symbol)
    expect(within(card).getByText(ENGINE_EXPLANATION)).toBeInTheDocument()
    expect(card.querySelector('[data-claim-removed]')).toBeNull()
  })

  it('says so rather than showing a gap when the claim was the whole headline', async () => {
    const payload = clone(OVERVIEW)
    const target = payload.items.find((i) => i.evidence_basis === 'technical_only')!
    target.headline = 'This is an uncertain, model-based assessment — not financial advice.'

    renderBoard(payload)
    const card = await row(target.symbol)
    expect(card.querySelector('.advisory-headline')).toBeNull()
    expect(
      within(card).getByText(/recorded no one-line explanation this row can stand behind/i)
    ).toBeInTheDocument()
  })

  it('renders the fixture headlines as the server sent them', async () => {
    // Nothing in the current payload trips the filter, and the page must not
    // edit prose it has no reason to edit.
    renderBoard()
    for (const i of ITEMS) {
      const card = await row(i.symbol)
      if (i.headline.trim() === '') continue
      expect(within(card).getByText(i.headline)).toBeInTheDocument()
      expect(card.querySelector('[data-claim-removed]')).toBeNull()
    }
  })
})

describe('Advisory board — omitted factors', () => {
  it('lists what was not weighed, collapsed but present in the document', async () => {
    const target = MOST_OMITTED
    expect(target.omitted_factors.length).toBeGreaterThan(0)

    renderBoard()
    const card = await row(target.symbol)
    const details = card.querySelector('details')
    expect(details).not.toBeNull()
    // Collapsed by default — the score stays readable.
    expect(details).not.toHaveAttribute('open')
    expect(
      within(card).getByText(
        new RegExp(`Factors not weighed \\(${target.omitted_factors.length}\\)`)
      )
    ).toBeInTheDocument()

    // Reachable, not hidden: every factor and its reason are in the DOM behind
    // the disclosure, so nothing is dropped on the way to the page.
    for (const f of target.omitted_factors) {
      expect(within(card).getByText(f.factor)).toBeInTheDocument()
      expect(within(card).getByText(f.reason)).toBeInTheDocument()
    }
  })

  it('opens on a click, revealing the reasons', async () => {
    const target = MOST_OMITTED
    renderBoard()
    const card = await row(target.symbol)
    const details = card.querySelector('details') as HTMLDetailsElement
    const summary = within(card).getByText(/Factors not weighed/)

    const reason = target.omitted_factors[0].reason
    expect(within(card).getByText(reason)).not.toBeVisible()
    fireEvent.click(summary)
    expect(details.open).toBe(true)
    expect(within(card).getByText(reason)).toBeVisible()
  })

  it('says so explicitly when nothing was omitted, rather than showing nothing', async () => {
    const payload = clone(OVERVIEW)
    const target = payload.items[0]
    target.omitted_factors = []

    renderBoard(payload)
    const card = await row(target.symbol)
    expect(card.querySelector('details')).toBeNull()
    expect(
      within(card).getByText(/Every factor this asset can carry was computed/i)
    ).toBeInTheDocument()
  })

  it('counts each row\'s omissions from that row\'s own list', async () => {
    renderBoard()
    for (const i of ITEMS) {
      const card = await row(i.symbol)
      const n = i.omitted_factors.length
      if (n === 0) {
        expect(card.querySelector('.advisory-omitted-none')).not.toBeNull()
        continue
      }
      expect(card.querySelector('.advisory-omitted-list')?.children).toHaveLength(n)
    }
  })
})

describe('Advisory board — the cost hurdle and its basis', () => {
  it('shows the observed spread with its source and figure', async () => {
    renderBoard()
    const card = await row(OBSERVED.symbol)
    const cost = costOf(card)

    expect(cost).toHaveAttribute('data-cost-basis', 'observed_spread')
    expect(within(cost).getByText('OBSERVED SPREAD')).toBeInTheDocument()
    expect(within(cost).queryByText('ASSUMPTION')).toBeNull()
    expect(within(cost).getByText(OBSERVED.cost_basis!.source!)).toBeInTheDocument()
    expect(within(cost).getByText(hurdleText(OBSERVED))).toBeInTheDocument()
  })

  it('shows an assumed hurdle as an ASSUMPTION, visibly unlike an observed one', async () => {
    const target = ASSUMED[0]
    renderBoard()
    const cost = costOf(await row(target.symbol))

    expect(cost).toHaveAttribute('data-cost-basis', 'assumed')
    expect(within(cost).getByText('ASSUMPTION')).toBeInTheDocument()
    expect(within(cost).queryByText('OBSERVED SPREAD')).toBeNull()
    // No dealer is credited for a number nobody quoted.
    expect(within(cost).queryByText(OBSERVED.cost_basis!.source!)).toBeNull()
    // ...and the generator's own explanation travels with it, verbatim.
    expect(within(cost).getByText(target.cost_basis!.reason!)).toBeInTheDocument()
  })

  it('distinguishes the two bases by more than wording alone', async () => {
    renderBoard()
    const observed = costOf(await row(OBSERVED.symbol))
    const assumed = costOf(await row(ASSUMED[0].symbol))

    expect(observed.getAttribute('data-cost-basis')).not.toBe(
      assumed.getAttribute('data-cost-basis')
    )
    const badgeClass = (el: HTMLElement) => el.querySelector('.badge')?.className ?? ''
    expect(badgeClass(observed)).toContain('badge-ok')
    expect(badgeClass(assumed)).toContain('badge-warn')
  })

  it('calls a model-backed asset\'s hurdle assumed when that is what it is', async () => {
    // Model evidence and cost provenance are independent claims; conflating
    // them would let a trained model lend authority to a guessed spread.
    const target = MODEL_BACKED.find((i) => i.cost_basis?.basis === 'assumed')
    if (!target) return
    renderBoard()
    const card = await row(target.symbol)
    expect(within(card).getByText('MODEL-BACKED')).toBeInTheDocument()
    expect(costOf(card)).toHaveAttribute('data-cost-basis', 'assumed')
    expect(within(card).getByText('ASSUMPTION')).toBeInTheDocument()
  })

  it('renders each row\'s hurdle from that row\'s own figure', async () => {
    renderBoard()
    for (const i of ITEMS) {
      const cost = costOf(await row(i.symbol))
      expect(within(cost).getByText(hurdleText(i))).toBeInTheDocument()
    }
  })
})

describe('Advisory board — the production shape: one figure, repeated', () => {
  /**
   * The fixture, reshaped into what the backend can actually emit. costs.py
   * resolves ONE observed spread and hands every other asset the same
   * FALLBACK_ROUND_TRIP_COST_PCT; engine.py hands every technical-only reading
   * the same mean_conf = 0.3. So in production six of seven rows carry an
   * identical hurdle and an identical confidence, and the board has to stay
   * readable — and still tell the rows apart — when they do.
   *
   * The figures are taken from the fixture rather than written down here, so
   * this stays true whatever the fallback constant is.
   */
  function productionShaped(): {
    payload: SignalsOverviewResponse
    hurdle: number
    confidence: number
  } {
    const payload = clone(OVERVIEW)
    const assumed = payload.items.filter((i) => i.cost_basis?.basis === 'assumed')
    const hurdle = assumed[0].cost_pct as number
    for (const i of assumed) i.cost_pct = hurdle

    const technical = payload.items.filter((i) => i.evidence_basis === 'technical_only')
    const confidence = technical[0].confidence
    for (const i of technical) i.confidence = confidence

    return { payload, hurdle, confidence }
  }

  it('renders the same hurdle on every assumed row without collapsing them', async () => {
    const { payload, hurdle } = productionShaped()
    const assumed = payload.items.filter((i) => i.cost_basis?.basis === 'assumed')
    expect(assumed.length).toBeGreaterThan(1)

    renderBoard(payload)
    for (const i of assumed) {
      const cost = costOf(await row(i.symbol))
      expect(within(cost).getByText(formatPct(hurdle, { sign: false }))).toBeInTheDocument()
      expect(within(cost).getByText('ASSUMPTION')).toBeInTheDocument()
      // Identical numbers, different reasons: the rows are still distinguishable
      // by the thing that actually differs between them.
      expect(within(cost).getByText(i.cost_basis!.reason!)).toBeInTheDocument()
    }
    // Not asserted: that the reasons are all DIFFERENT. costs.py gives the two
    // TSE fund units the same sentence, because the same thing is true of
    // both — the point is that each row carries the reason for ITS asset, not
    // that six distinct sentences exist.
  })

  it('says the repeated figure is one assumption, not one quote per market', async () => {
    const { payload, hurdle } = productionShaped()
    const assumed = payload.items.filter((i) => i.cost_basis?.basis === 'assumed')

    renderBoard(payload)
    await row(payload.items[0].symbol)
    const note = document.querySelector('.advisory-cost-summary') as HTMLElement
    expect(note.textContent).toContain(`not ${assumed.length} per-market quotes`)
    expect(note.textContent).toContain(formatPct(hurdle, { sign: false }))
    expect(note.textContent).toContain(OBSERVED.symbol)
  })

  it('claims no such uniformity when the hurdles actually differ', async () => {
    const payload = clone(OVERVIEW)
    const assumed = payload.items.filter((i) => i.cost_basis?.basis === 'assumed')
    assumed[0].cost_pct = (assumed[0].cost_pct as number) + 1.5

    renderBoard(payload)
    await row(payload.items[0].symbol)
    const note = document.querySelector('.advisory-cost-summary') as HTMLElement
    expect(note.textContent).not.toMatch(/one and the same conservative assumption/)
    expect(note.textContent).toMatch(/assumed rather than quoted/)
  })

  it('renders the same confidence on every technical-only row', async () => {
    const { payload, confidence } = productionShaped()
    const technical = payload.items.filter((i) => i.evidence_basis === 'technical_only')
    const expected = `${Math.round(confidencePct(confidence) ?? 0)}%`

    renderBoard(payload)
    for (const i of technical) {
      const card = await row(i.symbol)
      expect(within(card).getByText(expected)).toBeInTheDocument()
    }
  })

  it('renders each fixture row\'s confidence from that row\'s own number', async () => {
    renderBoard()
    for (const i of ITEMS) {
      const card = await row(i.symbol)
      const scores = card.querySelector('.advisory-scores') as HTMLElement
      expect(within(scores).getByText(confidenceText(i))).toBeInTheDocument()
      expect(within(scores).getByText(`${i.score} / 100`)).toBeInTheDocument()
    }
  })
})

describe('Advisory board — a null metric is a dash, never a zero', () => {
  it('renders an absent cost hurdle as a dash and prints no percentage', async () => {
    // cost_pct and cost_basis are Go pointer fields: null is on the contract.
    const payload = clone(OVERVIEW)
    const target = payload.items[0]
    target.cost_pct = null
    target.cost_basis = null

    renderBoard(payload)
    const cost = costOf(await row(target.symbol))

    const dash = within(cost).getByText('—')
    expect(dash).toHaveAttribute('data-absent', 'true')
    expect(dash.className).toContain('metric-absent')
    // Not 0, not 0.00%, not an empty cell.
    expect(cost.textContent).not.toMatch(/\d+(\.\d+)?\s*%/)
    expect(within(cost).getByText('BASIS UNRECORDED')).toBeInTheDocument()
    expect(within(cost).getByText(/recorded no cost provenance/i)).toBeInTheDocument()
  })

  it('keeps every other row\'s hurdle readable when one is null', async () => {
    const payload = clone(OVERVIEW)
    const victim = payload.items.find((i) => i.symbol !== OBSERVED.symbol)!
    victim.cost_pct = null
    renderBoard(payload)
    const cost = costOf(await row(OBSERVED.symbol))
    expect(within(cost).getByText(hurdleText(OBSERVED))).toBeInTheDocument()
  })

  it('shows a basis with no recorded reason as exactly that', async () => {
    const payload = clone(OVERVIEW)
    const target = payload.items[0]
    target.cost_basis = { basis: 'assumed', source: null, observed_at: null, reason: null }

    renderBoard(payload)
    const cost = costOf(await row(target.symbol))
    expect(within(cost).getByText('ASSUMPTION')).toBeInTheDocument()
    expect(within(cost).getByText(/recorded no reason for this basis/i)).toBeInTheDocument()
    expect(within(cost).queryByText(/recorded no cost provenance/i)).toBeNull()
  })

  it('never renders a blank chip for a blank basis word', async () => {
    const payload = clone(OVERVIEW)
    const target = payload.items[0]
    target.cost_basis = { basis: '  ', source: null, observed_at: null, reason: null }
    target.evidence_basis = ''

    renderBoard(payload)
    const card = await row(target.symbol)
    expect(costOf(card)).toHaveAttribute('data-cost-basis', 'unrecorded')
    expect(within(costOf(card)).getByText('BASIS UNRECORDED')).toBeInTheDocument()
    // An unreadable evidence basis is `unknown`, never an empty pill.
    const evidence = card.querySelector('[data-evidence]') as HTMLElement
    expect(evidence.getAttribute('data-evidence')).toBe('unknown')
    expect(evidence.textContent?.trim()).toBe('EVIDENCE UNRECORDED')
  })

  it('gives a basis it has never seen a label and a sentence, not a blank', async () => {
    const payload = clone(OVERVIEW)
    const target = payload.items[0]
    target.evidence_basis = 'partially_modelled'

    renderBoard(payload)
    const card = await row(target.symbol)
    expect(within(card).getByText('PARTIALLY MODELLED')).toBeInTheDocument()
    const note = card.querySelector('.advisory-evidence-note')
    expect(note?.textContent).toMatch(/does not recognise/i)
    expect(note?.textContent).toMatch(/nothing is claimed/i)
  })

  it('renders a null as_of as a dash rather than 1970', async () => {
    const payload = clone(OVERVIEW)
    payload.as_of = null
    renderBoard(payload)
    const counts = await screen.findByText(/readings are model-backed/)
    expect(counts.textContent).toContain('—')
    expect(counts.textContent).not.toContain('1970')
  })

  it('drops an argument line the server sent as null', async () => {
    const payload = clone(OVERVIEW)
    const target = payload.items[0]
    target.top_supporting = null
    target.top_conflicting = ''

    renderBoard(payload)
    const card = await row(target.symbol)
    expect(within(card).queryByText(/Strongest support:/)).toBeNull()
    expect(within(card).queryByText(/Strongest objection:/)).toBeNull()
  })

  it('states a blank stale reason instead of a stale banner with nothing in it', async () => {
    const payload = clone(OVERVIEW)
    const target = payload.items[0]
    target.data_fresh = false
    target.stale_reason = '   '

    renderBoard(payload)
    const card = await row(target.symbol)
    expect(
      within(card).getByText(/The generator recorded no reason for the staleness/i)
    ).toBeInTheDocument()
  })
})

describe('Advisory board — what this platform does not cover', () => {
  it('names each uncovered entry and gives the API reason verbatim', async () => {
    renderBoard()
    await row(ITEMS[0].symbol)
    expect(OVERVIEW.unavailable.length).toBeGreaterThan(0)
    for (const entry of OVERVIEW.unavailable) {
      const el = document.querySelector(`[data-unavailable="${entry.symbol_or_class}"]`)
      expect(el).not.toBeNull()
      expect(el?.textContent).toContain(entry.reason)
      // Named in words or as its own ticker, never as a mangled token.
      const name = el?.querySelector('.advisory-unavailable-name')?.textContent ?? ''
      expect(name).not.toBe('')
      if (/[a-z]/.test(entry.symbol_or_class)) expect(name).not.toContain('_')
    }
  })

  it('reads the label case-insensitively, whichever spelling the API sends', async () => {
    // overview.go spells local silver `ir_silver`; universe.py spells the same
    // gap `IR_SILVER`. The reader must not see a raw token either way.
    const payload = clone(OVERVIEW)
    payload.unavailable = [
      {
        symbol_or_class: 'IR_SILVER',
        category: 'not_collected',
        reason: 'No local Iranian silver series exists.'
      }
    ]
    renderBoard(payload)
    await row(ITEMS[0].symbol)
    expect(screen.getByText('Iranian silver, local price')).toBeInTheDocument()
  })

  it('leaves an instrument code spelled the way the registry spells it', async () => {
    const payload = clone(OVERVIEW)
    payload.unavailable = [
      {
        symbol_or_class: 'IR_GOLD_FUND_KAHRABA',
        category: 'not_scored',
        reason: 'Collected, deliberately not scored.'
      }
    ]
    renderBoard(payload)
    await row(ITEMS[0].symbol)
    // "IR GOLD FUND KAHRABA" is not a name anybody uses.
    expect(screen.getByText('IR_GOLD_FUND_KAHRABA')).toBeInTheDocument()
  })

  it('groups the four kinds of absence and states each one separately', async () => {
    // A single sentence over the whole list is guaranteed to contradict some
    // of the entries under it: `not_scored` covers symbols collected DAILY,
    // and `no_current_reading` is exactly the pending reading the old copy
    // denied existed.
    const payload = clone(OVERVIEW)
    payload.unavailable = [
      { symbol_or_class: 'cars', category: 'not_collected', reason: 'No vehicle series exists.' },
      {
        symbol_or_class: 'DXY',
        category: 'not_scored',
        reason: 'An index level, not a price a reader can hold.'
      },
      {
        symbol_or_class: 'IR_COIN_EMAMI',
        category: 'no_current_reading',
        reason: 'Signal-eligible, but no reading has been published for it in the last 24 hours.'
      },
      {
        symbol_or_class: 'IR_GOLD_FUND_ZAR',
        category: 'not_registered',
        reason: 'Signal-eligible, but absent from the instrument registry.'
      }
    ]
    renderBoard(payload)
    await row(ITEMS[0].symbol)

    for (const category of ['not_collected', 'not_scored', 'no_current_reading', 'not_registered']) {
      const group = document.querySelector(
        `[data-unavailable-group="${category}"]`
      ) as HTMLElement
      expect(group).not.toBeNull()
      expect(group.querySelectorAll('[data-unavailable]')).toHaveLength(1)
    }
    const section = document.querySelector('.advisory-unavailable') as HTMLElement
    // The claim that killed the old copy is gone, and each group says what is
    // true of it alone.
    expect(section.textContent).not.toMatch(/not because a reading is pending/i)
    expect(section.textContent).toMatch(/No price series for these exists in this system at all/i)
    expect(section.textContent).toMatch(/ingested and kept current, and refused a buy\/sell call/i)
    expect(section.textContent).toMatch(/nothing has been published for them recently enough/i)
    expect(section.textContent).toMatch(/registry has no row for them/i)
  })

  it('shows only the groups the payload actually contains', async () => {
    const payload = clone(OVERVIEW)
    payload.unavailable = [
      { symbol_or_class: 'cars', category: 'not_collected', reason: 'No vehicle series exists.' }
    ]
    renderBoard(payload)
    await row(ITEMS[0].symbol)
    expect(document.querySelector('[data-unavailable-group="not_collected"]')).not.toBeNull()
    expect(document.querySelector('[data-unavailable-group="not_scored"]')).toBeNull()
    expect(document.querySelector('[data-unavailable-group="no_current_reading"]')).toBeNull()
  })

  it('does not guess a category the API did not send', async () => {
    // A static bundle can be served against an API that predates `category`.
    const payload = clone(OVERVIEW)
    payload.unavailable = [{ symbol_or_class: 'cars', reason: 'No vehicle series exists.' }]
    renderBoard(payload)
    await row(ITEMS[0].symbol)

    const group = document.querySelector('[data-unavailable-group="uncategorised"]') as HTMLElement
    expect(group).not.toBeNull()
    expect(group.textContent).toMatch(/without saying which kind of absence/i)
    expect(document.querySelector('[data-unavailable="cars"]')).not.toBeNull()
  })

  it('never renders an uncovered entry as an asset row or as "coming soon"', async () => {
    renderBoard()
    await row(ITEMS[0].symbol)
    for (const entry of OVERVIEW.unavailable) {
      expect(screen.queryByTestId(`advisory-${entry.symbol_or_class}`)).toBeNull()
    }
    expect(screen.queryByText(/coming soon/i)).toBeNull()
    // No uncovered entry carries a score, a signal badge or an empty number.
    const section = document.querySelector('.advisory-unavailable') as HTMLElement
    expect(within(section).queryByTestId('signal-badge')).toBeNull()
    expect(section.textContent).not.toMatch(/\d+\s*\/\s*100/)
  })

  it('drops the section entirely rather than showing an empty heading', async () => {
    const payload = clone(OVERVIEW)
    payload.unavailable = []
    renderBoard(payload)
    await row(ITEMS[0].symbol)
    expect(document.querySelector('.advisory-unavailable')).toBeNull()
  })
})

describe('Advisory board — how old the reading itself is', () => {
  it('flags a reading that outlived the engine\'s own review window', async () => {
    const payload = clone(OVERVIEW)
    payload.stale_after_hours = 6
    const target = payload.items[0]
    target.reading_stale = true
    target.reading_age_hours = 19.25

    renderBoard(payload)
    const card = await row(target.symbol)
    const flag = card.querySelector('[data-reading-stale]') as HTMLElement
    expect(flag).not.toBeNull()
    expect(flag.textContent).toContain('19.3 hours')
    expect(flag.textContent).toContain('6-hour window')
    expect(flag.textContent).toMatch(/rather than restated as a current call/i)
  })

  it('keeps reading age and input staleness as two separate statements', async () => {
    // A reading computed on perfectly fresh prices three days ago is fresh by
    // one measure and stale by the other. Merging them would lose whichever
    // one the reader needed.
    const payload = clone(OVERVIEW)
    payload.stale_after_hours = 6
    const target = payload.items[0]
    target.data_fresh = true
    target.reading_stale = true
    target.reading_age_hours = 74

    renderBoard(payload)
    const card = await row(target.symbol)
    expect(card.querySelector('[data-reading-stale]')).not.toBeNull()
    expect(card.querySelector('[data-inputs-stale]')).toBeNull()
  })

  it('flags a row exactly when the server marks it stale, and no other', async () => {
    renderBoard()
    for (const i of ITEMS) {
      const card = await row(i.symbol)
      const flagged = card.querySelector('[data-reading-stale]') !== null
      expect(flagged).toBe(Boolean(i.reading_stale))
    }
  })

  it('states the span when the readings were not all written at once', async () => {
    const payload = clone(OVERVIEW)
    payload.oldest_reading_at = '2026-09-08T06:00:00Z'
    payload.as_of = '2026-09-09T06:00:00Z'

    renderBoard(payload)
    const counts = await screen.findByText(/readings are model-backed/)
    expect(counts.textContent).toMatch(/were not all written at once/i)
    expect(counts.textContent).toMatch(/each row carries its own time/i)
  })

  it('says "as of" only when every reading shares one timestamp', async () => {
    const payload = clone(OVERVIEW)
    payload.oldest_reading_at = payload.as_of
    renderBoard(payload)
    const counts = await screen.findByText(/readings are model-backed/)
    expect(counts.textContent).toMatch(/Readings as of/)
    expect(counts.textContent).not.toMatch(/were not all written at once/i)
  })
})

describe('Advisory board — the coverage statement adds up', () => {
  it('counts an unreadable basis as neither model-backed nor technical-only', async () => {
    const payload = clone(OVERVIEW)
    const target = payload.items.find((i) => i.evidence_basis === 'technical_only')!
    target.evidence_basis = 'unknown'

    const backed = payload.items.filter((i) => i.evidence_basis === 'model_backed').length
    const technical = payload.items.filter((i) => i.evidence_basis === 'technical_only').length

    renderBoard(payload)
    const counts = await screen.findByText(/readings are model-backed/)
    expect(counts.textContent).toContain(`${backed} of ${payload.items.length}`)
    expect(counts.textContent).toContain(`${technical} are technical-only`)
    // The row Go refused to characterise is not silently folded into the
    // technical-only tally, which would assert the thing Go refused to assert.
    expect(counts.textContent).toMatch(/1 records no readable evidence basis/)
  })

  it('makes the three buckets cover every row exactly once', async () => {
    renderBoard()
    const counts = await screen.findByText(/readings are model-backed/)
    const other = ITEMS.length - MODEL_BACKED.length - TECHNICAL.length
    expect(counts.textContent).toContain(`${MODEL_BACKED.length} of ${ITEMS.length}`)
    expect(counts.textContent).toContain(`${TECHNICAL.length} are technical-only`)
    if (other === 0) expect(counts.textContent).not.toMatch(/no readable evidence basis/)
  })

  it('keeps the not-financial-advice framing on the card', async () => {
    renderBoard()
    await row(ITEMS[0].symbol)
    expect(screen.getByText(/Decision support only — not financial advice\./)).toBeInTheDocument()
  })
})

describe('Advisory board — provenance and staleness', () => {
  it('flags a proxy series and leaves an official mirror unflagged', async () => {
    const proxy = pick('a proxy series', (i) => i.is_proxy)
    const direct = pick('a non-proxy series', (i) => !i.is_proxy)
    renderBoard()

    expect(within(await row(proxy.symbol)).getByText('PROXY')).toBeInTheDocument()
    expect(within(await row(direct.symbol)).queryByText('PROXY')).toBeNull()
  })

  it('shows the stale reason on a stale reading and invents none on a fresh one', async () => {
    const stale = pick('stale', (i) => !i.data_fresh)
    const fresh = pick('fresh', (i) => i.data_fresh)
    renderBoard()

    const card = await row(stale.symbol)
    expect(within(card).getByText(/Computed from stale inputs/)).toBeInTheDocument()
    expect(card.textContent).toContain(stale.stale_reason)
    expect(within(await row(fresh.symbol)).queryByText(/Computed from stale inputs/)).toBeNull()
  })
})

describe('Advisory board — states it must not disappear in', () => {
  it('shows a loading state while the request is open', () => {
    serve(null)
    render(
      <SettingsProvider>
        <AdvisoryBoard />
      </SettingsProvider>
    )
    expect(screen.getByText(/Loading the advisory board/)).toBeInTheDocument()
    // The card frame and its preamble are up before the data lands, so the
    // board never appears out of nowhere.
    expect(screen.getByText(/Advisory board — every asset scored/)).toBeInTheDocument()
  })

  it('surfaces an error with a retry rather than an empty card', async () => {
    serve(undefined as unknown as SignalsOverviewResponse, new Error('database error'))
    render(
      <SettingsProvider>
        <AdvisoryBoard />
      </SettingsProvider>
    )
    expect(await screen.findByText('database error')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Retry' })).toBeInTheDocument()
  })

  it('states that nothing has been published instead of inventing rows', async () => {
    renderBoard({ as_of: null, items: [], unavailable: [] })
    expect(await screen.findByText('No readings published')).toBeInTheDocument()
    expect(screen.queryByTestId(/^advisory-/)).toBeNull()
    expect(document.querySelector('.advisory-cost-summary')).toBeNull()
  })

  it('still names the coverage gaps when no reading exists at all', async () => {
    renderBoard({ as_of: null, items: [], unavailable: OVERVIEW.unavailable })
    expect(await screen.findByText('No readings published')).toBeInTheDocument()
    expect(document.querySelector('.advisory-unavailable')).not.toBeNull()
  })
})

describe('The board agrees with the frozen /signals/current reading', () => {
  it('renders the same gold numbers the Advisor card is served', async () => {
    // The two endpoints project the same stored row. If the board ever showed
    // a different score, confidence or hurdle for IR_GOLD_18K than
    // /signals/current does, one of the two would be lying to the same reader
    // on the same page.
    const gold = item('IR_GOLD_18K')
    expect(gold.score).toBe(CURRENT.score)
    expect(gold.confidence).toBe(CURRENT.confidence)
    expect(gold.top_supporting).toBe(CURRENT.supporting[0])
    expect(gold.top_conflicting).toBe(CURRENT.conflicting[0])
    expect(gold.cost_pct).toBe(CURRENT.inputs.round_trip_cost_pct)
    expect(gold.cost_basis?.basis).toBe(CURRENT.inputs.round_trip_cost_basis)
    expect(gold.evidence_basis).toBe(CURRENT.inputs.evidence_basis)

    // The headline is the explanation's opening, not a second opinion about
    // the same row: engine.py builds `explanation` FROM `headline`, so one is
    // always a prefix of the other however much of it the overview projects.
    expect(CURRENT.explanation.startsWith(gold.headline)).toBe(true)

    renderBoard()
    const card = await row('IR_GOLD_18K')
    expect(within(card).getByText(`${CURRENT.score} / 100`)).toBeInTheDocument()
    expect(within(card).getByText(gold.headline)).toBeInTheDocument()
    expect(within(card).getByText(CURRENT.supporting[0])).toBeInTheDocument()
    expect(within(card).getByText(confidenceText(gold))).toBeInTheDocument()
  })
})

describe('Overview keeps the gold advisor and gains the board', () => {
  it('renders both cards from the real page tree', async () => {
    serve(OVERVIEW)
    render(
      <MemoryRouter>
        <SettingsProvider>
          <Overview />
        </SettingsProvider>
      </MemoryRouter>
    )
    // The existing gold card is untouched...
    expect(await screen.findByText('Advisor')).toBeInTheDocument()
    // ...and the multi-asset board is an addition beside it.
    expect(
      await screen.findByText('Advisory board — every asset scored, not gold alone')
    ).toBeInTheDocument()
    for (const i of ITEMS) {
      expect(await screen.findByTestId(`advisory-${i.symbol}`)).toBeInTheDocument()
    }
  })

  it('asks the API for the overview endpoint', async () => {
    serve(OVERVIEW)
    render(
      <MemoryRouter>
        <SettingsProvider>
          <Overview />
        </SettingsProvider>
      </MemoryRouter>
    )
    await waitFor(() =>
      expect(
        apiMock.mock.calls.some((c: unknown[]) => String(c[0]).startsWith('/signals/overview'))
      ).toBe(true)
    )
  })
})
