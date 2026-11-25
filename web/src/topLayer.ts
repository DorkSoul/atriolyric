import { useSyncExternalStore } from 'react'

// The app's open modal <dialog> elements, innermost last. A modal dialog sits
// in the browser's top layer and makes everything outside it inert, so what
// has to stay visible and usable above it - the toasts, a help bubble - is put
// inside the innermost one instead of at the end of <body>.
const dialogs: HTMLDialogElement[] = []
const listeners = new Set<() => void>()

function changed() {
  for (const listener of listeners) listener()
}

export function pushTopLayerDialog(dialog: HTMLDialogElement): () => void {
  dialogs.push(dialog)
  changed()
  return () => {
    const index = dialogs.lastIndexOf(dialog)
    if (index >= 0) dialogs.splice(index, 1)
    changed()
  }
}

function subscribe(listener: () => void) {
  listeners.add(listener)
  return () => {
    listeners.delete(listener)
  }
}

function innermost(): HTMLElement | null {
  return dialogs[dialogs.length - 1] ?? null
}

/** The innermost open modal dialog, or null when none is open. */
export function useTopLayerDialog(): HTMLElement | null {
  return useSyncExternalStore(subscribe, innermost, () => null)
}
