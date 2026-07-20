import {
  Area,
  CartesianGrid,
  ComposedChart,
  Line,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis
} from 'recharts'
import { formatCompact } from '../lib/format'

export interface ChartPoint {
  label: string
  actual?: number | null
  forecast?: number | null
  /** [lower, upper] prediction interval, rendered as a shaded band. */
  band?: [number, number] | null
}

// ---------- Shared custom tooltip (also used by Technical/Drivers charts) ----------

export interface ChartTipItem {
  name?: string | number
  value?: number | string | Array<number | string> | null
  color?: string
}

export function ChartTip({
  active,
  payload,
  label,
  format
}: {
  active?: boolean
  payload?: ChartTipItem[]
  label?: string | number
  format?: (v: number) => string
}) {
  if (!active || !payload || payload.length === 0) return null
  const fmt = format ?? ((v: number) => formatCompact(v))
  const fmtVal = (x: number | string | null | undefined): string =>
    typeof x === 'number' ? fmt(x) : x === null || x === undefined ? '—' : String(x)
  return (
    <div className="chart-tip">
      <div className="chart-tip-label">{label}</div>
      {payload.map((item, i) => {
        const v = item.value
        const text = Array.isArray(v) ? `${fmtVal(v[0])} – ${fmtVal(v[1])}` : fmtVal(v)
        return (
          <div key={i} className="chart-tip-row">
            <span className="chart-tip-dot" style={{ background: item.color }} aria-hidden="true" />
            <span>{item.name}</span>
            <span className="mono chart-tip-value">{text}</span>
          </div>
        )
      })}
    </div>
  )
}

// ---------- Price line + interval band ----------

export default function PriceChart({
  data,
  height = 300,
  format
}: {
  data: ChartPoint[]
  height?: number
  format: (v: number) => string
}) {
  return (
    <div className="chart-box" style={{ height }}>
      <ResponsiveContainer width="100%" height="100%">
        <ComposedChart data={data} margin={{ top: 8, right: 16, bottom: 4, left: 8 }}>
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
          <Tooltip content={<ChartTip format={format} />} />
          <Area
            type="monotone"
            dataKey="band"
            name="Interval"
            fill="var(--band-fill)"
            stroke="none"
            isAnimationActive={false}
            connectNulls
            legendType="none"
          />
          <Line
            type="monotone"
            dataKey="actual"
            name="Actual"
            stroke="var(--accent)"
            strokeWidth={2}
            dot={false}
            isAnimationActive={false}
            connectNulls
          />
          <Line
            type="monotone"
            dataKey="forecast"
            name="Forecast"
            stroke="var(--info)"
            strokeWidth={2}
            strokeDasharray="6 4"
            dot={{ r: 3 }}
            isAnimationActive={false}
            connectNulls
          />
        </ComposedChart>
      </ResponsiveContainer>
    </div>
  )
}
