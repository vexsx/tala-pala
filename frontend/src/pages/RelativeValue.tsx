import { useMemo, useState } from 'react'
import {
  CartesianGrid,
  ComposedChart,
  Line,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis
} from 'recharts'
import { useApi } from '../hooks/useApi'
import {
  MARKET_PERIODS,
  MARKET_PERIOD_LABELS,
  SYMBOLS,
  SYMBOL_LABELS,
  type InstrumentItem,
  type MarketPeriod,
  type PercentileBasis,
  type RelativeValueLeg,
  type RelativeValueResponse
} from '../api/types'
import { unwrapList } from '../lib/unwrap'
import { useSettings } from '../lib/settings'
import { finiteOrNull } from '../lib/markets'
import { formatDate, formatDateTime, formatPct, pctClass, shortDate } from '../lib/format'
import { ChartTip } from '../components/PriceChart'
import Provenance from '../components/Provenance'
import Loading from '../components/Loading'
import ErrorMessage from '../components/ErrorMessage'
import EmptyState from '../components/EmptyState'

interface AssetOption {
  code: string
  label: string
}

const DEFAULT_A = 'IR_GOLD_18K'
const DEFAULT_B = 'USD_IRT'

function legName(leg: RelativeValueLeg | undefined, fallback: string): string {
  return leg?.name_en || leg?.code || fallback
}

/**
 * The headline, in the research register the redesign brief mandates: a
 * measurement of what HAS happened over a stated window. The gap is a
 * description of the past, and this page never turns it into a claim about
 * what happens next.
 */
function gapSentence(
  gap: number | null,
  aName: string,
  bName: string
): { text: string; tone: string } {
  if (gap === null) {
    return {
      text: `No gap is reported between ${aName} and ${bName} over this window.`,
      tone: 'flat'
    }
  }
  const magnitude = formatPct(Math.abs(gap), { sign: false })
  if (Math.abs(gap) < 0.005) {
    return {
      text: `${aName} and ${bName} have grown by the same amount over this window (${magnitude} apart).`,
      tone: 'flat'
    }
  }
  if (gap < 0) {
    return { text: `${aName} has lagged ${bName} by ${magnitude} over this window.`, tone: 'neg' }
  }
  return { text: `${aName} has led ${bName} by ${magnitude} over this window.`, tone: 'pos' }
}

/**
 * The percentile and its evidence, together or not at all.
 *
 * Rolling windows over a single price history overlap, so a count of them is
 * not a count of observations. When the gate on INDEPENDENT windows does not
 * pass, the shortfall is stated in words and no number is shown — hiding the
 * field would leave a reader assuming it had simply not been computed.
 */
function PercentileEvidence({
  percentile,
  basis
}: {
  percentile: number | null
  basis: PercentileBasis | null
}) {
  if (!basis) {
    return (
      <div className="rv-percentile" data-testid="rv-percentile">
        <div className="rv-percentile-head">Percentile not reported</div>
        <div className="muted small">
          The response carried no percentile basis, so there is nothing to rank this gap against.
        </div>
      </div>
    )
  }

  const independent = finiteOrNull(basis.independent_windows)
  const required = finiteOrNull(basis.min_independent_windows)
  const overlapping = finiteOrNull(basis.overlapping_windows)
  const windowDays = finiteOrNull(basis.window_days)

  const evidence = (
    <ul className="rv-basis-list">
      {windowDays !== null && <li>Rolling window: {windowDays} days.</li>}
      {overlapping !== null && <li>{overlapping} overlapping windows in the pair's history.</li>}
      {independent !== null && (
        <li>
          {independent} independent window{independent === 1 ? '' : 's'}
          {required !== null ? `; ${required} needed` : ''}.
        </li>
      )}
      {basis.note && <li>{basis.note}</li>}
    </ul>
  )

  if (basis.sufficient && percentile !== null) {
    return (
      <div className="rv-percentile" data-testid="rv-percentile">
        <div className="rv-percentile-head">
          This gap sits at percentile <span className="mono">{percentile.toFixed(1)}</span> of the
          pair's own history.
        </div>
        {evidence}
      </div>
    )
  }

  const shortfall =
    independent !== null && required !== null
      ? `${independent} independent window${independent === 1 ? '' : 's'}; ${required} needed.`
      : 'the response did not report how many independent windows it had.'

  return (
    <div className="rv-percentile rv-percentile-insufficient" data-testid="rv-percentile">
      <div className="rv-percentile-head">Percentile not reported: {shortfall}</div>
      <div className="muted small">
        {basis.sufficient
          ? 'The evidence gate passed but no percentile accompanied it, so none is shown.'
          : 'The evidence gate did not pass, so no percentile is shown in its place.'}
      </div>
      {evidence}
    </div>
  )
}

export default function RelativeValue() {
  const { calendar } = useSettings()
  const [a, setA] = useState<string>(DEFAULT_A)
  const [b, setB] = useState<string>(DEFAULT_B)
  const [period, setPeriod] = useState<MarketPeriod>('1y')

  // The symbol vocabulary. When the registry is unavailable the page falls back
  // to the symbol list the app already ships rather than to an empty selector.
  const instruments = useApi<unknown>('/instruments?enabled=true')
  const options = useMemo<AssetOption[]>(() => {
    const items = unwrapList<InstrumentItem>(instruments.data, 'items', 'instruments')
    if (items.length > 0) {
      return items.map((i) => ({ code: i.code, label: i.name_en || i.code }))
    }
    return SYMBOLS.map((code) => ({ code, label: SYMBOL_LABELS[code] }))
  }, [instruments.data])

  const samePair = a === b
  const rv = useApi<RelativeValueResponse>(
    samePair
      ? null
      : `/relative-value?a=${encodeURIComponent(a)}&b=${encodeURIComponent(b)}&period=${encodeURIComponent(period)}`
  )

  const data = rv.data
  const aName = legName(data?.a, a)
  const bName = legName(data?.b, b)
  const gap = finiteOrNull(data?.gap_pct)
  const headline = gapSentence(gap, aName, bName)

  const chartData = useMemo(
    () =>
      (data?.series ?? []).map((p) => ({
        label: shortDate(new Date(p.t * 1000), calendar),
        a: p.a_indexed,
        b: p.b_indexed
      })),
    [data, calendar]
  )

  const warnings = data?.warnings ?? []

  return (
    <div className="page-body">
      <h2 className="page-title">Relative value</h2>
      <p className="muted small rv-preamble">
        Two assets indexed to 100 at a shared base date, and the growth gap between them over the
        window. A gap is a measurement of what has already happened; this page reports it and its
        evidence, and draws no conclusion about what follows.
      </p>

      <div className="row wrap rv-controls">
        <div className="field">
          <label htmlFor="rv-a">Asset A</label>
          <select id="rv-a" value={a} onChange={(e) => setA(e.target.value)}>
            {options.map((o) => (
              <option key={o.code} value={o.code}>
                {o.label}
              </option>
            ))}
          </select>
        </div>
        <div className="field">
          <label htmlFor="rv-b">Asset B</label>
          <select id="rv-b" value={b} onChange={(e) => setB(e.target.value)}>
            {options.map((o) => (
              <option key={o.code} value={o.code}>
                {o.label}
              </option>
            ))}
          </select>
        </div>
        <div className="field">
          <span className="field-label">Window</span>
          <div className="chip-row" role="group" aria-label="Window">
            {MARKET_PERIODS.map((p) => (
              <button
                key={p}
                type="button"
                className={`chip ${period === p ? 'active' : ''}`}
                aria-pressed={period === p}
                title={MARKET_PERIOD_LABELS[p]}
                onClick={() => setPeriod(p)}
              >
                {p}
              </button>
            ))}
          </div>
        </div>
      </div>

      {samePair ? (
        <EmptyState
          title="Pick two different assets"
          hint="A pair compared against itself has no gap to measure."
        />
      ) : (
        <>
          {rv.error && <ErrorMessage message={rv.error} onRetry={rv.reload} />}

          {warnings.map((w, i) => (
            <div key={i} className="callout callout-warn">
              {w}
            </div>
          ))}

          {rv.loading && !data ? (
            <Loading label="Loading relative value…" />
          ) : !data ? null : (
            <>
              <div className="card rv-headline-card">
                <div className={`rv-headline ${headline.tone}`} data-testid="rv-headline">
                  {headline.text}
                </div>
                <div className="row wrap rv-legs">
                  <div className="rv-leg">
                    <span className="rv-leg-tag">A</span>
                    <span className="rv-leg-name">{aName}</span>
                    {data.a?.name_fa && (
                      <span className="bidi-fa" lang="fa" dir="rtl">
                        {data.a.name_fa}
                      </span>
                    )}
                    <span className="mono muted small">
                      {data.a?.code}
                      {data.a?.unit ? ` · ${data.a.unit}` : ''}
                    </span>
                    <Provenance tier={data.a?.quality_tier} isProxy={data.a?.is_proxy} />
                    <span className={`mono ${pctClass(finiteOrNull(data.a_growth_pct))}`}>
                      {formatPct(finiteOrNull(data.a_growth_pct))}
                    </span>
                  </div>
                  <div className="rv-leg">
                    <span className="rv-leg-tag">B</span>
                    <span className="rv-leg-name">{bName}</span>
                    {data.b?.name_fa && (
                      <span className="bidi-fa" lang="fa" dir="rtl">
                        {data.b.name_fa}
                      </span>
                    )}
                    <span className="mono muted small">
                      {data.b?.code}
                      {data.b?.unit ? ` · ${data.b.unit}` : ''}
                    </span>
                    <Provenance tier={data.b?.quality_tier} isProxy={data.b?.is_proxy} />
                    <span className={`mono ${pctClass(finiteOrNull(data.b_growth_pct))}`}>
                      {formatPct(finiteOrNull(data.b_growth_pct))}
                    </span>
                  </div>
                </div>
                <div className="muted small">
                  Indexed to 100 at {formatDate(data.base_date, calendar)} ·{' '}
                  {formatDate(data.from, calendar)} → {formatDate(data.to, calendar)}
                  {data.as_of ? ` · as of ${formatDateTime(data.as_of, calendar)}` : ''}
                </div>
              </div>

              <div className="card">
                <div className="card-title">Indexed growth (both series = 100 at the base date)</div>
                {chartData.length === 0 ? (
                  <EmptyState
                    title="No overlapping history for this pair"
                    hint="Both series need observations inside the window before they can be indexed together."
                  />
                ) : (
                  <div className="chart-box" style={{ height: 320 }}>
                    <ResponsiveContainer width="100%" height="100%">
                      <ComposedChart
                        data={chartData}
                        margin={{ top: 8, right: 16, bottom: 4, left: 8 }}
                      >
                        <CartesianGrid stroke="var(--border)" strokeDasharray="3 3" vertical={false} />
                        <XAxis
                          dataKey="label"
                          tick={{ fill: 'var(--muted)', fontSize: 11 }}
                          minTickGap={28}
                          tickLine={false}
                          axisLine={{ stroke: 'var(--border)' }}
                        />
                        <YAxis
                          tick={{ fill: 'var(--muted)', fontSize: 11 }}
                          tickFormatter={(v: number) => v.toFixed(0)}
                          width={56}
                          domain={['auto', 'auto']}
                          tickLine={false}
                          axisLine={{ stroke: 'var(--border)' }}
                        />
                        <Tooltip content={<ChartTip format={(v) => v.toFixed(1)} />} />
                        <ReferenceLine y={100} stroke="var(--border)" strokeDasharray="4 4" />
                        <Line
                          type="monotone"
                          dataKey="a"
                          name={aName}
                          stroke="var(--accent)"
                          strokeWidth={2}
                          dot={false}
                          isAnimationActive={false}
                          connectNulls
                        />
                        <Line
                          type="monotone"
                          dataKey="b"
                          name={bName}
                          stroke="var(--info)"
                          strokeWidth={2}
                          dot={false}
                          isAnimationActive={false}
                          connectNulls
                        />
                      </ComposedChart>
                    </ResponsiveContainer>
                  </div>
                )}
              </div>

              <div className="card">
                <div className="card-title">Gap and its evidence</div>
                <div className="kv-list rv-kv">
                  <div className="kv">
                    <span className="muted">Growth gap</span>
                    <span className={`mono ${pctClass(gap)}`}>{formatPct(gap)}</span>
                  </div>
                  <div className="kv">
                    <span className="muted">{aName} growth</span>
                    <span className={`mono ${pctClass(finiteOrNull(data.a_growth_pct))}`}>
                      {formatPct(finiteOrNull(data.a_growth_pct))}
                    </span>
                  </div>
                  <div className="kv">
                    <span className="muted">{bName} growth</span>
                    <span className={`mono ${pctClass(finiteOrNull(data.b_growth_pct))}`}>
                      {formatPct(finiteOrNull(data.b_growth_pct))}
                    </span>
                  </div>
                  <div className="kv">
                    <span className="muted">Ratio drawdown</span>
                    <span className="mono">{formatPct(finiteOrNull(data.ratio_drawdown_pct))}</span>
                  </div>
                </div>
                <PercentileEvidence
                  percentile={finiteOrNull(data.gap_percentile)}
                  basis={data.percentile_basis ?? null}
                />
                <div className="muted small rv-footer">
                  The gap is (1 + {aName} growth) / (1 + {bName} growth) − 1, computed by the backend
                  over the window shown. Nothing on this page is recomputed in the browser.
                </div>
              </div>
            </>
          )}
        </>
      )}
    </div>
  )
}
