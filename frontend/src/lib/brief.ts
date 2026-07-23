import type { CustomForecast, MarketSummary, Prediction } from '../api/types'
import { confidencePct, formatPct } from './format'
import {
  ADVISOR_HORIZON_LABELS,
  bestWindows,
  planRows,
  type PlanRow,
  type Tilt
} from './advice'

/**
 * The written brief (docs/CONTRACTS.md Addendum 12): a plain-language reading
 * of the current forecasts — situation, expectations, possibilities and an
 * overall prescription. Pure composition over already-fetched payloads; every
 * sentence is derived from numbers the rest of the UI shows, so the brief can
 * never disagree with the dashboards.
 */
export interface BriefSection {
  key: string
  title: string
  paragraphs: string[]
}

export interface BriefInput {
  summary: MarketSummary | null
  /** Latest 18k predictions (normalized). */
  predictions: Prediction[]
  /** On-demand 7-day forecast with Monte Carlo odds; null while loading/unavailable. */
  custom: CustomForecast | null
  /** Round-trip cost basis (live dealer spread or fallback), percent. */
  costPct: number
  /** Toman formatter bound to the user's IRT/IRR display unit. */
  fmt: (v: number) => string
  /** Fixed clock for tests. */
  now?: number
}

const signedPct = (v: number): string => formatPct(v)

function trendWord(pct: number | null | undefined): string {
  if (pct === null || pct === undefined || !Number.isFinite(pct)) return 'steady'
  if (pct > 0.05) return `up ${formatPct(pct, { sign: false })}`
  if (pct < -0.05) return `down ${formatPct(Math.abs(pct), { sign: false })}`
  return 'flat'
}

function situationParagraphs(s: MarketSummary | null, costPct: number, fmt: (v: number) => string): string[] {
  if (s === null) return []
  const out: string[] = []
  if (s.current_18k) {
    out.push(
      `18k gold trades at ${fmt(s.current_18k.value)} per gram, ` +
        `${trendWord(s.current_18k.change_24h_pct)} over the last 24 hours.`
    )
  }
  const legs: string[] = []
  if (s.usd_irt) legs.push(`the free-market dollar is at ${fmt(s.usd_irt.value)} (${trendWord(s.usd_irt.change_24h_pct)})`)
  if (s.xau_usd) legs.push(`global gold sits at $${s.xau_usd.value.toFixed(0)} per ounce (${trendWord(s.xau_usd.change_24h_pct)})`)
  if (legs.length > 0) out.push(`Behind it, ${legs.join(' and ')}.`)
  if (s.premium_pct !== null && s.premium_pct !== undefined) {
    const side = s.premium_pct >= 0 ? 'above' : 'below'
    let sentence =
      `The local price is ${formatPct(Math.abs(s.premium_pct), { sign: false })} ${side} its ` +
      `global-parity value (world gold × dollar rate)`
    if (s.premium_avg_30d !== null && s.premium_avg_30d !== undefined) {
      const dev = s.premium_pct - s.premium_avg_30d
      const verdict =
        Math.abs(dev) < 1
          ? 'in line with the 30-day norm'
          : dev > 0
            ? `stretched ${formatPct(dev, { sign: false })} above the 30-day norm — local demand is paying up`
            : `compressed ${formatPct(Math.abs(dev), { sign: false })} below the 30-day norm — locally cheap versus parity`
      sentence += `, ${verdict}`
    }
    out.push(sentence + '.')
  }
  out.push(
    `Every round trip through a dealer costs about ${costPct.toFixed(2)}% ` +
      `(the observed buy/sell spread) — a forecast move only matters once it clears that bar.`
  )
  return out
}

function expectationParagraphs(rows: PlanRow[], fmt: (v: number) => string): string[] {
  if (rows.length === 0) return []
  const lines = rows.map((r) => {
    const label = ADVISOR_HORIZON_LABELS[r.horizon] ?? r.horizon
    return (
      `${label}: ${fmt(r.forecast)} (${signedPct(r.expectedChangePct)}), ` +
      `90% range ${fmt(r.lower)} – ${fmt(r.upper)}.`
    )
  })
  let hi = rows[0]
  let lo = rows[0]
  for (const r of rows) {
    if (r.expectedChangePct > hi.expectedChangePct) hi = r
    if (r.expectedChangePct < lo.expectedChangePct) lo = r
  }
  const spread =
    hi === lo
      ? []
      : [
          `The strongest projected move is ${signedPct(hi.expectedChangePct)} by ` +
            `${(ADVISOR_HORIZON_LABELS[hi.horizon] ?? hi.horizon).toLowerCase()}; the weakest is ` +
            `${signedPct(lo.expectedChangePct)} by ${(ADVISOR_HORIZON_LABELS[lo.horizon] ?? lo.horizon).toLowerCase()}.`
        ]
  return [...lines, ...spread]
}

function possibilityParagraphs(custom: CustomForecast | null, rows: PlanRow[], fmt: (v: number) => string): string[] {
  const out: string[] = []
  const mc = custom?.monte_carlo
  if (custom && mc) {
    out.push(
      `Simulating ${mc.n_paths.toLocaleString()} price paths over the next ${custom.horizon_days} days: ` +
        `${formatPct(mc.p_up * 100, { sign: false, digits: 0 })} odds the price ends higher at all, ` +
        `${formatPct(mc.p_gain_over_cost * 100, { sign: false, digits: 0 })} odds of a gain that clears the round-trip cost, and ` +
        `${formatPct(mc.p_loss_over_cost * 100, { sign: false, digits: 0 })} odds of a loss deeper than that cost.`
    )
    out.push(
      `The middle simulated outcome is ${signedPct(mc.sim_median_pct)}; a bad run (worst 1-in-20) is around ` +
        `${signedPct(mc.sim_p05_pct)} and a good run (best 1-in-20) around ${signedPct(mc.sim_p95_pct)}.`
    )
  }
  if (rows.length > 0) {
    let lo = rows[0].lower
    let hi = rows[0].upper
    for (const r of rows) {
      if (r.lower < lo) lo = r.lower
      if (r.upper > hi) hi = r.upper
    }
    out.push(
      `Across all standard horizons the 90% intervals span ${fmt(lo)} to ${fmt(hi)} — ` +
        `treat that as the plausible envelope, not a prediction.`
    )
  }
  return out
}

function tiltCensus(rows: PlanRow[]): Record<Tilt, PlanRow[]> {
  const by: Record<Tilt, PlanRow[]> = {
    'favors-buying': [],
    'favors-selling': [],
    'favors-waiting': [],
    unclear: [],
    'no-call': []
  }
  for (const r of rows) by[r.tilt].push(r)
  return by
}

const horizonList = (rows: PlanRow[]): string =>
  rows.map((r) => (ADVISOR_HORIZON_LABELS[r.horizon] ?? r.horizon).toLowerCase()).join(', ')

function prescriptionParagraphs(
  rows: PlanRow[],
  custom: CustomForecast | null,
  currentPrice: number | null,
  costPct: number,
  fmt: (v: number) => string
): string[] {
  if (rows.length === 0) return []
  const out: string[] = []
  const by = tiltCensus(rows)

  if (by['favors-buying'].length > 0) {
    out.push(
      `Over ${horizonList(by['favors-buying'])} the projected gain clears the ${costPct.toFixed(2)}% ` +
        `round-trip cost with decent confidence: conditions modestly favor buying for those timeframes.`
    )
  }
  if (by['favors-selling'].length > 0) {
    out.push(
      `Over ${horizonList(by['favors-selling'])} the models project a drop beyond the cost bar: ` +
        `conditions modestly favor selling (or delaying a planned purchase) for those timeframes.`
    )
  }
  if (by['favors-buying'].length === 0 && by['favors-selling'].length === 0) {
    out.push(
      `Every projected move is smaller than the ${costPct.toFixed(2)}% round-trip cost, or the models are ` +
        `not confident enough to call it. The prescription is patience: hold what you hold, buy only for ` +
        `reasons beyond this week's price (savings plans, hedging a toman income), and re-read this brief ` +
        `after the next training run.`
    )
  }

  const windows = bestWindows(rows, currentPrice)
  if (windows) {
    const describe = (w: (typeof windows)['buy'], kind: 'buy' | 'sell'): string => {
      if (w.when === 'today') {
        return kind === 'buy'
          ? 'every forecast sits above today’s price, so the likely best buy window is now'
          : 'every forecast sits below today’s price, so the likely best sell window is now'
      }
      const label = (ADVISOR_HORIZON_LABELS[w.row.horizon] ?? w.row.horizon).toLowerCase()
      return `the likely best ${kind} window is around ${label} (projected ${fmt(w.row.forecast)})`
    }
    out.push(
      `Timing, if you must: ${describe(windows.buy, 'buy')}; ${describe(windows.sell, 'sell')}. ` +
        `Both are point guesses inside wide intervals.`
    )
  }

  if (custom) {
    const conf = confidencePct(custom.confidence)
    out.push(
      `The on-demand ${custom.horizon_days}-day model (${custom.model_name}) leans "${custom.decision_lean}"` +
        `${conf !== null ? ` at ${formatPct(conf, { sign: false, digits: 0 })} confidence` : ''}: ` +
        `${custom.decision_note}`
    )
  }

  const staleRows = rows.filter((r) => !r.dataFresh)
  if (staleRows.length > 0) {
    out.push(
      `Caution: forecasts for ${horizonList(staleRows)} were computed from stale inputs — ` +
        `treat them as background, not signals.`
    )
  }
  const gap = custom?.provider_gap_pct
  if (gap !== null && gap !== undefined && gap >= 1) {
    out.push(
      `Caution: price providers currently disagree by ${formatPct(gap, { sign: false })} — ` +
        `quote uncertainty is elevated, so intervals are wider than usual.`
    )
  }

  out.push(
    'This brief is a statistical reading of recent data. It is uncertain by construction and is not financial advice.'
  )
  return out
}

/** Compose the whole brief; sections without content are dropped. */
export function composeBrief(input: BriefInput): BriefSection[] {
  const currentPrice = input.summary?.current_18k?.value ?? null
  const rows = planRows(input.predictions, currentPrice, input.costPct, input.now)
  const sections: BriefSection[] = [
    {
      key: 'situation',
      title: 'Where things stand',
      paragraphs: situationParagraphs(input.summary, input.costPct, input.fmt)
    },
    {
      key: 'expectations',
      title: 'What the models expect',
      paragraphs: expectationParagraphs(rows, input.fmt)
    },
    {
      key: 'possibilities',
      title: 'Possibilities and odds',
      paragraphs: possibilityParagraphs(input.custom, rows, input.fmt)
    },
    {
      key: 'prescription',
      title: 'Bottom line — the prescription',
      paragraphs: prescriptionParagraphs(rows, input.custom, currentPrice, input.costPct, input.fmt)
    }
  ]
  return sections.filter((s) => s.paragraphs.length > 0)
}
