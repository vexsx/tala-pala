import type { SignalLevel } from '../api/types'

const LABELS: Record<SignalLevel, string> = {
  strong_buy: 'Strong Buy',
  buy: 'Buy',
  hold: 'Hold',
  sell: 'Sell',
  strong_sell: 'Strong Sell'
}

export default function SignalBadge({
  signal,
  size = 'md'
}: {
  signal: SignalLevel | string | null | undefined
  size?: 'md' | 'lg'
}) {
  if (!signal) {
    return <span className="signal-badge sig-none" data-testid="signal-badge">No signal</span>
  }
  const key = signal as SignalLevel
  const label = LABELS[key] ?? String(signal)
  const known = key in LABELS
  return (
    <span
      className={`signal-badge sig-${known ? key : 'none'}${size === 'lg' ? ' signal-lg' : ''}`}
      data-testid="signal-badge"
    >
      {label}
    </span>
  )
}
