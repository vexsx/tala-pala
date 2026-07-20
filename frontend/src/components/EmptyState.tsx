import type { ReactNode } from 'react'

export default function EmptyState({
  title,
  hint,
  children
}: {
  title: string
  hint?: string
  children?: ReactNode
}) {
  return (
    <div className="empty">
      <div className="empty-title">{title}</div>
      {hint && <div className="muted small">{hint}</div>}
      {children}
    </div>
  )
}
