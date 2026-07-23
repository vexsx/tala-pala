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
import type {
  MarketSummary,
  PremiumPoint,
  PriceHistoryItem,
  PriceHistoryResponse
} from '../api/types'
import { unwrapList } from '../lib/unwrap'
import { useSettings } from '../lib/settings'
import {
  formatCompact,
  formatPct,
  formatToman,
  formatUsd,
  pctClass,
  shortDate
} from '../lib/format'
import { ChartTip } from '../components/PriceChart'
import Sparkline from '../components/Sparkline'
import StatCard from '../components/StatCard'
import Loading from '../components/Loading'
import ErrorMessage from '../components/ErrorMessage'
import EmptyState from '../components/EmptyState'

const DAY_MS = 24 * 60 * 60 * 1000

function sorted(items: PriceHistoryItem[]): PriceHistoryItem[] {
  return items.slice().sort((a, b) => a.observed_at.localeCompare(b.observed_at))
}

function changePct(values: number[], back: number): number | null {
  if (values.length < back + 1) return null
  const prev = values[values.length - 1 - back]
  if (prev === 0) return null
  return ((values[values.length - 1] - prev) / prev) * 100
}

export default function Drivers() {
  const { unit, calendar } = useSettings()

  const summary = useApi<MarketSummary>('/market/summary')
  const premium = useApi<unknown>('/market/premium?days=90')

  const from30 = useMemo(() => new Date(Date.now() - 30 * DAY_MS).toISOString(), [])
  const xauHistory = useApi<PriceHistoryResponse>(
    `/prices/history?symbol=XAUUSD&interval=daily&page_size=500&from=${encodeURIComponent(from30)}`
  )
  const usdHistory = useApi<PriceHistoryResponse>(
    `/prices/history?symbol=USD_IRT&interval=daily&page_size=500&from=${encodeURIComponent(from30)}`
  )

  const premiumItems = useMemo(
    () => unwrapList<PremiumPoint>(premium.data, 'items', 'history', 'series'),
    [premium.data]
  )

  const xauValues = useMemo(
    () => sorted(xauHistory.data?.items ?? []).map((i) => i.value),
    [xauHistory.data]
  )
  const usdValues = useMemo(
    () => sorted(usdHistory.data?.items ?? []).map((i) => i.value),
    [usdHistory.data]
  )

  const s = summary.data
  const premiumChart = premiumItems.map((p) => ({
    label: shortDate(p.date, calendar),
    observed: p.observed_18k,
    theoretical: p.theoretical_18k,
    premium: p.premium_pct
  }))
  const premiumAvg =
    s?.premium_avg_30d ??
    (premiumItems.length > 0
      ? premiumItems.slice(-30).reduce((acc, p) => acc + p.premium_pct, 0) /
        Math.min(30, premiumItems.length)
      : null)

  // Unusual movement callouts
  const callouts: string[] = []
  const xauDay = changePct(xauValues, 1)
  const usdDay = changePct(usdValues, 1)
  if (xauDay !== null && Math.abs(xauDay) >= 2) {
    callouts.push(
      `Global gold moved ${formatPct(xauDay)} in a day — an unusually large move that usually feeds into local prices.`
    )
  }
  if (usdDay !== null && Math.abs(usdDay) >= 2) {
    callouts.push(
      `The free-market dollar moved ${formatPct(usdDay)} in a day — expect knock-on volatility in gold.`
    )
  }
  const premiumNow = s?.premium_pct ?? null
  if (premiumNow !== null && premiumAvg !== null && Math.abs(premiumNow - premiumAvg) >= 2) {
    callouts.push(
      `The local premium (${formatPct(premiumNow)}) is far from its 30-day average (${formatPct(premiumAvg)}) — the market is pricing local risk ${
        premiumNow > premiumAvg ? 'up' : 'down'
      }.`
    )
  }

  if (summary.loading && premium.loading) return <Loading label="Loading drivers…" />

  return (
    <div className="page-body">
      <h2 className="page-title">Price drivers</h2>

      {summary.error && <ErrorMessage message={summary.error} onRetry={summary.reload} />}

      {callouts.map((c, i) => (
        <div key={i} className="callout callout-warn">
          {c}
        </div>
      ))}

      <div className="grid">
        <StatCard
          label="Global gold (XAU/USD)"
          value={
            xauValues.length > 0
              ? formatUsd(xauValues[xauValues.length - 1])
              : s?.xau_usd
                ? formatUsd(s.xau_usd.value)
                : '—'
          }
          delta={changePct(xauValues, 1)}
          sub={
            xauHistory.loading ? (
              <Loading label="Loading…" />
            ) : xauHistory.error ? (
              <span className="neg small">{xauHistory.error}</span>
            ) : (
              <>
                <Sparkline values={xauValues} />
                <div className="muted small">
                  30d: <span className={pctClass(changePct(xauValues, xauValues.length - 1))}>
                    {formatPct(changePct(xauValues, xauValues.length - 1))}
                  </span>
                </div>
              </>
            )
          }
        />
        <StatCard
          label="USD / IRT (free market)"
          value={
            usdValues.length > 0
              ? formatToman(usdValues[usdValues.length - 1], unit)
              : s?.usd_irt
                ? formatToman(s.usd_irt.value, unit)
                : '—'
          }
          delta={changePct(usdValues, 1)}
          sub={
            usdHistory.loading ? (
              <Loading label="Loading…" />
            ) : usdHistory.error ? (
              <span className="neg small">{usdHistory.error}</span>
            ) : (
              <>
                <Sparkline values={usdValues} />
                <div className="muted small">
                  30d: <span className={pctClass(changePct(usdValues, usdValues.length - 1))}>
                    {formatPct(changePct(usdValues, usdValues.length - 1))}
                  </span>
                </div>
              </>
            )
          }
        />
        <StatCard
          label="Local premium"
          value={s ? formatPct(s.premium_pct) : '—'}
          sub={
            <span className="muted small">
              30d average: {premiumAvg !== null ? formatPct(premiumAvg) : '—'} · Premium is the gap
              between the observed 18k price and global parity (XAU × USD/IRT × 0.75 / 31.1035).
            </span>
          }
        />
      </div>

      <div className="card">
        <div className="card-title">Theoretical vs observed 18k price (90d)</div>
        {premium.loading ? (
          <Loading label="Loading premium history…" />
        ) : premium.error ? (
          <ErrorMessage message={premium.error} onRetry={premium.reload} />
        ) : premiumChart.length === 0 ? (
          <EmptyState title="No premium history yet" />
        ) : (
          <div className="chart-box" style={{ height: 300 }}>
            <ResponsiveContainer width="100%" height="100%">
              <ComposedChart data={premiumChart} margin={{ top: 8, right: 16, bottom: 4, left: 8 }}>
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
                  tickFormatter={(v: number) => formatCompact(v)}
                  width={64}
                  domain={['auto', 'auto']}
                  tickLine={false}
                  axisLine={{ stroke: 'var(--border)' }}
                />
                <Tooltip content={<ChartTip format={(v) => formatToman(v, unit)} />} />
                <Line
                  type="monotone"
                  dataKey="observed"
                  name="Observed"
                  stroke="var(--accent)"
                  strokeWidth={2}
                  dot={false}
                  isAnimationActive={false}
                />
                <Line
                  type="monotone"
                  dataKey="theoretical"
                  name="Theoretical"
                  stroke="var(--info)"
                  strokeWidth={1.5}
                  strokeDasharray="5 4"
                  dot={false}
                  isAnimationActive={false}
                />
              </ComposedChart>
            </ResponsiveContainer>
          </div>
        )}
      </div>

      <div className="card">
        <div className="card-title">Premium % history (90d)</div>
        {premium.loading ? (
          <Loading label="Loading…" />
        ) : premiumChart.length === 0 ? (
          <EmptyState title="No premium history yet" />
        ) : (
          <div className="chart-box" style={{ height: 240 }}>
            <ResponsiveContainer width="100%" height="100%">
              <ComposedChart data={premiumChart} margin={{ top: 8, right: 16, bottom: 4, left: 8 }}>
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
                  tickFormatter={(v: number) => `${v.toFixed(0)}%`}
                  width={44}
                  domain={['auto', 'auto']}
                  tickLine={false}
                  axisLine={{ stroke: 'var(--border)' }}
                />
                <Tooltip content={<ChartTip format={(v) => `${v.toFixed(2)}%`} />} />
                <ReferenceLine y={0} stroke="var(--border)" />
                {premiumAvg !== null && (
                  <ReferenceLine
                    y={premiumAvg}
                    stroke="var(--warn)"
                    strokeDasharray="4 4"
                    label={{
                      value: `30d avg ${formatPct(premiumAvg)}`,
                      fill: 'var(--warn)',
                      fontSize: 11,
                      position: 'insideTopRight'
                    }}
                  />
                )}
                <Line
                  type="monotone"
                  dataKey="premium"
                  name="Premium %"
                  stroke="var(--warn)"
                  strokeWidth={2}
                  dot={false}
                  isAnimationActive={false}
                />
              </ComposedChart>
            </ResponsiveContainer>
          </div>
        )}
      </div>
    </div>
  )
}
