import type { NumeraireOption } from '../api/types'

/**
 * Pure helpers for the P1 purchasing-power pages.
 *
 * The one rule these encode: a missing metric is not a zero. `finiteOrNull`
 * refuses to coerce, `compareNullable` keeps un-measured rows out of the
 * ranking instead of parking them at the bottom of the number line, and
 * `noteText` surfaces the backend's own reason for the absence.
 */

export type SortDir = 'asc' | 'desc'

/**
 * A number, or null. Anything that is not a finite number — undefined, null,
 * NaN, Infinity, a string the backend failed to render — becomes null. It must
 * never become 0: "we could not measure it" and "it did not move" are
 * different facts.
 */
export function finiteOrNull(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

/**
 * Order two optional metrics. Nulls sort LAST in both directions: an
 * un-measured row is not the smallest value, it is not on the scale at all,
 * and letting it lead an ascending sort would read as "worst performer".
 */
export function compareNullable(a: number | null, b: number | null, dir: SortDir): number {
  if (a === null && b === null) return 0
  if (a === null) return 1
  if (b === null) return -1
  return dir === 'asc' ? a - b : b - a
}

export function compareText(a: string, b: string, dir: SortDir): number {
  const cmp = a.localeCompare(b)
  return dir === 'asc' ? cmp : -cmp
}

/** The badge class for a quality tier. The tier WORD is always rendered too. */
export function tierClass(tier: string | null | undefined): string {
  switch (tier) {
    case 'official':
      return 'badge-ok'
    case 'official_mirror':
      return 'badge-info'
    case 'commercial':
      return 'badge-info'
    case 'proxy':
    case 'estimate':
    case 'experimental':
      return 'badge-warn'
    default:
      return 'badge-off'
  }
}

export function tierLabel(tier: string | null | undefined): string {
  return (tier ?? 'unknown').replace(/_/g, ' ')
}

// --- instrument domains -------------------------------------------------------
//
// `instruments.domain` is free text in the registry (migration 0024 lists gold,
// fx, global and fund; 0031 adds silver; the purchasing-power table adds
// ir_equity for Tehran shares). These helpers give each a readable name and one
// fixed order, and fall back to the raw domain for one a later migration adds,
// so a new asset class shows up under its own name instead of vanishing.

/** Readable names for `instruments.domain`, in the order pickers list them. */
export const DOMAIN_LABELS: Record<string, string> = {
  gold: 'Gold & coins',
  silver: 'Silver',
  fund: 'Commodity funds',
  fx: 'Currency',
  global: 'Global markets',
  ir_equity: 'Tehran equities'
}

const DOMAIN_ORDER = Object.keys(DOMAIN_LABELS)

/** The label for a domain; the raw domain itself when this build has no name for it. */
export function domainLabel(domain: string | null | undefined): string {
  const key = (domain ?? '').trim()
  if (key === '') return 'Other'
  return DOMAIN_LABELS[key] ?? key
}

/** Known domains in DOMAIN_LABELS order, then any other domain alphabetically. */
export function compareDomains(a: string, b: string): number {
  const ia = DOMAIN_ORDER.indexOf(a)
  const ib = DOMAIN_ORDER.indexOf(b)
  if (ia !== -1 || ib !== -1) {
    if (ia === -1) return 1
    if (ib === -1) return -1
    return ia - ib
  }
  return a.localeCompare(b)
}

export interface DomainGroup<T> {
  domain: string
  label: string
  items: T[]
}

/**
 * Group rows by domain, groups in the fixed domain order, rows keeping the
 * order they arrived in. A row with no domain lands in one group of its own
 * rather than being dropped.
 */
export function groupByDomain<T extends { domain?: string | null }>(items: T[]): DomainGroup<T>[] {
  const groups = new Map<string, T[]>()
  for (const item of items) {
    const key = (item.domain ?? '').trim()
    const bucket = groups.get(key)
    if (bucket) bucket.push(item)
    else groups.set(key, [item])
  }
  return Array.from(groups.keys())
    .sort(compareDomains)
    .map((domain) => ({ domain, label: domainLabel(domain), items: groups.get(domain) ?? [] }))
}

/** The distinct domains present, in the fixed order: the filter chips' vocabulary. */
export function domainsPresent(items: Array<{ domain?: string | null }>): string[] {
  return groupByDomain(items).map((g) => g.domain)
}

/**
 * Whether GET /relative-value can serve a registry row as a leg.
 *
 * The endpoint reads daily closes from `prices` and refuses a disabled row or a
 * rate (quote_currency PCT) with a 400 — relvalue.legRefusal. An economic
 * series (CPI, money supply) is refused by nothing but has no `prices` rows,
 * so picking one only ever produced "these two share no day". None of the
 * three is offered.
 */
export function isRelativeValueLeg(item: {
  kind?: string | null
  quote_currency?: string | null
  enabled?: boolean | null
}): boolean {
  return (
    item.kind !== 'economic_series' && item.quote_currency !== 'PCT' && item.enabled !== false
  )
}

/** The backend's own explanation for this row, joined for a title attribute. */
export function noteText(notes: string[] | null | undefined): string {
  if (!notes || notes.length === 0) return ''
  return notes.join(' · ')
}

/**
 * Extract the item with the highest (or lowest) value of a metric, ignoring
 * rows where the metric is null. Returns null when nothing was measured — the
 * caller then says so rather than crowning an arbitrary row.
 */
export function rankBy<T>(
  items: T[],
  metric: (item: T) => number | null,
  direction: 'highest' | 'lowest'
): { item: T; value: number; measured: number } | null {
  const scored = items
    .map((item) => ({ item, value: metric(item) }))
    .filter((row): row is { item: T; value: number } => row.value !== null)
  if (scored.length === 0) return null
  const best = scored.reduce((acc, row) =>
    direction === 'highest' ? (row.value > acc.value ? row : acc) : row.value < acc.value ? row : acc
  )
  return { item: best.item, value: best.value, measured: scored.length }
}

// --- numéraires --------------------------------------------------------------
//
// The identifier Go emits is `key`, the backing instrument is `series`, and
// `notes` is an array. Reading `code`/`series_code`/`note` instead matched
// nothing, and the selector then pinned itself on `undefined` and asked the
// server for `numeraire=undefined` for the rest of the session. These helpers
// exist so that failure mode has one place to be tested.

/** A numéraire key that is actually usable as a query parameter. */
function hasKey(option: NumeraireOption): boolean {
  return typeof option.key === 'string' && option.key.length > 0
}

/**
 * The numéraires this deployment can back, in the order Go listed them.
 *
 * `available === false` is Go saying a request naming this key would earn a
 * 400, so it must not be selectable. An option carrying no `key` is dropped
 * too: it could only ever become `<option value={undefined}>`, which is the
 * bug this module is here to prevent.
 */
export function offeredNumeraires(options: NumeraireOption[]): NumeraireOption[] {
  return options.filter((n) => hasKey(n) && n.available !== false)
}

/** The ones Go listed and refused, with the reason it gave. Never hidden. */
export function withheldNumeraires(options: NumeraireOption[]): NumeraireOption[] {
  return options.filter((n) => hasKey(n) && n.available === false)
}

/**
 * The English label for a numéraire.
 *
 * Go emits `label_en` and `name_en` carrying the same string (the display
 * contract names the field label_*, the wire has always carried name_*, and
 * numeraires.go now publishes both rather than let a dropdown render
 * `undefined`). Read both, then the key — which always exists. The key is a
 * poor label but it is never empty, and an empty <option> is unpickable.
 */
export function numeraireLabel(option: NumeraireOption): string {
  return option.label_en?.trim() || option.name_en?.trim() || option.key
}

export function numeraireLabelFa(option: NumeraireOption): string {
  return option.label_fa?.trim() || option.name_fa?.trim() || ''
}

/**
 * Which numéraire the page should actually ask for.
 *
 * The rule, in order: the current selection if the server still offers it;
 * else the server's published `default`, but only if IT is offered; else the
 * first AVAILABLE entry. The result is never undefined and never an empty
 * string — with nothing offered at all, the caller's own fallback stands, so
 * the page keeps asking for something the server recognises instead of
 * `numeraire=undefined`.
 */
export function resolveNumeraire(
  selected: string,
  offered: NumeraireOption[],
  preferred: string | null | undefined,
  fallback: string
): string {
  if (offered.length === 0) return selected || fallback
  if (offered.some((n) => n.key === selected)) return selected
  if (preferred && offered.some((n) => n.key === preferred)) return preferred
  return offered[0].key
}

/**
 * What the VALUE column is denominated in.
 *
 * `end_value` is the close of the CONVERTED series, so its unit is the selected
 * numéraire and the instrument's own `quote_currency` has nothing to say about
 * it. Reading quote_currency there printed toman digits under a dollar heading
 * — and then the display-unit toggle multiplied them by ten on the way past.
 *
 * `rialToggleApplies` is the whole point of the discriminant: ×10 is a
 * toman→rial conversion and is a lie about a dollar or a gram.
 */
export interface ValueUnit {
  kind: 'toman' | 'usd' | 'gold' | 'other'
  /** Column-heading form: 'تومان', 'USD', 'g 18k'. */
  short: string
  /** Spelled out for a title attribute: 'grams of 18k gold'. */
  long: string
  rialToggleApplies: boolean
}

/**
 * Resolve the value unit from the numéraire key Go's closed vocabulary uses.
 *
 * `rialLabel` is passed in rather than imported so this stays a pure function
 * of the display setting; `serverUnit` is the numéraire's own `unit` string,
 * used verbatim for any key a later migration adds.
 */
export function valueUnitFor(
  numeraireKey: string,
  rialLabel: string,
  isRialView: boolean,
  serverUnit?: string | null
): ValueUnit {
  switch (numeraireKey) {
    case 'IRT':
      return {
        kind: 'toman',
        short: rialLabel,
        long: isRialView ? 'rial (a display-only ×10 of the stored toman)' : 'toman',
        rialToggleApplies: true
      }
    case 'USD':
      return { kind: 'usd', short: 'USD', long: 'US dollars', rialToggleApplies: false }
    case 'GOLD':
      return {
        kind: 'gold',
        short: 'g 18k',
        long: 'grams of 18k gold',
        rialToggleApplies: false
      }
    default: {
      const unit = serverUnit?.trim() || numeraireKey
      return { kind: 'other', short: unit, long: unit, rialToggleApplies: false }
    }
  }
}
