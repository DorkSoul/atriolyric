import { useCallback, useEffect, useRef, useState } from 'react'

export const PAGE_SIZE = 100

/** What failed when `error` is set: the first page (nothing to show), a
 * later page (the rows read so far stay), or an in-place refresh. */
export type PagedListFailure = 'first' | 'more' | 'refresh'

function sameDeps(a: readonly unknown[], b: readonly unknown[]): boolean {
  return a.length === b.length && a.every((value, index) => Object.is(value, b[index]))
}

/** Fetches a list one page at a time instead of all at once, so a huge
 * library shows its first page almost immediately instead of blocking the
 * tab on one multi-thousand-row request. `deps` changing (search, filters)
 * resets to page one. `reload()` is the other kind of refresh, for after an
 * edit: it refetches exactly the range already loaded and swaps it in, so the
 * rows stay on screen, the scroll position holds, and the list does not fall
 * back to page one. */
export function usePagedList<T>(fetchPage: (offset: number, limit: number) => Promise<T[]>, deps: unknown[]) {
  const [items, setItems] = useState<T[]>([])
  const [loading, setLoading] = useState(true)
  const [loadingMore, setLoadingMore] = useState(false)
  const [hasMore, setHasMore] = useState(false)
  const [error, setError] = useState<unknown>(null)
  const [failure, setFailure] = useState<PagedListFailure | null>(null)
  const offsetRef = useRef(0)
  const fetchRef = useRef(fetchPage)
  useEffect(() => {
    fetchRef.current = fetchPage
  })
  const loadingMoreRef = useRef(false)
  const generationRef = useRef(0)
  // The inputs the current rows were fetched for. A hidden `<Activity>`
  // disconnects this effect and runs it again on reveal: with the same inputs
  // the list is already right, so it is left alone (scroll and rows intact).
  const fetchedForRef = useRef<readonly unknown[] | null>(null)

  useEffect(() => {
    if (fetchedForRef.current && sameDeps(fetchedForRef.current, deps)) return
    let cancelled = false
    const generation = ++generationRef.current
    fetchedForRef.current = null
    offsetRef.current = 0
    loadingMoreRef.current = false
    // The state too, not just the ref: a "load more" still in flight for the
    // old filter is ignored when it lands (generation check), so nothing else
    // would ever clear "Loading more songs…".
    setLoadingMore(false)
    setLoading(true)
    setItems([])
    setHasMore(false)
    setError(null)
    setFailure(null)
    fetchRef
      .current(0, PAGE_SIZE)
      .then((page) => {
        if (cancelled || generation !== generationRef.current) return
        setItems(page)
        setHasMore(page.length === PAGE_SIZE)
        offsetRef.current = page.length
        fetchedForRef.current = deps
      })
      .catch((reason) => {
        if (cancelled || generation !== generationRef.current) return
        setError(reason)
        setFailure('first')
      })
      .finally(() => {
        if (!cancelled && generation === generationRef.current) setLoading(false)
      })
    return () => {
      cancelled = true
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps])

  const reload = useCallback(() => {
    const generation = ++generationRef.current
    // A "load more" in flight belongs to the list being replaced.
    loadingMoreRef.current = false
    setLoadingMore(false)
    setError(null)
    setFailure(null)
    const wanted = Math.max(offsetRef.current, PAGE_SIZE)
    if (offsetRef.current === 0) setLoading(true)
    void (async () => {
      const rows: T[] = []
      let lastPageFull = true
      try {
        // The loaded range, a page at a time: the server caps one request.
        while (rows.length < wanted && lastPageFull) {
          const size = Math.min(PAGE_SIZE, wanted - rows.length)
          const page = await fetchRef.current(rows.length, size)
          if (generation !== generationRef.current) return
          rows.push(...page)
          lastPageFull = page.length === size
        }
        setItems(rows)
        // A range that came back full may have more behind it; a short one
        // (rows were removed) has reached the end.
        setHasMore(lastPageFull)
        offsetRef.current = rows.length
      } catch (reason) {
        if (generation !== generationRef.current) return
        setError(reason)
        setFailure(offsetRef.current === 0 ? 'first' : 'refresh')
      } finally {
        if (generation === generationRef.current) setLoading(false)
      }
    })()
  }, [])

  const loadMore = useCallback(() => {
    if (loadingMoreRef.current || !hasMore) return
    loadingMoreRef.current = true
    const generation = generationRef.current
    setLoadingMore(true)
    setError(null)
    setFailure(null)
    fetchRef
      .current(offsetRef.current, PAGE_SIZE)
      .then((page) => {
        if (generation !== generationRef.current) return
        setItems((prev) => [...prev, ...page])
        setHasMore(page.length === PAGE_SIZE)
        offsetRef.current += page.length
      })
      .catch((reason) => {
        if (generation !== generationRef.current) return
        setError(reason)
        setFailure('more')
      })
      .finally(() => {
        if (generation !== generationRef.current) return
        loadingMoreRef.current = false
        setLoadingMore(false)
      })
  }, [hasMore])

  return { items, loading, loadingMore, hasMore, loadMore, reload, error, failure }
}
