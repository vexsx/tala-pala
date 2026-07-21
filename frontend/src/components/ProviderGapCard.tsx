import { useMemo } from 'react'
import {
  CartesianGrid,
  ComposedChart,
  Line,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis
} from 'recharts'
import { useApi } from '../hooks/useApi'
import type { ProviderGapResponse } from '../api/types'
import { useSettings } from '../lib/settings'
import { formatPct, formatToman, shortDate } from '../lib/format'
import { ChartTip } from './PriceChart'
import Loading from './Loading'
import ErrorMessage from './ErrorMessage'
import EmptyState from './EmptyState'

/**
 * Dispersion between Iranian providers quoting the 18k gram price. A wide gap
 * means the "current price" is genuinely ambiguous — treat signals and
 * forecasts with more caution (the backend widens intervals accordingly).
 */
export default function ProviderGapCard() {
  const { unit, calendar } = useSettings()
  const gap = useApi<ProviderGapResponse>('/market/provider-gap?symbol=IR_GOLD_18K&history_days=30')

  const history = useMemo(
    () =>
      (gap.data?.history ?? []).map((p) => ({
        label: shortDate(p.date, calendar),
        gap: p.gap_pct
      })),
    [gap.data, calendar]
  )

  if (gap.loading) return <Loading label="Loading provider gap…" />

  const gapPct = gap.data?.gap_pct ?? null
  const wide = gapPct !== null && gapPct >= 1

  return (
    <div className="card">
      <div className="card-title">Iranian provider gap (18k gram)</div>
      {gap.error ? (
        <ErrorMessage message={gap.error} onRetry={gap.reload} />
      ) : (
        <>
          <div className="kv">
            <span className="muted">Current gap</span>
            <span className={`mono ${wide ? 'neg' : ''}`}>
              {gapPct !== null ? formatPct(gapPct) : '— (fewer than 2 sources fresh)'}
            </span>
          </div>
          {wide && (
            <div className="callout callout-warn">
              Providers disagree materially right now — the &ldquo;current price&rdquo; is
              ambiguous and forecast intervals are widened to match.
            </div>
          )}
          {(gap.data?.providers ?? []).length > 0 && (
            <div className="table-wrap">
              <table className="table">
                <thead>
                  <tr>
                    <th>Provider</th>
                    <th className="num">Quote</th>
                  </tr>
                </thead>
                <tbody>
                  {(gap.data?.providers ?? [])
                    .slice()
                    .sort((a, b) => b.value - a.value)
                    .map((q) => (
                      <tr key={q.provider}>
                        <td className="mono">{q.provider}</td>
                        <td className="num mono">{formatToman(q.value, unit)}</td>
                      </tr>
                    ))}
                </tbody>
              </table>
            </div>
          )}
          {history.length >= 2 ? (
            <div className="chart-box" style={{ height: 160 }}>
              <ResponsiveContainer width="100%" height="100%">
                <ComposedChart data={history} margin={{ top: 8, right: 16, bottom: 4, left: 8 }}>
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
                    width={44}
                    tickLine={false}
                    axisLine={{ stroke: 'var(--border)' }}
                    tickFormatter={(v: number) => `${v.toFixed(1)}%`}
                  />
                  <Tooltip content={<ChartTip format={(v) => `${v.toFixed(2)}%`} />} />
                  <Line
                    type="monotone"
                    dataKey="gap"
                    name="daily gap"
                    stroke="var(--warn)"
                    strokeWidth={1.5}
                    dot={false}
                    isAnimationActive={false}
                    connectNulls
                  />
                </ComposedChart>
              </ResponsiveContainer>
            </div>
          ) : (
            <EmptyState
              title="No gap history yet"
              hint="Needs at least two providers quoting the same day."
            />
          )}
        </>
      )}
    </div>
  )
}
