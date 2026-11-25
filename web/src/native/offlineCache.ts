// What the Android app keeps of the server's answers, so its pages still have
// something to show with no connection: the last answer to each read request
// (by address), and every song or video the app has been told about (by id),
// which is what the player needs to show a queue of downloads. Kept in
// IndexedDB on the device; emptied when the account changes.

import type { MediaItem } from '../types'
import { isNativeApp } from './bridge'

const DB_NAME = 'atriolyric-offline'
const ANSWERS = 'answers'
const ITEMS = 'items'

// Never kept: the server's own state (version, updates), admin pages, and
// the status of a video being prepared for download.
const NOT_KEPT = ['/api/version', '/api/admin', '/api/client', '/api/download/video', '/api/library/media?']

/** Answers kept at most. Every read request is kept (each search, each
 * page of a list), so without a limit the store would only grow. */
const MAX_ANSWERS = 5_000
/** A pass trims to this, so it does not run again after every new answer. */
const TRIM_TO = 4_500
/** New answers between two looks at the count. */
const TRIM_EVERY = 200

// Never trimmed: the app's copy of the library (offline:* and the lists
// offlineLibrary.ts answers from, which it refreshes daily anyway) and a
// downloaded song's lyrics, which are kept for reading with no connection.
const ALWAYS_KEPT = [/^offline:/, /^\/api\/library\/labels\/all$/, /^\/api\/library\/(albums|artists)\?.*full=1/, /\/lyrics$/]

let opening: Promise<IDBDatabase | null> | null = null
let keptSinceTrim = 0

function open(): Promise<IDBDatabase | null> {
  opening ??= new Promise((resolve) => {
    try {
      // Version 2 adds the index by age that trimming walks.
      const request = indexedDB.open(DB_NAME, 2)
      request.onupgradeneeded = (event) => {
        const db = request.result
        const answers = event.oldVersion < 1 ? db.createObjectStore(ANSWERS) : request.transaction!.objectStore(ANSWERS)
        if (event.oldVersion < 1) db.createObjectStore(ITEMS)
        if (!answers.indexNames.contains('at')) answers.createIndex('at', 'at')
      }
      request.onsuccess = () => {
        // A store already over the limit is trimmed as soon as it opens.
        trimAnswers(request.result)
        resolve(request.result)
      }
      request.onerror = () => resolve(null)
      request.onblocked = () => resolve(null)
    } catch {
      resolve(null)
    }
  })
  return opening
}

function done<T>(request: IDBRequest<T>): Promise<T | undefined> {
  return new Promise((resolve) => {
    request.onsuccess = () => resolve(request.result)
    request.onerror = () => resolve(undefined)
  })
}

/** Whether answers to `path` are kept for offline use. */
export function keepsAnswer(path: string, method: string | undefined): boolean {
  if (!isNativeApp()) return false
  if (method && method !== 'GET') return false
  return !NOT_KEPT.some((prefix) => path.startsWith(prefix))
}

export async function rememberAnswer(path: string, body: unknown): Promise<void> {
  const db = await open()
  if (!db) return
  try {
    db.transaction(ANSWERS, 'readwrite').objectStore(ANSWERS).put({ body, at: Date.now() }, path)
  } catch {
    // Full or closed: this answer is not kept.
  }
  keptSinceTrim += 1
  if (keptSinceTrim >= TRIM_EVERY) {
    keptSinceTrim = 0
    trimAnswers(db)
  }
}

/** Drops a kept answer (the app's own working copies, see offlineLibrary.ts). */
export async function forgetAnswer(path: string): Promise<void> {
  const db = await open()
  if (!db) return
  try {
    db.transaction(ANSWERS, 'readwrite').objectStore(ANSWERS).delete(path)
  } catch {
    // Gone already, or the store is closed.
  }
}

/** Once the store holds more than MAX_ANSWERS, drops the answers kept
 * longest ago (oldest first, by the `at` index) down to TRIM_TO. */
function trimAnswers(db: IDBDatabase): void {
  try {
    // All in callbacks of one transaction: an await between the count and
    // the cursor could let the transaction close.
    const store = db.transaction(ANSWERS, 'readwrite').objectStore(ANSWERS)
    if (!store.indexNames.contains('at')) return
    const counting = store.count()
    counting.onsuccess = () => {
      let excess = counting.result - TRIM_TO
      if (counting.result <= MAX_ANSWERS) return
      const walk = store.index('at').openCursor()
      walk.onsuccess = () => {
        const at = walk.result
        if (!at || excess <= 0) return
        if (!ALWAYS_KEPT.some((rule) => rule.test(String(at.primaryKey)))) {
          at.delete()
          excess -= 1
        }
        at.continue()
      }
    }
  } catch {
    // Not this time; the next pass tries again.
  }
}

/** The last answer kept for `path`, or undefined when there is none. */
export async function rememberedAnswer<T>(path: string): Promise<{ body: T } | undefined> {
  const db = await open()
  if (!db) return undefined
  try {
    return await done<{ body: T }>(db.transaction(ANSWERS).objectStore(ANSWERS).get(path))
  } catch {
    return undefined
  }
}

export async function rememberItems(items: MediaItem[]): Promise<void> {
  if (!isNativeApp() || items.length === 0) return
  const db = await open()
  if (!db) return
  try {
    const store = db.transaction(ITEMS, 'readwrite').objectStore(ITEMS)
    for (const item of items) store.put(item, item.id)
  } catch {
    // Not kept this time.
  }
}

/** The items of `ids` the app has been told about, in that order. */
export async function rememberedItems(ids: number[]): Promise<MediaItem[]> {
  const db = await open()
  if (!db) return []
  try {
    const store = db.transaction(ITEMS).objectStore(ITEMS)
    const found = await Promise.all(ids.map((id) => done<MediaItem>(store.get(id))))
    return found.filter((item): item is MediaItem => item != null)
  } catch {
    return []
  }
}

/** Another account signs in, or this one signs out: nothing of it stays. */
export async function forgetOfflineData(): Promise<void> {
  if (!isNativeApp()) return
  const db = await open()
  if (!db) return
  try {
    const transaction = db.transaction([ANSWERS, ITEMS], 'readwrite')
    transaction.objectStore(ANSWERS).clear()
    transaction.objectStore(ITEMS).clear()
  } catch {
    // Nothing to clear.
  }
}
