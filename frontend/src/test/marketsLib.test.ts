import { describe, expect, it } from 'vitest'
import {
  DOMAIN_LABELS,
  compareDomains,
  compareNullable,
  compareText,
  domainLabel,
  domainsPresent,
  finiteOrNull,
  groupByDomain,
  isRelativeValueLeg,
  noteText,
  numeraireLabel,
  offeredNumeraires,
  rankBy,
  resolveNumeraire,
  tierClass,
  tierLabel,
  valueUnitFor,
  withheldNumeraires
} from '../lib/markets'
import type { NumeraireOption } from '../api/types'
// The router source, so the two P1 pages cannot be built and left unreachable.
import appSource from '../App.tsx?raw'

describe('finiteOrNull never manufactures a zero', () => {
  it('passes finite numbers through, including a genuine zero', () => {
    expect(finiteOrNull(0)).toBe(0)
    expect(finiteOrNull(-18.4)).toBe(-18.4)
  })

  it('turns every non-measurement into null rather than 0', () => {
    for (const value of [null, undefined, Number.NaN, Infinity, -Infinity, '', '0', {}, []]) {
      expect(finiteOrNull(value)).toBeNull()
    }
  })
})

describe('compareNullable keeps un-measured rows off the number line', () => {
  it('sorts nulls last in BOTH directions', () => {
    const values: Array<number | null> = [5, null, -2, 41]
    expect(values.slice().sort((a, b) => compareNullable(a, b, 'asc'))).toEqual([-2, 5, 41, null])
    expect(values.slice().sort((a, b) => compareNullable(a, b, 'desc'))).toEqual([41, 5, -2, null])
  })

  it('does not rank a null as if it were zero', () => {
    // If null were 0 it would sit between -2 and 5 ascending.
    expect(compareNullable(null, -2, 'asc')).toBeGreaterThan(0)
    expect(compareNullable(null, 5, 'asc')).toBeGreaterThan(0)
    expect(compareNullable(0, null, 'asc')).toBeLessThan(0)
  })

  it('treats two nulls as tied', () => {
    expect(compareNullable(null, null, 'asc')).toBe(0)
    expect(compareNullable(null, null, 'desc')).toBe(0)
  })
})

describe('compareText', () => {
  it('reverses on descending', () => {
    expect(['b', 'a', 'c'].slice().sort((x, y) => compareText(x, y, 'asc'))).toEqual(['a', 'b', 'c'])
    expect(['b', 'a', 'c'].slice().sort((x, y) => compareText(x, y, 'desc'))).toEqual([
      'c',
      'b',
      'a'
    ])
  })
})

describe('rankBy', () => {
  const rows = [
    { code: 'A', real: 41 },
    { code: 'B', real: null as number | null },
    { code: 'C', real: 5 }
  ]

  it('ignores rows without the metric and reports how many had it', () => {
    const best = rankBy(rows, (r) => r.real, 'highest')
    expect(best?.item.code).toBe('A')
    expect(best?.value).toBe(41)
    expect(best?.measured).toBe(2)
  })

  it('finds the lowest measured value, not the null', () => {
    const worst = rankBy(rows, (r) => r.real, 'lowest')
    expect(worst?.item.code).toBe('C')
    expect(worst?.value).toBe(5)
  })

  it('returns null when nothing was measured, so nothing is crowned', () => {
    expect(rankBy([{ real: null as number | null }], (r) => r.real, 'highest')).toBeNull()
    expect(rankBy([], (r: { real: number | null }) => r.real, 'lowest')).toBeNull()
  })
})

describe('quality tiers render as a word, not only a colour', () => {
  it('spells the tier out with underscores removed', () => {
    expect(tierLabel('official_mirror')).toBe('official mirror')
    expect(tierLabel(null)).toBe('unknown')
  })

  it('never gives a proxy or an estimate the healthy treatment', () => {
    expect(tierClass('official')).toBe('badge-ok')
    expect(tierClass('proxy')).toBe('badge-warn')
    expect(tierClass('estimate')).toBe('badge-warn')
    expect(tierClass('experimental')).toBe('badge-warn')
    expect(tierClass('who_knows')).toBe('badge-off')
  })
})

describe('noteText', () => {
  it('joins the reasons and stays empty when there are none', () => {
    expect(noteText(['one', 'two'])).toBe('one · two')
    expect(noteText([])).toBe('')
    expect(noteText(null)).toBe('')
    expect(noteText(undefined)).toBe('')
  })
})

describe('both P1 pages are reachable', () => {
  it('is routed', () => {
    expect(appSource).toContain('<Route path="/markets" element={<Markets />} />')
    expect(appSource).toContain('<Route path="/relative-value" element={<RelativeValue />} />')
  })

  it('is in the navigation', () => {
    expect(appSource).toContain("{ to: '/markets', label: 'Purchasing power' },")
    expect(appSource).toContain("{ to: '/relative-value', label: 'Relative value' },")
  })
})

// --- numéraire resolution ----------------------------------------------------
//
// The page reads `key`, `series` and `notes[]`. It used to read `code`,
// `series_code` and a scalar `note`, matched nothing, and pinned itself on
// `undefined ?? offered[0].code` — which is how it spent a session asking the
// server for `numeraire=undefined`.

/** Shaped exactly like relvalue.numeraireItem. See Markets.test.tsx for why. */
function option(key: string, over: Partial<NumeraireOption> = {}): NumeraireOption {
  return {
    key,
    unit: key === 'IRT' ? 'toman' : key === 'USD' ? 'USD' : 'grams of 18k gold',
    name_en: key,
    name_fa: key,
    label_en: key,
    label_fa: key,
    series: key === 'IRT' ? null : `${key}_SERIES`,
    quality_tier: null,
    is_proxy: null,
    coverage_from: null,
    coverage_to: null,
    observations: 0,
    notes: [],
    available: true,
    unavailable_reason: null,
    is_identity: key === 'IRT',
    ...over
  }
}

describe('offeredNumeraires gates the menu on what the server can back', () => {
  it('keeps available entries and drops the withheld ones', () => {
    const items = [option('IRT'), option('USD'), option('GOLD', { available: false })]
    expect(offeredNumeraires(items).map((n) => n.key)).toEqual(['IRT', 'USD'])
    expect(withheldNumeraires(items).map((n) => n.key)).toEqual(['GOLD'])
  })

  it('drops an entry with no key, which could only become <option value={undefined}>', () => {
    const items = [option(''), option('USD')]
    expect(offeredNumeraires(items).map((n) => n.key)).toEqual(['USD'])
  })

  it('treats a missing `available` as available rather than hiding the entry', () => {
    const items = [{ key: 'IRT' } as NumeraireOption]
    expect(offeredNumeraires(items).map((n) => n.key)).toEqual(['IRT'])
  })
})

describe('numeraireLabel survives either spelling of the label', () => {
  it('prefers label_en, accepts name_en, and never returns an empty string', () => {
    expect(numeraireLabel(option('USD', { label_en: 'US dollar', name_en: 'dollar' }))).toBe(
      'US dollar'
    )
    expect(numeraireLabel(option('USD', { label_en: null, name_en: 'US dollar' }))).toBe('US dollar')
    expect(numeraireLabel(option('USD', { label_en: null, name_en: null }))).toBe('USD')
    expect(numeraireLabel(option('USD', { label_en: '  ', name_en: '' }))).toBe('USD')
  })
})

describe('resolveNumeraire never returns undefined', () => {
  const offered = [option('IRT'), option('USD'), option('GOLD')]

  it('keeps a selection the server still offers', () => {
    expect(resolveNumeraire('GOLD', offered, 'IRT', 'IRT')).toBe('GOLD')
  })

  it("takes the server's default when the selection is not offered", () => {
    expect(resolveNumeraire('EUR', offered, 'USD', 'IRT')).toBe('USD')
  })

  it('ignores a default the server does not offer, and takes the first available', () => {
    expect(resolveNumeraire('EUR', offered.slice(1), 'IRT', 'IRT')).toBe('USD')
  })

  it('falls back to the first available when there is no default at all', () => {
    expect(resolveNumeraire('EUR', offered.slice(1), null, 'IRT')).toBe('USD')
    expect(resolveNumeraire('EUR', offered.slice(1), undefined, 'IRT')).toBe('USD')
  })

  it('holds the caller’s own fallback while the registry has answered nothing', () => {
    expect(resolveNumeraire('IRT', [], 'GOLD', 'IRT')).toBe('IRT')
    expect(resolveNumeraire('', [], null, 'IRT')).toBe('IRT')
  })

  it('is never undefined, whatever it is handed', () => {
    for (const selected of ['', 'EUR', 'IRT']) {
      for (const list of [[], offered, offered.slice(2)]) {
        for (const preferred of [null, undefined, '', 'EUR', 'IRT']) {
          const got = resolveNumeraire(selected, list, preferred, 'IRT')
          expect(typeof got).toBe('string')
          expect(got.length).toBeGreaterThan(0)
          expect(got).not.toBe('undefined')
        }
      }
    }
  })
})

describe('valueUnitFor decides where the ×10 rial conversion may fire', () => {
  it('applies the toggle to toman and to nothing else', () => {
    expect(valueUnitFor('IRT', 'تومان', false).rialToggleApplies).toBe(true)
    expect(valueUnitFor('IRT', 'ریال', true).rialToggleApplies).toBe(true)
    expect(valueUnitFor('USD', 'ریال', true).rialToggleApplies).toBe(false)
    expect(valueUnitFor('GOLD', 'ریال', true).rialToggleApplies).toBe(false)
  })

  it('names the unit the column is actually in', () => {
    expect(valueUnitFor('IRT', 'ریال', true).short).toBe('ریال')
    expect(valueUnitFor('USD', 'تومان', false).short).toBe('USD')
    expect(valueUnitFor('GOLD', 'تومان', false).long).toBe('grams of 18k gold')
  })

  it('uses the server’s own unit string for a key this build has never seen', () => {
    const unit = valueUnitFor('XAU', 'تومان', false, 'troy ounces of fine gold')
    expect(unit.kind).toBe('other')
    expect(unit.short).toBe('troy ounces of fine gold')
    expect(unit.rialToggleApplies).toBe(false)
  })
})

describe('instrument domains', () => {
  it('names every domain the registry and the equity table use', () => {
    expect(domainLabel('gold')).toBe('Gold & coins')
    expect(domainLabel('silver')).toBe('Silver')
    expect(domainLabel('fund')).toBe('Commodity funds')
    expect(domainLabel('fx')).toBe('Currency')
    expect(domainLabel('global')).toBe('Global markets')
    expect(domainLabel('ir_equity')).toBe('Tehran equities')
    expect(Object.keys(DOMAIN_LABELS)).toEqual(['gold', 'silver', 'fund', 'fx', 'global', 'ir_equity'])
  })

  it('falls back to the raw domain rather than hiding an unnamed one', () => {
    expect(domainLabel('saffron')).toBe('saffron')
    expect(domainLabel(' housing ')).toBe('housing')
    expect(domainLabel('')).toBe('Other')
    expect(domainLabel(null)).toBe('Other')
  })

  it('orders known domains first, in their fixed order, then the rest alphabetically', () => {
    const domains = ['macro', 'global', 'saffron', 'gold', 'fx', 'silver', 'fund', 'ir_equity']
    expect(domains.slice().sort(compareDomains)).toEqual([
      'gold',
      'silver',
      'fund',
      'fx',
      'global',
      'ir_equity',
      'macro',
      'saffron'
    ])
  })

  it('groups rows without reordering them inside a group, and drops none', () => {
    const rows = [
      { code: 'USD_IRT', domain: 'fx' },
      { code: 'IR_GOLD_18K', domain: 'gold' },
      { code: 'IR_SILVER_999', domain: 'silver' },
      { code: 'IR_COIN_EMAMI', domain: 'gold' },
      { code: 'NO_DOMAIN', domain: '' }
    ]
    const groups = groupByDomain(rows)
    expect(groups.map((g) => [g.label, g.items.map((i) => i.code)])).toEqual([
      ['Gold & coins', ['IR_GOLD_18K', 'IR_COIN_EMAMI']],
      ['Silver', ['IR_SILVER_999']],
      ['Currency', ['USD_IRT']],
      ['Other', ['NO_DOMAIN']]
    ])
    expect(domainsPresent(rows)).toEqual(['gold', 'silver', 'fx', ''])
  })
})

describe('isRelativeValueLeg mirrors what /relative-value can pair', () => {
  it('offers prices, fx and index levels', () => {
    expect(isRelativeValueLeg({ kind: 'market_price', quote_currency: 'IRT', enabled: true })).toBe(true)
    expect(isRelativeValueLeg({ kind: 'fx', quote_currency: 'IRT', enabled: true })).toBe(true)
    // Indexing both legs to 100 cancels units, so an index level is a fair leg.
    expect(isRelativeValueLeg({ kind: 'index', quote_currency: 'INDEX', enabled: true })).toBe(true)
  })

  it('refuses a rate, an economic series and a disabled row', () => {
    expect(isRelativeValueLeg({ kind: 'market_price', quote_currency: 'PCT', enabled: true })).toBe(false)
    expect(isRelativeValueLeg({ kind: 'index', quote_currency: 'PCT', enabled: true })).toBe(false)
    expect(isRelativeValueLeg({ kind: 'economic_series', quote_currency: 'INDEX', enabled: true })).toBe(
      false
    )
    expect(isRelativeValueLeg({ kind: 'market_price', quote_currency: 'IRT', enabled: false })).toBe(false)
  })
})
