import { CloseIcon } from './Icons'

interface Props {
  /** The search text applied to the list; nothing is shown when empty. */
  query: string
  onClear: () => void
}

/** "Search: love ✕" in a browse tab's toolbar: the query Home's "View full
 * search" opened the tab with. The tabs have no search box of their own
 * (Home searches the whole library), so this is how the applied search
 * is shown, and removing it clears it. */
export function SearchChip({ query, onClear }: Props) {
  if (!query) return null
  return (
    <button
      type="button"
      className="chip chip-token search-chip"
      onClick={onClear}
      aria-label={`Clear search ${query}`}
      title="Clear search"
    >
      <span className="search-chip-text">Search: {query}</span>
      <CloseIcon aria-hidden="true" />
    </button>
  )
}
