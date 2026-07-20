import { useMemo, useState } from 'react'
import {
  Area,
  Bar,
  CartesianGrid,
  ComposedChart,
  Legend,
  Line,
  ReferenceArea,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis
} from 'recharts'
import { useApi } from '../hooks/useApi'
import type { IndicatorPoint, IndicatorsResponse } from '../api/types'
import { useSettings } from '../lib/settings'
import { formatCompact, formatGrouped, formatToman, shortDate } from '../lib/format'
import { ChartTip } from '../components/PriceChart'
import Loading from '../components/Loading'
import ErrorMessage from '../components/ErrorMessage'
import EmptyState from '../components/EmptyState'

interface Row {
  label: string
  close: number
  sma20: number | null
  sma50: number | null
  bb: [number, number] | null
  bbMid: number | null
  rsi: number | null
  macdLine: number | null
  macdSignal: number | null
  macdHist: number | null
}

function lastDefined<T>(items: IndicatorPoint[], pick: (p: IndicatorPoint) => T | null | undefined): T | null {
  for (let i = items.length - 1; i >= 0; i--) {
    const v = pick(items[i])
    if (v !== null && v !== undefined) return v
  }
  return null
}

export default function Technical() {
  const { unit, calendar } = useSettings()
  const res = useApi<IndicatorsResponse>('/market/indicators?days=90')
  const [showBollinger, setShowBollinger] = useState(true)

  const items = useMemo(() => res.data?.items ?? [], [res.data])

  const rows: Row[] = useMemo(
    () =>
      items.map((p) => ({
        label: shortDate(p.date, calendar),
        close: p.close,
        sma20: p.sma_20,
        sma50: p.sma_50,
        bb: p.bollinger ? [p.bollinger.lower, p.bollinger.upper] : null,
        bbMid: p.bollinger?.mid ?? null,
        rsi: p.rsi_14,
        macdLine: p.macd?.line ?? null,
        macdSignal: p.macd?.signal ?? null,
        macdHist: p.macd?.hist ?? null
      })),
    [items, calendar]
  )

  if (res.loading) return <Loading label="Computing indicators…" />
  if (res.error) return <ErrorMessage message={res.error} onRetry={res.reload} />
  if (items.length === 0) {
    return <EmptyState title="No indicator data" hint="Indicators need daily price history to compute." />
  }

  const last = items[items.length - 1]
  const support = res.data?.support ?? null
  const resistance = res.data?.resistance ?? null
  const fmt = (v: number) => formatToman(v, unit)

  const close = last.close
  const rsi = lastDefined(items, (p) => p.rsi_14)
  const macd = lastDefined(items, (p) => p.macd)
  const boll = lastDefined(items, (p) => p.bollinger)
  const sma20 = lastDefined(items, (p) => p.sma_20)
  const sma50 = lastDefined(items, (p) => p.sma_50)
  const ema12 = lastDefined(items, (p) => p.ema_12)
  const ema26 = lastDefined(items, (p) => p.ema_26)
  const atr = lastDefined(items, (p) => p.atr_14)
  const momentum = lastDefined(items, (p) => p.momentum_10)
  const roc = lastDefined(items, (p) => p.roc_10)
  const vol = lastDefined(items, (p) => p.volatility_20)

  const indicatorRows: Array<{ name: string; value: string; meaning: string }> = [
    {
      name: 'Close',
      value: formatToman(close, unit),
      meaning: 'Latest daily 18k gold price per gram.'
    },
    {
      name: 'SMA 20 / SMA 50',
      value: `${sma20 !== null ? formatToman(sma20, unit, false) : '—'} / ${sma50 !== null ? formatToman(sma50, unit, false) : '—'}`,
      meaning:
        sma20 !== null && sma50 !== null
          ? sma20 > sma50
            ? 'Short-term average above long-term — trend tilts upward.'
            : 'Short-term average below long-term — trend tilts downward.'
          : 'Needs more history to read the trend.'
    },
    {
      name: 'EMA 12 / EMA 26',
      value: `${ema12 !== null ? formatToman(ema12, unit, false) : '—'} / ${ema26 !== null ? formatToman(ema26, unit, false) : '—'}`,
      meaning: 'Exponential averages that react faster to recent moves than SMAs.'
    },
    {
      name: 'RSI 14',
      value: rsi !== null ? rsi.toFixed(1) : '—',
      meaning:
        rsi === null
          ? 'Not enough data.'
          : rsi >= 70
            ? 'Overbought — the recent rally may be stretched.'
            : rsi <= 30
              ? 'Oversold — the recent drop may be stretched.'
              : 'Neutral momentum, no extreme reading.'
    },
    {
      name: 'MACD (line / signal / hist)',
      value: macd
        ? `${formatCompact(macd.line)} / ${formatCompact(macd.signal)} / ${formatCompact(macd.hist)}`
        : '—',
      meaning: macd
        ? macd.hist > 0
          ? 'MACD above its signal line — bullish momentum.'
          : 'MACD below its signal line — bearish momentum.'
        : 'Not enough data.'
    },
    {
      name: 'Bollinger (upper / mid / lower)',
      value: boll
        ? `${formatCompact(boll.upper)} / ${formatCompact(boll.mid)} / ${formatCompact(boll.lower)}`
        : '—',
      meaning: boll
        ? close >= boll.upper
          ? 'Price at/above the upper band — stretched to the upside.'
          : close <= boll.lower
            ? 'Price at/below the lower band — stretched to the downside.'
            : 'Price inside the bands — normal volatility range.'
        : 'Not enough data.'
    },
    {
      name: 'ATR 14',
      value: atr !== null ? formatToman(atr, unit) : '—',
      meaning: 'Average daily trading range — higher means choppier prices.'
    },
    {
      name: 'Momentum 10',
      value: momentum !== null ? formatCompact(momentum) : '—',
      meaning:
        momentum === null
          ? 'Not enough data.'
          : momentum > 0
            ? 'Price is higher than 10 days ago.'
            : 'Price is lower than 10 days ago.'
    },
    {
      name: 'ROC 10',
      value: roc !== null ? `${roc.toFixed(2)}%` : '—',
      meaning: 'Percent change versus 10 days ago.'
    },
    {
      name: 'Volatility 20',
      value: vol !== null ? formatGrouped(vol, 4) : '—',
      meaning: '20-day standard deviation of returns — higher means riskier.'
    },
    {
      name: 'Support / Resistance',
      value: `${support !== null ? formatToman(support, unit, false) : '—'} / ${resistance !== null ? formatToman(resistance, unit, false) : '—'}`,
      meaning: 'Recent floor and ceiling levels where price has previously turned.'
    }
  ]

  return (
    <div className="page-body">
      <h2 className="page-title">Technical analysis</h2>

      <div className="card">
        <div className="row space-between">
          <div className="card-title">Price with moving averages (90d, daily)</div>
          <label className="check-label">
            <input
              type="checkbox"
              checked={showBollinger}
              onChange={(e) => setShowBollinger(e.target.checked)}
            />{' '}
            Bollinger bands
          </label>
        </div>
        <div className="chart-box" style={{ height: 340 }}>
          <ResponsiveContainer width="100%" height="100%">
            <ComposedChart data={rows} margin={{ top: 8, right: 16, bottom: 4, left: 8 }}>
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
              <Tooltip content={<ChartTip format={fmt} />} />
              <Legend wrapperStyle={{ fontSize: 12 }} />
              {showBollinger && (
                <Area
                  type="monotone"
                  dataKey="bb"
                  name="Bollinger"
                  fill="var(--band-fill)"
                  stroke="none"
                  isAnimationActive={false}
                  connectNulls
                  legendType="none"
                />
              )}
              <Line
                type="monotone"
                dataKey="close"
                name="Close"
                stroke="var(--accent)"
                strokeWidth={2}
                dot={false}
                isAnimationActive={false}
              />
              <Line
                type="monotone"
                dataKey="sma20"
                name="SMA 20"
                stroke="var(--info)"
                strokeWidth={1.5}
                dot={false}
                isAnimationActive={false}
                connectNulls
              />
              <Line
                type="monotone"
                dataKey="sma50"
                name="SMA 50"
                stroke="var(--purple)"
                strokeWidth={1.5}
                dot={false}
                isAnimationActive={false}
                connectNulls
              />
              {support !== null && (
                <ReferenceLine
                  y={support}
                  stroke="var(--pos)"
                  strokeDasharray="4 4"
                  label={{ value: 'Support', fill: 'var(--pos)', fontSize: 11, position: 'insideBottomLeft' }}
                />
              )}
              {resistance !== null && (
                <ReferenceLine
                  y={resistance}
                  stroke="var(--neg)"
                  strokeDasharray="4 4"
                  label={{ value: 'Resistance', fill: 'var(--neg)', fontSize: 11, position: 'insideTopLeft' }}
                />
              )}
            </ComposedChart>
          </ResponsiveContainer>
        </div>
      </div>

      <div className="grid grid-2">
        <div className="card">
          <div className="card-title">RSI 14</div>
          <div className="chart-box" style={{ height: 200 }}>
            <ResponsiveContainer width="100%" height="100%">
              <ComposedChart data={rows} margin={{ top: 8, right: 16, bottom: 4, left: 8 }}>
                <CartesianGrid stroke="var(--border)" strokeDasharray="3 3" vertical={false} />
                <XAxis
                  dataKey="label"
                  tick={{ fill: 'var(--muted)', fontSize: 11 }}
                  minTickGap={28}
                  tickLine={false}
                  axisLine={{ stroke: 'var(--border)' }}
                />
                <YAxis
                  domain={[0, 100]}
                  ticks={[0, 30, 50, 70, 100]}
                  tick={{ fill: 'var(--muted)', fontSize: 11 }}
                  width={36}
                  tickLine={false}
                  axisLine={{ stroke: 'var(--border)' }}
                />
                <Tooltip content={<ChartTip format={(v) => v.toFixed(1)} />} />
                <ReferenceArea y1={30} y2={70} fill="var(--band-fill)" strokeOpacity={0} />
                <ReferenceLine y={70} stroke="var(--neg)" strokeDasharray="4 4" />
                <ReferenceLine y={30} stroke="var(--pos)" strokeDasharray="4 4" />
                <Line
                  type="monotone"
                  dataKey="rsi"
                  name="RSI"
                  stroke="var(--info)"
                  strokeWidth={1.5}
                  dot={false}
                  isAnimationActive={false}
                  connectNulls
                />
              </ComposedChart>
            </ResponsiveContainer>
          </div>
        </div>

        <div className="card">
          <div className="card-title">MACD (12, 26, 9)</div>
          <div className="chart-box" style={{ height: 200 }}>
            <ResponsiveContainer width="100%" height="100%">
              <ComposedChart data={rows} margin={{ top: 8, right: 16, bottom: 4, left: 8 }}>
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
                  width={56}
                  tickLine={false}
                  axisLine={{ stroke: 'var(--border)' }}
                />
                <Tooltip content={<ChartTip format={(v) => formatCompact(v)} />} />
                <ReferenceLine y={0} stroke="var(--border)" />
                <Bar dataKey="macdHist" name="Histogram" fill="var(--muted)" isAnimationActive={false} />
                <Line
                  type="monotone"
                  dataKey="macdLine"
                  name="MACD"
                  stroke="var(--info)"
                  strokeWidth={1.5}
                  dot={false}
                  isAnimationActive={false}
                  connectNulls
                />
                <Line
                  type="monotone"
                  dataKey="macdSignal"
                  name="Signal"
                  stroke="var(--accent)"
                  strokeWidth={1.5}
                  dot={false}
                  isAnimationActive={false}
                  connectNulls
                />
              </ComposedChart>
            </ResponsiveContainer>
          </div>
        </div>
      </div>

      <div className="card">
        <div className="card-title">Current readings</div>
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th>Indicator</th>
                <th className="num">Value</th>
                <th>What it means</th>
              </tr>
            </thead>
            <tbody>
              {indicatorRows.map((r) => (
                <tr key={r.name}>
                  <td>{r.name}</td>
                  <td className="num mono">{r.value}</td>
                  <td className="muted">{r.meaning}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  )
}
