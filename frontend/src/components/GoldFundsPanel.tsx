import { useMemo } from 'react'
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
import type { FundsResponse } from '../api/types'
import { SYMBOL_LABELS, type Symbol_ } from '../api/types'
import { useSettings } from '../lib/settings'
import { formatCompact, formatPct, formatToman, pctClass, shortDate } from '../lib/format'
import { ChartTip } from './PriceChart'
import Loading from './Loading'
import ErrorMessage from './ErrorMessage'
import EmptyState from './EmptyState'

/**
 * Tehran-exchange gold funds ("boxes"): prices, volume, and the retail
 * (حقیقی) buyer/seller composition per fund — plus the composite retail
 * net-flow history. Data lands once per hour during the TSE session
 * (12:00–17:00 Tehran, Sat–Wed) to respect the source's request budget.
 */
export default function GoldFundsPanel() {
  const { unit, calendar } = useSettings()
  const res = useApi<FundsResponse>('/market/funds')

  const flowChart = useMemo(
    () =>
      (res.data?.flow_history ?? []).map((p) => ({
        label: shortDate(p.date, calendar),
        flow: p.flow_pct
      })),
    [res.data, calendar]
  )

  if (res.loading) return <Loading label="Loading gold funds…" />
  if (res.error) return <ErrorMessage message={res.error} onRetry={res.reload} />
  const funds = res.data?.funds ?? []
  if (funds.length === 0) {
    return (
      <EmptyState
        title="No fund data yet"
        hint="TSE gold-fund quotes arrive hourly during the trading session (12:00–17:00 Tehran, Sat–Wed)."
      />
    )
  }

  return (
    <>
      <div className="kv">
        <span className="muted">Market</span>
        <span className={res.data?.market_state === 'open' ? 'pos' : 'muted'}>
          {res.data?.market_state === 'open' ? '● open (12:00–17:00 Tehran)' : '○ closed'}
        </span>
      </div>
      <div className="table-wrap">
        <table className="table">
          <thead>
            <tr>
              <th>Fund</th>
              <th className="num">Price</th>
              <th className="num">Δ day</th>
              <th className="num">Volume</th>
              <th className="num">Retail buys</th>
              <th className="num">Retail sells</th>
              <th className="num">Today avg B/S</th>
              <th className="num" title="Per-capita retail buy vs sell volume — above 1 means buyers are more eager">
                Buyer power
              </th>
            </tr>
          </thead>
          <tbody>
            {funds.map((f) => (
              <tr key={f.symbol}>
                <td>
                  {SYMBOL_LABELS[f.symbol as Symbol_] ?? f.ticker ?? f.symbol}
                </td>
                <td className="num mono">{formatToman(f.price, unit, false)}</td>
                <td className={`num mono ${f.change_24h_pct !== null ? pctClass(f.change_24h_pct) : ''}`}>
                  {f.change_24h_pct !== null ? formatPct(f.change_24h_pct) : '—'}
                </td>
                <td className="num mono">{formatCompact(f.volume)}</td>
                <td className="num mono pos">
                  {f.retail_buy_pct !== null ? `${f.retail_buy_pct.toFixed(1)}%` : '—'}
                </td>
                <td className="num mono neg">
                  {f.retail_sell_pct !== null ? `${f.retail_sell_pct.toFixed(1)}%` : '—'}
                </td>
                <td className="num mono muted">
                  {f.today_avg_retail_buy_pct !== null && f.today_avg_retail_sell_pct !== null
                    ? `${f.today_avg_retail_buy_pct.toFixed(0)}% / ${f.today_avg_retail_sell_pct.toFixed(0)}%`
                    : '—'}
                </td>
                <td
                  className={`num mono ${
                    f.buyer_power !== null ? (f.buyer_power > 1 ? 'pos' : 'neg') : ''
                  }`}
                >
                  {f.buyer_power !== null ? f.buyer_power.toFixed(2) : '—'}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="muted small">
        Retail buys/sells = share of today&rsquo;s volume traded by individuals (حقیقی). Positive
        net flow means individuals are accumulating from institutions.
      </p>

      {res.data?.flow_pct != null && (
        <div className="kv">
          <span className="muted">Retail net flow now</span>
          <span className={`mono ${res.data.flow_pct > 0 ? 'pos' : 'neg'}`}>
            {formatPct(res.data.flow_pct)} of volume
          </span>
        </div>
      )}
      {flowChart.length >= 2 && (
        <div className="chart-box" style={{ height: 160 }}>
          <ResponsiveContainer width="100%" height="100%">
            <ComposedChart data={flowChart} margin={{ top: 8, right: 16, bottom: 4, left: 8 }}>
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
                tickFormatter={(v: number) => `${v.toFixed(0)}%`}
              />
              <Tooltip content={<ChartTip format={(v) => `${v.toFixed(1)}%`} />} />
              <ReferenceLine y={0} stroke="var(--border)" />
              <Line
                type="monotone"
                dataKey="flow"
                name="retail net flow"
                stroke="var(--accent)"
                strokeWidth={1.5}
                dot={false}
                isAnimationActive={false}
                connectNulls
              />
            </ComposedChart>
          </ResponsiveContainer>
        </div>
      )}
    </>
  )
}
