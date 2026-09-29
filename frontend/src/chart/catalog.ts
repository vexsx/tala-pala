import { useEffect, useMemo } from 'react'
import { useApi } from '../hooks/useApi'
import type {
  BourseIndexItem,
  BourseIndicesResponse,
  InstrumentItem,
  StockRosterItem,
  StocksResponse,
  Symbol_
} from '../api/types'
import { SYMBOL_LABELS } from '../api/types'
import { unwrapList } from '../lib/unwrap'
import { KIND_LABEL } from '../lib/bourse'
import { CHART_SYMBOLS } from './prefs'
import { registerRegistryQuotes, TSE_INDEX, type ChartSymbol, type SymbolKind } from './symbols'

/**
 * What the symbol picker offers, and in which groups.
 *
 * Three lists feed it — the instrument registry, the Tehran index registry and
 * the equity roster — and none of them is waited for: the static groups below
 * render on the first paint, so the gold chart and TEDPIX are one click away
 * while the lists load, and stay when a list fails.
 *
 * An entry that cannot be charted is SHOWN, disabled, with the reason — an
 * index whose correction was refused, a share whose adjustment was not
 * validated. Hiding it would make "refused" look like "never existed".
 */

export interface ChartSymbolOption {
  symbol: ChartSymbol
  /** The option text. */
  label: string
  /** What the chart header calls it. */
  short: string
  kind: SymbolKind
  /** Why this entry cannot be charted; absent when it can. */
  disabled?: string
  /** Longer provenance for the option's tooltip. */
  title?: string
}

export interface ChartSymbolGroup {
  id: string
  label: string
  options: ChartSymbolOption[]
}

export interface CatalogSources {
  instruments: InstrumentItem[] | null
  indices: BourseIndexItem[] | null
  stocks: StockRosterItem[] | null
}

// ---------------------------------------------------------------------------
// The registry commodities
// ---------------------------------------------------------------------------

/** Registry domains in picker order, with their group names. */
const DOMAIN_GROUPS: Array<{ domain: string; label: string }> = [
  { domain: 'gold', label: 'Gold & coins' },
  { domain: 'silver', label: 'Silver' },
  { domain: 'fund', label: 'Commodity funds' },
  { domain: 'fx', label: 'FX' },
  { domain: 'global', label: 'Global' }
]

/**
 * The registry rows the candles API draws from `prices` — the frontend twin of
 * chartableRegistryRow in backend-go/internal/prices/chart_symbols.go. A flow
 * ratio, a yield or a monthly economic series is not a price and is not
 * offered as one.
 */
export function isChartableInstrument(item: InstrumentItem): boolean {
  return (
    item.enabled &&
    (item.kind === 'market_price' || item.kind === 'fx') &&
    (item.quote_currency === 'IRT' || item.quote_currency === 'USD')
  )
}

function instrumentOption(item: InstrumentItem): ChartSymbolOption {
  const name = item.name_en || item.code
  // USD_IRT is the USDT/toman market, a documented proxy; say so on the
  // option when the name itself does not.
  const proxy = item.is_proxy && !/proxy/i.test(name) ? ' (proxy)' : ''
  return {
    symbol: item.code,
    label: `${name}${proxy}${item.name_fa ? ` — ${item.name_fa}` : ''}`,
    short: SYMBOL_LABELS[item.code as Symbol_] ?? name,
    kind: 'ticks',
    title: item.notes || undefined
  }
}

/** The two symbols that are always offered, whatever the registry says. */
const STATIC_TICKS: Record<string, { domain: string; option: ChartSymbolOption }> = {
  IR_GOLD_18K: {
    domain: 'gold',
    option: { symbol: 'IR_GOLD_18K', label: SYMBOL_LABELS.IR_GOLD_18K, short: SYMBOL_LABELS.IR_GOLD_18K, kind: 'ticks' }
  },
  XAUUSD: {
    domain: 'global',
    option: { symbol: 'XAUUSD', label: SYMBOL_LABELS.XAUUSD, short: SYMBOL_LABELS.XAUUSD, kind: 'ticks' }
  }
}

function tickGroups(instruments: InstrumentItem[] | null): ChartSymbolGroup[] {
  const byDomain = new Map<string, ChartSymbolOption[]>()
  const push = (domain: string, option: ChartSymbolOption) => {
    const list = byDomain.get(domain) ?? []
    list.push(option)
    byDomain.set(domain, list)
  }
  const seen = new Set<string>()
  for (const item of (instruments ?? []).filter(isChartableInstrument)) {
    push(item.domain, instrumentOption(item))
    seen.add(item.code)
  }
  for (const code of CHART_SYMBOLS) {
    if (!seen.has(code)) push(STATIC_TICKS[code].domain, STATIC_TICKS[code].option)
  }

  const known = new Set(DOMAIN_GROUPS.map((d) => d.domain))
  const order = [
    ...DOMAIN_GROUPS,
    // A domain added to the registry later still charts, under its own name.
    ...Array.from(byDomain.keys())
      .filter((d) => !known.has(d))
      .sort()
      .map((d) => ({ domain: d, label: d }))
  ]
  const groups: ChartSymbolGroup[] = []
  for (const { domain, label } of order) {
    const options = byDomain.get(domain)
    if (options && options.length > 0) groups.push({ id: `domain:${domain}`, label, options })
  }
  return groups
}

// ---------------------------------------------------------------------------
// The Tehran market
// ---------------------------------------------------------------------------

/**
 * The headline indices, offered before /bourse/indices answers. Names as
 * migration 0029 registers them; the list replaces these once it arrives,
 * with each index's own verdict.
 */
const STATIC_HEADLINES: ChartSymbolOption[] = [
  { symbol: TSE_INDEX.TEDPIX, name: 'TEDPIX, all-share (price and dividends)', fa: 'شاخص کل', short: 'TEDPIX' },
  { symbol: TSE_INDEX.EQUAL_WEIGHTED, name: 'Equal-weighted all-share', fa: 'شاخص کل (هم وزن)', short: 'Equal-weighted' },
  { symbol: TSE_INDEX.TEPIX, name: 'TEPIX, all-share price index', fa: 'شاخص قیمت (وزنی-ارزشی)', short: 'TEPIX' },
  {
    symbol: TSE_INDEX.EQUAL_WEIGHTED_PRICE,
    name: 'Equal-weighted price index',
    fa: 'شاخص قیمت (هم وزن)',
    short: 'Equal-weighted price'
  },
  { symbol: TSE_INDEX.IFX, name: 'IFX, Farabourse all-share', fa: 'شاخص کل فرابورس', short: 'IFX' }
].map((s) => ({ symbol: s.symbol, label: `${s.name} — ${s.fa}`, short: s.short, kind: 'tse_index' as const }))

/** Why an index cannot be charted, or undefined when it can. */
function indexRefusal(item: BourseIndexItem): { short: string; full: string } | undefined {
  const status = item.check?.status
  if (status === 'never_ingested') {
    return { short: 'never ingested', full: 'This index has never been ingested.' }
  }
  if (status !== 'validated') {
    const reason = item.check?.refusal_reason
    return {
      short: 'correction refused',
      full: `The index's correction was refused by its check${reason ? `: ${reason}` : ''}.`
    }
  }
  if (!item.last) return { short: 'no stored session', full: 'No session of this index is stored.' }
  return undefined
}

function indexOption(item: BourseIndexItem): ChartSymbolOption {
  const sector = item.kind === 'sector' && item.sector_code ? `${item.sector_code} · ` : ''
  const refusal = indexRefusal(item)
  const base = `${sector}${item.name_en || item.ins_code} — ${item.name_fa}`
  return {
    symbol: `IDX:${item.ins_code}`,
    label: refusal ? `${base} (${refusal.short})` : base,
    short: item.name_en || item.name_fa,
    kind: 'tse_index',
    disabled: refusal?.full,
    title: refusal?.full ?? item.notes
  }
}

function tehranIndexGroups(indices: BourseIndexItem[] | null): ChartSymbolGroup[] {
  if (indices === null) {
    return [{ id: 'tse:headline', label: `Tehran · ${KIND_LABEL.headline}`, options: STATIC_HEADLINES }]
  }
  const byOrder = [...indices].sort((a, b) => a.display_order - b.display_order)
  const pick = (f: (i: BourseIndexItem) => boolean) => byOrder.filter(f).map(indexOption)
  return [
    { id: 'tse:headline', label: `Tehran · ${KIND_LABEL.headline}`, options: pick((i) => i.kind === 'headline') },
    {
      id: 'tse:boards',
      label: 'Tehran · Boards & segments',
      options: pick((i) => i.kind === 'market' || i.kind === 'segment')
    },
    {
      id: 'tse:bourse-sectors',
      label: `Bourse · ${KIND_LABEL.sector}`,
      options: pick((i) => i.kind === 'sector' && i.market === 'bourse')
    },
    {
      id: 'tse:farabourse-sectors',
      label: `Farabourse · ${KIND_LABEL.sector}`,
      options: pick((i) => i.kind === 'sector' && i.market === 'farabourse')
    }
  ].filter((g) => g.options.length > 0)
}

/**
 * Why a share cannot be charted, or undefined when it can. The adjustment
 * verdict comes first: it is the measured reason (کچاد is disabled BECAUSE its
 * adjustment is refused), and the roster flag only says an operator agreed.
 */
function shareRefusal(item: StockRosterItem): { short: string; full: string } | undefined {
  const adj = item.adjustment
  if (!adj?.adjusted_servable) {
    if (!adj || adj.status === 'never_ingested') {
      return { short: 'never ingested', full: 'This share has never been ingested.' }
    }
    return {
      short: 'adjustment not validated',
      full: `Its corporate-action adjustment was not validated${
        adj.refusal_reason ? `: ${adj.refusal_reason}` : ''
      }, so no adjusted series is served.`
    }
  }
  if (!item.enabled) return { short: 'disabled', full: 'Disabled in the roster.' }
  if (item.bar_count === 0) return { short: 'no stored bars', full: 'No session of this share is stored.' }
  return undefined
}

function shareOption(item: StockRosterItem): ChartSymbolOption {
  const refusal = shareRefusal(item)
  const base = `${item.symbol} — ${item.name_fa}`
  return {
    symbol: `EQ:${item.ins_code}`,
    label: refusal ? `${base} (${refusal.short})` : base,
    short: item.symbol,
    kind: 'tse_equity',
    disabled: refusal?.full,
    title: refusal?.full ?? (item.sector_fa || undefined)
  }
}

function shareGroups(stocks: StockRosterItem[] | null): ChartSymbolGroup[] {
  if (!stocks || stocks.length === 0) return []
  const options = [...stocks].sort((a, b) => a.symbol.localeCompare(b.symbol, 'fa')).map(shareOption)
  return [{ id: 'tse:shares', label: 'Tehran · Shares', options }]
}

/**
 * The picker, group by group. Pure (unit tested): a null source is one that
 * has not answered, and falls back to what can be offered without it.
 */
export function buildCatalog(src: CatalogSources): ChartSymbolGroup[] {
  return [...tickGroups(src.instruments), ...tehranIndexGroups(src.indices), ...shareGroups(src.stocks)]
}

/** Every option in the catalog, keyed by symbol. */
export function catalogIndex(groups: ChartSymbolGroup[]): Map<ChartSymbol, ChartSymbolOption> {
  const out = new Map<ChartSymbol, ChartSymbolOption>()
  for (const g of groups) for (const o of g.options) out.set(o.symbol, o)
  return out
}

/** What the picker renders before anything has answered. */
export const STATIC_CATALOG: ChartSymbolGroup[] = buildCatalog({
  instruments: null,
  indices: null,
  stocks: null
})

export interface ChartCatalog {
  groups: ChartSymbolGroup[]
  find: (symbol: ChartSymbol) => ChartSymbolOption | null
}

export function useChartCatalog(): ChartCatalog {
  const instruments = useApi<unknown>('/instruments?enabled=true')
  const indices = useApi<BourseIndicesResponse>('/bourse/indices')
  const stocks = useApi<StocksResponse>('/stocks')

  const registry = useMemo(
    () => (instruments.data ? unwrapList<InstrumentItem>(instruments.data, 'items') : null),
    [instruments.data]
  )
  useEffect(() => {
    if (registry) registerRegistryQuotes(registry)
  }, [registry])

  const groups = useMemo(
    () =>
      buildCatalog({
        instruments: registry,
        indices: indices.data?.items ?? null,
        stocks: stocks.data?.items ?? null
      }),
    [registry, indices.data, stocks.data]
  )
  const index = useMemo(() => catalogIndex(groups), [groups])
  return { groups, find: (symbol) => index.get(symbol) ?? null }
}
