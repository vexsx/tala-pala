export default function Loading({ label }: { label?: string }) {
  return (
    <div className="loading" role="status" aria-live="polite">
      <span className="spinner" aria-hidden="true" />
      <span>{label ?? 'Loading…'}</span>
    </div>
  )
}
