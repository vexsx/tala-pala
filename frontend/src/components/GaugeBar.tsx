export default function GaugeBar({
  value,
  max = 100,
  label
}: {
  /** Score in [0, max]; null/undefined renders an empty gauge. */
  value: number | null | undefined
  max?: number
  label?: string
}) {
  const clamped =
    value === null || value === undefined || Number.isNaN(value)
      ? null
      : Math.max(0, Math.min(max, value))
  const pct = clamped === null ? 0 : (clamped / max) * 100
  const tone = clamped === null ? '' : pct >= 66 ? 'gauge-high' : pct >= 33 ? 'gauge-mid' : 'gauge-low'
  return (
    <div className="gauge-wrap">
      {label && (
        <div className="gauge-label">
          <span>{label}</span>
          <span className="mono">{clamped === null ? '—' : `${Math.round(pct)}%`}</span>
        </div>
      )}
      <div
        className="gauge"
        role="meter"
        aria-valuemin={0}
        aria-valuemax={max}
        aria-valuenow={clamped ?? undefined}
        aria-label={label ?? 'score'}
      >
        <div className={`gauge-fill ${tone}`} style={{ width: `${pct}%` }} />
      </div>
    </div>
  )
}
