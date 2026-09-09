import { useMemo, type CSSProperties } from 'react'
import { useApi } from '../hooks/useApi'
import type {
  SignalCostBasis,
  SignalOverviewItem,
  SignalUnavailableEntry,
  SignalsOverviewResponse
} from '../api/types'
import { useSettings } from '../lib/settings'
import { confidencePct, formatDateTime, formatPct } from '../lib/format'
import SignalBadge from './SignalBadge'
import Provenance from './Provenance'
import Loading from './Loading'
import ErrorMessage from './ErrorMessage'
import EmptyState from './EmptyState'

/**
 * The advisory board — every asset this system actually scores, not gold alone.
 *
 * The card exists to carry one distinction the rest of the Overview page cannot
 * make: only IR_GOLD_18K and XAUUSD have trained models behind them. The other
 * readings are trend, momentum and premium, and a reader who assumes the coin's
 * "hold" carries the same evidence as gold's has been misled by the layout, not
 * by the number.
 *
 * So the evidence basis is the most prominent mark on every row, and that is a
 * measurable claim rather than an aspiration:
 *
 *   * it is rendered FIRST, before the buy/sell word, in DOM order — which is
 *     also the order a screen reader announces them in;
 *   * it is the largest and heaviest chip on the row. `.advisory-item
 *     .advisory-evidence` is 14px/700 and `.advisory-item .signal-badge` is
 *     11px/600, inverting the sizes the shared components ship with, and
 *     AdvisoryBoard.test.tsx asserts that relationship. On a board where most
 *     of the assets have no model behind them, "Buy" set larger than
 *     TECHNICAL-ONLY is the wrong emphasis, and it is the emphasis a reader
 *     acts on.
 *
 * Four further refusals, all inherited from the /signals/overview contract
 * (backend-go/internal/signalsvc/overview.go):
 *
 *   * A factor the scorer could not compute is SHOWN as omitted, with the
 *     generator's reason, rather than back-filled with a neutral value that
 *     would read as evidence that was weighed.
 *   * A cost hurdle that was assumed says ASSUMPTION, not a dealer spread.
 *     An observed hurdle exists for 18k gold and for nothing else.
 *   * No row repeats a model claim it does not have. The scorer's prose is
 *     passed through, not rewritten — with the single exception documented at
 *     `withoutUnsupportedModelClaim` below.
 *   * Everything this board does NOT cover is named, grouped by the kind of
 *     absence the API declares (never collected / collected but not scored /
 *     scored with nothing current / not in the registry), so seven rows cannot
 *     be mistaken for the investable universe and no single sentence is
 *     written over four different kinds of gap. None is rendered as an empty
 *     row or as "coming soon".
 *   * A reading that has outlived the window the engine wrote it for is shown
 *     with its age, not restated under the board's newest timestamp.
 */

const DISCLAIMER = 'Decision support only — not financial advice.'

/**
 * The prominence contract, as numbers, in one place.
 *
 * These are the sizes of the two competing marks on a row, and they are
 * declared here rather than only in styles.css so the relationship between
 * them is something a test can assert and a reviewer can see: the evidence
 * badge MUST be larger and heavier than the buy/sell word. The board passes
 * them to CSS as custom properties on its root and the two rules in styles.css
 * read them from there, so there is exactly one place to change and no way to
 * change one without the other.
 *
 * 14 is not arbitrary: the shared `.signal-badge` is 13px/700 globally, which
 * is what used to make "Buy" the loudest thing on a row with no model behind
 * it, so the evidence mark has to clear that number and not merely clear the
 * demoted chip below it.
 */
export const MARK_SIZES = {
  /** The evidence badge: the largest mark on a row, deliberately. */
  evidencePx: 14,
  evidenceWeight: 700,
  /** The buy/sell word, demoted to the size of any other chip. */
  callPx: 11,
  callWeight: 600,
  /** What `.signal-badge` renders at everywhere else on the site. */
  globalSignalBadgePx: 13
} as const

const MARK_STYLE = {
  '--advisory-evidence-size': `${MARK_SIZES.evidencePx}px`,
  '--advisory-evidence-weight': String(MARK_SIZES.evidenceWeight),
  '--advisory-call-size': `${MARK_SIZES.callPx}px`,
  '--advisory-call-weight': String(MARK_SIZES.callWeight)
} as CSSProperties

export const EVIDENCE_MODEL_BACKED = 'model_backed'
const EVIDENCE_TECHNICAL_ONLY = 'technical_only'
const EVIDENCE_UNKNOWN = 'unknown'

const EVIDENCE_LABELS: Record<string, string> = {
  [EVIDENCE_MODEL_BACKED]: 'MODEL-BACKED',
  [EVIDENCE_TECHNICAL_ONLY]: 'TECHNICAL-ONLY',
  [EVIDENCE_UNKNOWN]: 'EVIDENCE UNRECORDED'
}

const EVIDENCE_SENTENCES: Record<string, string> = {
  [EVIDENCE_MODEL_BACKED]: 'A trained model forecast was weighed into this reading.',
  [EVIDENCE_TECHNICAL_ONLY]:
    'No trained model exists for this asset, so this reading is trend, momentum and premium ' +
    'only. It carries no model evidence.',
  [EVIDENCE_UNKNOWN]:
    'The stored inputs behind this reading could not be read, so whether a model stood behind ' +
    'it is not known.'
}

const EVIDENCE_BADGE_CLASS: Record<string, string> = {
  [EVIDENCE_MODEL_BACKED]: 'badge-ok',
  [EVIDENCE_TECHNICAL_ONLY]: 'badge-warn',
  [EVIDENCE_UNKNOWN]: 'badge-off'
}

const COST_OBSERVED = 'observed_spread'
const COST_ASSUMED = 'assumed'
const COST_UNRECORDED = 'unrecorded'

const COST_BASIS_LABELS: Record<string, string> = {
  [COST_OBSERVED]: 'OBSERVED SPREAD',
  [COST_ASSUMED]: 'ASSUMPTION',
  [COST_UNRECORDED]: 'BASIS UNRECORDED'
}

const COST_BASIS_BADGE_CLASS: Record<string, string> = {
  [COST_OBSERVED]: 'badge-ok',
  [COST_ASSUMED]: 'badge-warn',
  [COST_UNRECORDED]: 'badge-off'
}

/**
 * Readable names for the asset classes the API reports as uncollected. Looked
 * up case-insensitively: the Go handler spells local silver `ir_silver` and
 * prediction-python/app/signals/universe.py spells the same gap `IR_SILVER`,
 * and whichever of the two ends up on the wire, the reader should not be shown
 * a raw token.
 */
const UNAVAILABLE_LABELS: Record<string, string> = {
  cars: 'Cars',
  housing: 'Housing',
  tehran_equities: 'Tehran-listed equities',
  ir_silver: 'Iranian silver, local price'
}

function humanize(token: string): string {
  return token.replace(/_/g, ' ')
}

/**
 * `symbol_or_class` carries two different vocabularies: lower-case asset
 * classes (cars, housing) and upper-case instrument codes (DXY, US10Y,
 * IR_GOLD_FUND_KAHRABA). Humanising a code turns a real ticker into
 * "IR GOLD FUND KAHRABA", which is not a name anybody uses, so a code is left
 * exactly as the registry spells it.
 */
function isSymbolCode(token: string): boolean {
  return /[A-Z]/.test(token) && !/[a-z]/.test(token)
}

function unavailableLabel(token: string): string {
  const known = UNAVAILABLE_LABELS[token.trim().toLowerCase()]
  if (known) return known
  return isSymbolCode(token) ? token : humanize(token)
}

/**
 * The evidence basis as a lookup key. An absent or blank basis is `unknown`,
 * never the empty string: an empty string would render as a blank chip, which
 * reads as "nothing to declare" rather than "we could not tell".
 */
function evidenceKey(raw: string | null | undefined): string {
  const key = (raw ?? '').trim()
  return key === '' ? EVIDENCE_UNKNOWN : key
}

/** Every basis gets a sentence, including one this build has never seen. */
function evidenceSentence(basis: string): string {
  return (
    EVIDENCE_SENTENCES[basis] ??
    `The generator recorded the evidence basis for this reading as “${basis}”, which this ` +
      `page does not recognise, so nothing is claimed here about whether a trained model ` +
      `stood behind it.`
  )
}

/**
 * A sentence asserting that a model produced this reading.
 *
 * prediction-python/app/signals/engine.py closes every explanation it writes
 * with fixed boilerplate — "This is an uncertain, model-based assessment of
 * current conditions — not financial advice, and actual outcomes can differ."
 * — and that sentence is appended whether or not any model forecast was
 * available. On the five symbols with no trained model it is simply false, and
 * it is false in the one direction that matters: it lends model authority to a
 * number built from RSI and a moving average.
 *
 * The pattern is deliberately narrow. It matches the ASSERTION ("model-based"),
 * not the word "model", because the scorer's own conflicting-factor line for a
 * technical-only asset reads "No model forecast is available for any horizon" —
 * a denial, which must survive intact.
 */
const MODEL_CLAIM_SENTENCE = /[^.!?]*\bmodel[-\s]?based\b[^.!?]*[.!?]+\s*/gi

/**
 * Scorer prose with any unsupported model claim removed, and a flag saying
 * whether anything was removed.
 *
 * Editing the generator's words is not something this page does lightly, and
 * it is confined to exactly one case: a sentence claiming model backing on a
 * row whose own `evidence_basis` says there is none. The claim is dropped
 * rather than reworded — the true statement is already on the row, in the
 * evidence note — and the removal is disclosed to the reader rather than made
 * silently, so nobody has to wonder whether the page is quietly rewriting the
 * engine. A model-backed row is passed through untouched.
 */
export function withoutUnsupportedModelClaim(
  text: string | null | undefined,
  basis: string
): { text: string; removed: boolean } {
  const original = (text ?? '').trim()
  if (original === '' || basis === EVIDENCE_MODEL_BACKED) {
    return { text: original, removed: false }
  }
  const stripped = original.replace(MODEL_CLAIM_SENTENCE, '').trim()
  return { text: stripped, removed: stripped !== original }
}

/**
 * The one rendering of "not measured", matching the Markets table: dimmed, but
 * always a dash. A null hurdle is not a zero-cost trade.
 */
function AbsentMetric({ reason }: { reason: string }) {
  return (
    <span className="mono metric-absent" title={reason} data-absent="true">
      —
    </span>
  )
}

function EvidenceBadge({ basis }: { basis: string }) {
  const label = EVIDENCE_LABELS[basis] ?? humanize(basis).toUpperCase()
  return (
    <span
      className={`badge advisory-evidence ${EVIDENCE_BADGE_CLASS[basis] ?? 'badge-off'}`}
      data-evidence={basis}
      title={evidenceSentence(basis)}
    >
      {label}
    </span>
  )
}

/**
 * The hurdle and where it came from. The number is always shown — it is what
 * the scorer used — but the badge and the sentence beneath it say plainly
 * whether anybody actually quoted it.
 */
function CostHurdle({ pct, basis }: { pct: number | null; basis: SignalCostBasis | null }) {
  const { calendar } = useSettings()
  const kind = basis?.basis?.trim() || COST_UNRECORDED
  const label = COST_BASIS_LABELS[kind] ?? humanize(kind).toUpperCase()
  const reason = basis?.reason?.trim()
  return (
    <div className="advisory-cost" data-cost-basis={kind}>
      <div className="kv">
        <span className="muted">Round-trip cost hurdle</span>
        <span className="mono">
          {pct !== null && pct !== undefined ? (
            formatPct(pct, { sign: false })
          ) : (
            <AbsentMetric reason="This reading records no round-trip cost, so no hurdle is shown. It is not zero." />
          )}
        </span>
      </div>
      <div className="advisory-cost-basis">
        <span className={`badge ${COST_BASIS_BADGE_CLASS[kind] ?? 'badge-off'}`}>{label}</span>
        {basis?.source && <span className="mono small muted">{basis.source}</span>}
        {basis?.observed_at && (
          <span className="small muted">quoted {formatDateTime(basis.observed_at, calendar)}</span>
        )}
      </div>
      {reason && <p className="muted small advisory-cost-reason">{reason}</p>}
      {/* Two different silences, said differently. No provenance object at all
          is not the same as a basis recorded without its reason, and neither
          one is filled in with a plausible sentence. */}
      {!reason && !basis && (
        <p className="muted small advisory-cost-reason">
          The generator recorded no cost provenance for this reading, so nothing is claimed about
          where a hurdle would come from.
        </p>
      )}
      {!reason && basis && (
        <p className="muted small advisory-cost-reason">
          The generator recorded no reason for this basis, so none is invented here.
        </p>
      )}
    </div>
  )
}

/**
 * What was NOT weighed. Collapsed, because a thin-history fund carries four of
 * these and they would drown the reading — but present in the document and one
 * click from the score they qualify.
 */
function OmittedFactors({ item }: { item: SignalOverviewItem }) {
  const omitted = item.omitted_factors ?? []
  if (omitted.length === 0) {
    return (
      <p className="muted small advisory-omitted-none">
        Every factor this asset can carry was computed — nothing was skipped.
      </p>
    )
  }
  return (
    <details className="advisory-omitted">
      <summary>
        Factors not weighed ({omitted.length}) — and why
      </summary>
      <ul className="advisory-omitted-list">
        {omitted.map((f) => (
          <li key={f.factor} className="advisory-omitted-item">
            <span className="mono advisory-omitted-name">{f.factor}</span>
            <span className="muted small">{f.reason}</span>
          </li>
        ))}
      </ul>
    </details>
  )
}

/** One argument line, with any unsupported model claim already removed. */
function ArgumentLine({ label, text }: { label: string; text: string }) {
  if (text === '') return null
  return (
    <p className="small advisory-arg">
      <span className="muted">{label} </span>
      {text}
    </p>
  )
}

/**
 * How old this reading is, when the server told us.
 *
 * Two different stalenesses live on one row and they are independent, so they
 * are never merged: `data_fresh` is about the PRICES the scorer was handed
 * (false forced a hold), while `reading_stale` is about the READING itself
 * having outlived the review window the engine wrote it for. A reading
 * computed on perfectly fresh prices three days ago is fresh by the first
 * measure and stale by the second, and a reader deciding whether to act on it
 * needs the second.
 */
function ReadingAge({
  item,
  staleAfterHours
}: {
  item: SignalOverviewItem
  staleAfterHours: number | undefined
}) {
  if (!item.reading_stale) return null
  const age = item.reading_age_hours
  return (
    <div className="callout callout-warn small" data-reading-stale="true">
      This reading is {age !== undefined ? `${age.toFixed(1)} hours` : 'older than the review window'}{' '}
      old
      {staleAfterHours !== undefined && <> — past the {staleAfterHours}-hour window the engine
      writes each reading for</>}
      . It is shown with its age rather than restated as a current call.
    </div>
  )
}

function AssetRow({
  item,
  staleAfterHours
}: {
  item: SignalOverviewItem
  staleAfterHours: number | undefined
}) {
  const { calendar } = useSettings()
  const conf = confidencePct(item.confidence)
  const basis = evidenceKey(item.evidence_basis)

  // The scorer's prose, filtered against the row's own evidence basis.
  const headline = withoutUnsupportedModelClaim(item.headline, basis)
  const supporting = withoutUnsupportedModelClaim(item.top_supporting, basis)
  const conflicting = withoutUnsupportedModelClaim(item.top_conflicting, basis)
  const claimRemoved = headline.removed || supporting.removed || conflicting.removed

  const stale = item.stale_reason?.trim()

  return (
    <li className="advisory-item" data-symbol={item.symbol} data-testid={`advisory-${item.symbol}`}>
      {/* The evidence basis leads the row, in DOM order and in weight. */}
      <div className="advisory-item-head">
        <div className="advisory-name">
          <span className="advisory-name-en">{item.name_en}</span>{' '}
          <span className="bidi-fa" lang="fa" dir="rtl">
            {item.name_fa}
          </span>
          <div className="small muted">
            <span className="mono">{item.symbol}</span>
            {item.quote_currency && (
              <>
                {' '}
                · quoted in <span className="mono">{item.quote_currency}</span>
                {item.unit ? ` per ${humanize(item.unit)}` : ''}
              </>
            )}
          </div>
        </div>
        <EvidenceBadge basis={basis} />
      </div>

      <p className="small advisory-evidence-note">{evidenceSentence(basis)}</p>

      <div className="advisory-marks">
        <span className="advisory-call">
          <span className="muted small advisory-call-label">Reading</span>
          <SignalBadge signal={item.signal} />
        </span>
        <Provenance tier={item.quality_tier} isProxy={item.is_proxy} />
      </div>

      <div className="advisory-scores">
        <div className="kv">
          <span className="muted">Score</span>
          <span className="mono">{item.score} / 100</span>
        </div>
        <div className="kv">
          <span className="muted">Confidence</span>
          <span className="mono">
            {conf !== null ? (
              `${Math.round(conf)}%`
            ) : (
              <AbsentMetric reason="No confidence was recorded for this reading." />
            )}
          </span>
        </div>
      </div>

      {headline.text !== '' && <p className="advisory-headline">{headline.text}</p>}
      {headline.text === '' && (
        <p className="muted small advisory-headline-absent">
          The generator recorded no one-line explanation this row can stand behind.
        </p>
      )}
      {claimRemoved && (
        <p className="muted small advisory-claim-note" data-claim-removed="true">
          The generator&rsquo;s wording above ended with its fixed closing sentence asserting a
          model assessment. This row&rsquo;s own evidence basis says no model stood behind it, so
          that sentence was removed rather than reprinted here.
        </p>
      )}

      <ArgumentLine label="Strongest support:" text={supporting.text} />
      <ArgumentLine label="Strongest objection:" text={conflicting.text} />

      <CostHurdle pct={item.cost_pct} basis={item.cost_basis} />

      {!item.data_fresh && (
        <div className="callout callout-warn small" data-inputs-stale="true">
          Computed from stale inputs.{' '}
          {stale || 'The generator recorded no reason for the staleness.'}
        </div>
      )}

      <ReadingAge item={item} staleAfterHours={staleAfterHours} />

      <OmittedFactors item={item} />

      <div className="small muted advisory-item-foot">
        Read at {formatDateTime(item.generated_at, calendar)} (Tehran)
      </div>
    </li>
  )
}

/**
 * How the board's hurdles were sourced, derived from the payload rather than
 * asserted.
 *
 * `uniformAssumed` is the figure every assumed hurdle carries, or null when
 * they differ. It exists because of what
 * prediction-python/app/core/costs.py actually does: exactly one asset
 * (IR_GOLD_18K) has a two-sided dealer quote, and every other asset falls back
 * to the SAME structural fee-plus-spread-plus-slippage figure with its own
 * reason attached. Six identical percentages in a column look like six
 * measurements unless the page says otherwise — and it can only say so
 * honestly by checking, which is what this does.
 */
interface CostSourcing {
  observed: SignalOverviewItem[]
  assumed: SignalOverviewItem[]
  uniformAssumed: number | null
}

function summarizeCostSourcing(items: SignalOverviewItem[]): CostSourcing {
  const observed = items.filter((i) => i.cost_basis?.basis === COST_OBSERVED)
  const assumed = items.filter((i) => i.cost_basis?.basis === COST_ASSUMED)
  const figures = assumed.map((i) => i.cost_pct)
  const first = figures[0]
  const uniform =
    figures.length > 0 && first !== null && first !== undefined && figures.every((f) => f === first)
      ? first
      : null
  return { observed, assumed, uniformAssumed: uniform }
}

function CostSourcingNote({ sourcing }: { sourcing: CostSourcing }) {
  const { observed, assumed, uniformAssumed } = sourcing
  if (observed.length === 0 && assumed.length === 0) return null
  return (
    <p className="muted small advisory-cost-summary">
      {observed.length > 0 && (
        <>
          {observed.length === 1 ? 'One reading carries' : `${observed.length} readings carry`} an
          observed dealer spread (
          <span className="mono">{observed.map((i) => i.symbol).join(', ')}</span>).{' '}
        </>
      )}
      {observed.length === 0 && assumed.length > 0 && (
        <>No reading on this board carries an observed dealer spread. </>
      )}
      {assumed.length > 0 && uniformAssumed !== null && assumed.length > 1 && (
        <>
          The other {assumed.length} hurdles are not {assumed.length} per-market quotes: they are
          one and the same conservative assumption ({formatPct(uniformAssumed, { sign: false })}),
          repeated, and each row carries the reason no quote exists for that asset.
        </>
      )}
      {assumed.length > 0 && (uniformAssumed === null || assumed.length === 1) && (
        <>
          {assumed.length === 1 ? 'One hurdle is' : `${assumed.length} hurdles are`} assumed rather
          than quoted; each of those rows carries the reason no quote exists for that asset.
        </>
      )}
    </p>
  )
}

/** The board's account of its own coverage: what it read, and what it did not. */
function CoverageStatement({
  items,
  asOf,
  oldest
}: {
  items: SignalOverviewItem[]
  asOf: string | null | undefined
  oldest: string | null | undefined
}) {
  const { calendar } = useSettings()
  const backed = items.filter((i) => evidenceKey(i.evidence_basis) === EVIDENCE_MODEL_BACKED)
  const technical = items.filter((i) => evidenceKey(i.evidence_basis) === EVIDENCE_TECHNICAL_ONLY)
  // Anything else — `unknown`, or a basis this build does not know. Counted
  // separately rather than folded into "technical-only": Go emits `unknown`
  // precisely when it could NOT read the stored inputs, and calling that
  // technical-only would assert the very thing it refused to assert.
  const other = items.length - backed.length - technical.length
  // `as_of` is the NEWEST reading's timestamp, not a common one. Printing it
  // alone stamps the freshest row's time over the whole board, so when the
  // oldest differs the board states the span it actually covers.
  const spread = oldest && asOf && oldest !== asOf
  return (
    <p className="muted small advisory-counts">
      {backed.length} of {items.length} readings are model-backed
      {backed.length > 0 && (
        <> (<span className="mono">{backed.map((i) => i.symbol).join(', ')}</span>)</>
      )}
      ; {technical.length} {technical.length === 1 ? 'is' : 'are'} technical-only
      {other > 0 && (
        <>
          ; {other} {other === 1 ? 'records' : 'record'} no readable evidence basis and{' '}
          {other === 1 ? 'is' : 'are'} claimed as neither
        </>
      )}
      .{' '}
      {spread ? (
        <>
          These readings were not all written at once: they run from{' '}
          {formatDateTime(oldest ?? null, calendar)} to {formatDateTime(asOf ?? null, calendar)}{' '}
          (Tehran), and each row carries its own time.
        </>
      ) : (
        <>Readings as of {formatDateTime(asOf ?? null, calendar)} (Tehran).</>
      )}
    </p>
  )
}

/**
 * The four kinds of absence, in the order a reader asks about them, each with
 * the statement that is true of THAT group and of no other.
 *
 * The old copy wrote one sentence over the whole list — "absent because the
 * data does not exist in this system, not because a reading is pending" — and
 * the contract contradicted it twice over: `no_current_reading` is exactly a
 * pending reading, and `not_scored` covers DXY, US10Y and BRENT_OIL, which are
 * collected daily and refused for being index levels and yields rather than
 * prices. A single sentence over four categories is guaranteed to be false
 * about some of the entries printed under it.
 */
const UNAVAILABLE_GROUPS: Array<{ category: string; title: string; blurb: string }> = [
  {
    category: 'not_collected',
    title: 'Never collected',
    blurb:
      'No price series for these exists in this system at all, so there is nothing to score. ' +
      'Their absence is structural, not a pending reading.'
  },
  {
    category: 'not_scored',
    title: 'Collected, deliberately not scored',
    blurb:
      'These are ingested and kept current, and refused a buy/sell call on purpose. Each entry ' +
      'names the property that disqualifies it — an index level or a yield is not something a ' +
      'reader holds.'
  },
  {
    category: 'no_current_reading',
    title: 'Scored, but nothing current to show',
    blurb:
      'This board does score these. Nothing has been published for them recently enough to be ' +
      'called current, and the last reading is withheld rather than re-served under today’s ' +
      'timestamp.'
  },
  {
    category: 'not_registered',
    title: 'Scored, but not in the instrument registry',
    blurb:
      'Signal-eligible and impossible to name: the registry has no row for them, so their ' +
      'currency, unit and quality tier cannot be stated. An operational fault, shown rather ' +
      'than hidden.'
  }
]

const UNCATEGORISED_BLURB =
  'The API named these as uncovered without saying which kind of absence it is, so nothing is ' +
  'claimed here beyond each entry’s own reason.'

function UnavailableList({ entries }: { entries: SignalUnavailableEntry[] }) {
  return (
    <ul className="advisory-unavailable-list">
      {entries.map((entry) => (
        <li
          key={entry.symbol_or_class}
          className="advisory-unavailable-item"
          data-unavailable={entry.symbol_or_class}
          data-unavailable-category={entry.category ?? ''}
        >
          <span className="advisory-unavailable-name">
            {unavailableLabel(entry.symbol_or_class)}
          </span>
          <span className="muted small">
            {entry.reason?.trim() ||
              'The API named this as uncovered but recorded no reason, so none is invented here.'}
          </span>
        </li>
      ))}
    </ul>
  )
}

/**
 * The coverage boundary, grouped by the kind of absence the API declares.
 * Entries whose category this build does not know are shown last, under a
 * heading that says only what is actually known about them.
 */
function NotCovered({ entries }: { entries: SignalUnavailableEntry[] }) {
  const groups = useMemo(() => {
    const known = new Set(UNAVAILABLE_GROUPS.map((g) => g.category))
    const out = UNAVAILABLE_GROUPS.map((g) => ({
      ...g,
      entries: entries.filter((e) => e.category === g.category)
    })).filter((g) => g.entries.length > 0)
    const rest = entries.filter((e) => !e.category || !known.has(e.category))
    if (rest.length > 0) {
      out.push({
        category: '',
        title: 'Absent, kind not stated',
        blurb: UNCATEGORISED_BLURB,
        entries: rest
      })
    }
    return out
  }, [entries])

  if (entries.length === 0) return null

  return (
    <section className="advisory-unavailable">
      <div className="section-title">Not covered by this board</div>
      <p className="muted small">
        Every entry carries the API&rsquo;s own reason, verbatim, and is grouped by the kind of
        absence it is — the four are not interchangeable. None of them is rendered as an empty
        row, as a score, or as a promise that a reading is on the way.
      </p>
      {groups.map((group) => (
        <div
          key={group.category || 'uncategorised'}
          className="advisory-unavailable-group"
          data-unavailable-group={group.category || 'uncategorised'}
        >
          <div className="advisory-unavailable-group-title">{group.title}</div>
          <p className="muted small advisory-unavailable-group-blurb">{group.blurb}</p>
          <UnavailableList entries={group.entries} />
        </div>
      ))}
    </section>
  )
}

export default function AdvisoryBoard() {
  const board = useApi<SignalsOverviewResponse>('/signals/overview')

  const items = useMemo(() => board.data?.items ?? [], [board.data])
  const unavailable = useMemo(() => board.data?.unavailable ?? [], [board.data])
  const sourcing = useMemo(() => summarizeCostSourcing(items), [items])
  const staleAfterHours = board.data?.stale_after_hours

  return (
    <div className="card advisory-board" style={MARK_STYLE}>
      <div className="card-title">Advisory board — every asset scored, not gold alone</div>

      <p className="muted small advisory-preamble">
        One reading per asset the signal engine covers. The mark that matters most on each row is
        the evidence basis, and it is set larger than the buy/sell word for that reason: a
        MODEL-BACKED reading had a trained forecast weighed into it, while a TECHNICAL-ONLY reading
        is trend, momentum and premium with no model behind it. The two are not interchangeable,
        and this board never presents them as if they were. {DISCLAIMER}
      </p>

      {board.loading && <Loading label="Loading the advisory board…" />}
      {board.error && <ErrorMessage message={board.error} onRetry={board.reload} />}

      {!board.loading && !board.error && items.length === 0 && (
        <EmptyState
          title="No readings published"
          hint="The signal generator has not written a reading for any eligible symbol yet. Nothing is shown in their place."
        />
      )}

      {items.length > 0 && (
        <>
          <CoverageStatement
            items={items}
            asOf={board.data?.as_of}
            oldest={board.data?.oldest_reading_at}
          />
          <CostSourcingNote sourcing={sourcing} />
          <ul className="advisory-list">
            {items.map((item) => (
              <AssetRow key={item.symbol} item={item} staleAfterHours={staleAfterHours} />
            ))}
          </ul>
        </>
      )}

      <NotCovered entries={unavailable} />
    </div>
  )
}
