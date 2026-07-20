import type { ReactNode } from 'react'
import { formatPct, pctClass } from '../lib/format'

export interface StatCardProps {
  label: string
  value: ReactNode
  sub?: ReactNode
  /** 24h (or contextual) percent change; renders a colored delta chip. */
  delta?: number | null
  tone?: 'default' | 'warn' | 'accent'
  children?: ReactNode
}

export default function StatCard({ label, value, sub, delta, tone = 'default', children }: StatCardProps) {
  const toneClass = tone === 'warn' ? 'card-warn' : tone === 'accent' ? 'card-accent' : ''
  return (
    <div className={`card stat-card ${toneClass}`}>
      <div className="card-title">{label}</div>
      <div className="stat-value">{value}</div>
      {delta !== undefined && (
        <div className={`delta ${pctClass(delta)}`}>{formatPct(delta)}</div>
      )}
      {sub !== undefined && <div className="stat-sub">{sub}</div>}
      {children}
    </div>
  )
}
