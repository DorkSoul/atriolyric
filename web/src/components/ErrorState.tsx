interface ErrorStateProps {
  /** What went wrong, in words for the user. */
  message: string
  /** Shown as a Retry button when given. */
  onRetry?: () => void
}

/** The failed counterpart of "Loading…" and "Nothing here yet.": a view that
 * could not load says so (and offers Retry) instead of looking empty. */
export function ErrorState({ message, onRetry }: ErrorStateProps) {
  return (
    <div className="empty error-state" role="alert">
      <p className="error-state-message">{message}</p>
      {onRetry && (
        <button type="button" onClick={onRetry}>
          Retry
        </button>
      )}
    </div>
  )
}

/** The inline loading line every view shares. (The pill that names slow
 * requests is LoadingStatus; it never replaces this.) */
export function LoadingState({ label = 'Loading…' }: { label?: string }) {
  return (
    <p className="loading">{label}</p>
  )
}

/** The quiet note beside an answer the server is still completing (its
 * `pending` flag): what came back is already showing. */
export function FetchingMore({ label = 'Fetching more…' }: { label?: string }) {
  return (
    <p className="fetching-more" role="status">
      {label}
    </p>
  )
}
