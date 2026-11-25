// A count of in-flight API data requests, so background work (the art
// loader's set-aside downloads) can hold off while the list data for the
// view the user just opened is still arriving.

let inFlight = 0
const idleListeners = new Set<() => void>()

// Requests that carry a user-facing label ("albums", "playlists"), shown by
// the slow-loading indicator once they have been pending for a moment.
const labelled = new Map<symbol, string>()
const labelListeners = new Set<() => void>()

function notifyLabels() {
  for (const listener of [...labelListeners]) listener()
}

/** The distinct labels of requests currently in flight. */
export function pendingLabels(): string[] {
  return [...new Set(labelled.values())]
}

/** Calls `listener` whenever the set of labelled requests changes. */
export function onLabelsChange(listener: () => void): () => void {
  labelListeners.add(listener)
  return () => {
    labelListeners.delete(listener)
  }
}

/** Marks one request as started. Call the returned function when it ends;
 * calling it more than once is harmless. */
export function beginRequest(label?: string): () => void {
  inFlight++
  let ended = false
  const key = Symbol()
  if (label) {
    labelled.set(key, label)
    notifyLabels()
  }
  return () => {
    if (ended) return
    ended = true
    inFlight--
    if (labelled.delete(key)) notifyLabels()
    if (inFlight === 0) for (const listener of [...idleListeners]) listener()
  }
}

export function isNetworkBusy(): boolean {
  return inFlight > 0
}

/** Calls `listener` every time the in-flight count drops back to zero.
 * Returns a function that unsubscribes it. */
export function onNetworkIdle(listener: () => void): () => void {
  idleListeners.add(listener)
  return () => {
    idleListeners.delete(listener)
  }
}
