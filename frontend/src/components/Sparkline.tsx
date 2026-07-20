export default function Sparkline({
  values,
  width = 160,
  height = 40,
  stroke
}: {
  values: number[]
  width?: number
  height?: number
  /** Optional fixed stroke; defaults to green/red by overall direction. */
  stroke?: string
}) {
  if (values.length < 2) {
    return <span className="muted small">Not enough data</span>
  }
  const min = Math.min(...values)
  const max = Math.max(...values)
  const span = max - min || 1
  const points = values
    .map((v, i) => {
      const x = (i / (values.length - 1)) * (width - 4) + 2
      const y = height - 3 - ((v - min) / span) * (height - 6)
      return `${x.toFixed(1)},${y.toFixed(1)}`
    })
    .join(' ')
  const rising = values[values.length - 1] >= values[0]
  const color = stroke ?? (rising ? 'var(--pos)' : 'var(--neg)')
  return (
    <svg
      width={width}
      height={height}
      viewBox={`0 0 ${width} ${height}`}
      className="sparkline"
      role="img"
      aria-label="trend sparkline"
      preserveAspectRatio="none"
    >
      <polyline points={points} fill="none" stroke={color} strokeWidth={1.5} strokeLinejoin="round" />
    </svg>
  )
}
