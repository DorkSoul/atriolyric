// The bookkeeping behind Modal's `history` prop (a dialog that owns a history
// entry, so the phone's Back button closes it), and the one way to leave such
// a dialog for another page. Its own module: Modal.tsx exports only the
// component.

/** Modals currently open, innermost last: only the top one reacts to Escape
 * and keeps focus, so a picker opened from an info modal closes on its own. */
export const openStack: symbol[] = []

/** Modals that own a history entry (the `history` prop), by their stack token.
 * `pop` is what the Back button does to that modal. */
export interface HistoryEntry {
  id: number
  popped: boolean
  /** Closes the modal; false when closing is blocked and the entry was put back. */
  pop: () => boolean
  /** Set by closeModalThen: run once Back has taken the entry and the modal
   * has been told to close. */
  afterPop?: () => void
}
export const historyEntries = new Map<symbol, HistoryEntry>()
let nextHistoryId = 0
export function newHistoryId(): number {
  return nextHistoryId++
}
/** `history.back()` calls a modal made itself to drop its own entry. Their
 * popstate events are swallowed below, so neither the app's navigation nor
 * Now Playing's own history handling mistakes them for the user pressing Back. */
let pendingSelfPops = 0
let popstateGuardInstalled = false

/** Drops the current entry (a closed modal's own) without anyone taking the
 * resulting popstate for a Back press. */
export function selfPop() {
  pendingSelfPops += 1
  window.history.back()
}

function installPopstateGuard() {
  if (popstateGuardInstalled) return
  popstateGuardInstalled = true
  // Installed when this module loads, so it is the first popstate listener on
  // window (listeners on the target run in registration order, whatever the
  // capture flag): ahead of App.tsx and PlayerContext's, a modal that is on
  // top owns the Back press.
  window.addEventListener(
    'popstate',
    (event) => {
      if (pendingSelfPops > 0) {
        pendingSelfPops -= 1
        event.stopImmediatePropagation()
        return
      }
      const entry = topHistoryEntry()
      if (!entry) return
      event.stopImmediatePropagation()
      const after = entry.afterPop
      entry.afterPop = undefined
      if (entry.pop()) after?.()
    },
    true,
  )
}

if (typeof window !== 'undefined') installPopstateGuard()

/** The history entry of the modal on top. One whose entry Back already took
 * is skipped: it has been told to close but is still mounted until React
 * commits that, and the next Back belongs to the modal under it. */
function topHistoryEntry(): HistoryEntry | undefined {
  for (let index = openStack.length - 1; index >= 0; index -= 1) {
    const entry = historyEntries.get(openStack[index])
    if (!entry?.popped) return entry
  }
  return undefined
}

/** For a control inside a modal that owns a history entry (`history`) and
 * leads to another page: closes the modal, then runs `go`.
 *
 * Calling `onClose(); navigate()` in one tick pushes the new page on top of
 * the modal's entry, which is then left behind - Back returns to the old
 * page and the next Back does nothing visible. Here the modal's entry is
 * taken off first (the same way the Back button closes it, so `close` runs
 * through the modal's own `onClose`), and `go` runs once that has landed: the
 * new page follows the old one directly. `close` is only called here when
 * there is no entry to take (it was never pushed, or is already gone). */
export function closeModalThen(close: () => void, go: () => void) {
  const entry = topHistoryEntry()
  if (entry && window.history.state?.modalEntry === entry.id) {
    entry.afterPop = go
    window.history.back()
    return
  }
  close()
  go()
}
