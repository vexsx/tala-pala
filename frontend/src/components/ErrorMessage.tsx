export default function ErrorMessage({
  message,
  onRetry
}: {
  message: string
  onRetry?: () => void
}) {
  return (
    <div className="error-box" role="alert">
      <span>{message}</span>
      {onRetry && (
        <button type="button" className="btn btn-ghost btn-sm" onClick={onRetry}>
          Retry
        </button>
      )}
    </div>
  )
}
