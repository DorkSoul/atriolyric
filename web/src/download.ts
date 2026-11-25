// Downloads of originals: a single song/video goes through a plain
// same-origin link; an album/artist/playlist is written file-by-file into a
// folder the user picked once (File System Access API, desktop Chromium), or
// falls back to the server-built ZIP everywhere else.

export type CollectionKind = 'album' | 'artist' | 'playlist'
type Notify = (message: string, kind: 'success' | 'error' | 'warning', key?: string) => void

interface ManifestEntry {
  id: number
  path: string
  size: number
  url: string
}

interface FileHandleLike {
  getFile(): Promise<{ size: number }>
  createWritable(): Promise<{ write(data: Blob): Promise<void>; close(): Promise<void> } & WritableStream>
}

interface DirHandleLike {
  getDirectoryHandle(name: string, options?: { create?: boolean }): Promise<DirHandleLike>
  getFileHandle(name: string, options?: { create?: boolean }): Promise<FileHandleLike>
  queryPermission?(descriptor: { mode: 'readwrite' }): Promise<PermissionState>
  requestPermission?(descriptor: { mode: 'readwrite' }): Promise<PermissionState>
}

type PickerWindow = Window & {
  showDirectoryPicker?: (options?: { mode?: 'readwrite'; id?: string }) => Promise<DirHandleLike>
}

export function mediaDownloadUrl(id: number): string {
  return `/api/download/media/${id}`
}

export function zipDownloadUrl(kind: CollectionKind, key: string | number): string {
  return `/api/download/${kind}/${encodeURIComponent(String(key))}`
}

export function supportsDirectoryDownload(): boolean {
  return typeof (window as PickerWindow).showDirectoryPicker === 'function'
}

function startLinkDownload(url: string) {
  const a = document.createElement('a')
  a.href = url
  a.download = ''
  a.rel = 'noopener'
  a.style.display = 'none'
  document.body.appendChild(a)
  a.click()
  window.setTimeout(() => a.remove(), 1000)
}

export function downloadMedia(id: number, notify: Notify) {
  startLinkDownload(mediaDownloadUrl(id))
  notify('Download started', 'success')
}

// --- Remembered folder (IndexedDB) ---

const DB_NAME = 'atriolyric-download'
const STORE = 'handles'
const FOLDER_KEY = 'folder'

function openDb(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, 1)
    req.onupgradeneeded = () => req.result.createObjectStore(STORE)
    req.onsuccess = () => resolve(req.result)
    req.onerror = () => reject(req.error)
  })
}

async function loadFolder(): Promise<DirHandleLike | null> {
  try {
    const db = await openDb()
    return await new Promise((resolve) => {
      const req = db.transaction(STORE).objectStore(STORE).get(FOLDER_KEY)
      req.onsuccess = () => resolve((req.result as DirHandleLike | undefined) ?? null)
      req.onerror = () => resolve(null)
    })
  } catch {
    return null
  }
}

async function saveFolder(handle: DirHandleLike) {
  try {
    const db = await openDb()
    await new Promise<void>((resolve) => {
      const tx = db.transaction(STORE, 'readwrite')
      tx.objectStore(STORE).put(handle, FOLDER_KEY)
      tx.oncomplete = () => resolve()
      tx.onerror = () => resolve()
    })
  } catch {
    // Not persisting just means the picker shows again next time.
  }
}

/** The chosen download folder: the remembered one when its permission can
 * be (re)granted, else a fresh pick. 'cancelled' when the user dismissed the
 * picker; null when the API failed in some other way. */
async function chooseFolder(): Promise<DirHandleLike | 'cancelled' | null> {
  try {
    const stored = await loadFolder()
    if (stored) {
      try {
        let perm = (await stored.queryPermission?.({ mode: 'readwrite' })) ?? 'denied'
        if (perm !== 'granted') perm = (await stored.requestPermission?.({ mode: 'readwrite' })) ?? 'denied'
        if (perm === 'granted') return stored
      } catch {
        // fall through to a fresh pick
      }
    }
    const picked = await (window as PickerWindow).showDirectoryPicker!({
      mode: 'readwrite',
      id: 'atriolyric-downloads',
    })
    await saveFolder(picked)
    return picked
  } catch (err) {
    if (err instanceof DOMException && err.name === 'AbortError') return 'cancelled'
    return null
  }
}

async function writeEntry(root: DirHandleLike, entry: ManifestEntry): Promise<'written' | 'skipped'> {
  const parts = entry.path.split('/').filter((p) => p && p !== '.' && p !== '..')
  const name = parts.pop()
  if (!name) throw new Error('bad path')
  let dir = root
  for (const part of parts) dir = await dir.getDirectoryHandle(part, { create: true })
  try {
    const existing = await (await dir.getFileHandle(name)).getFile()
    if (existing.size === entry.size) return 'skipped'
  } catch {
    // not there yet
  }
  const res = await fetch(entry.url)
  if (!res.ok) throw new Error(`download failed (${res.status})`)
  const handle = await dir.getFileHandle(name, { create: true })
  const writable = await handle.createWritable()
  if (res.body) await res.body.pipeTo(writable)
  else {
    await writable.write(await res.blob())
    await writable.close()
  }
  return 'written'
}

async function downloadIntoFolder(
  root: DirHandleLike,
  kind: CollectionKind,
  key: string | number,
  notify: Notify,
): Promise<boolean> {
  const res = await fetch(`/api/download/${kind}/${encodeURIComponent(String(key))}/manifest`)
  if (!res.ok) return false
  const manifest = (await res.json()) as ManifestEntry[]
  let failed = 0
  for (let i = 0; i < manifest.length; i++) {
    notify(`Downloading ${i + 1}/${manifest.length}`, 'success', 'download-progress')
    try {
      await writeEntry(root, manifest[i])
    } catch {
      failed += 1
    }
  }
  if (failed === 0) notify(`Downloaded ${manifest.length} files`, 'success', 'download-progress')
  else notify(`Downloaded with ${failed} of ${manifest.length} files failed`, 'warning', 'download-progress')
  return true
}

/** Album/artist/playlist download. Falls back to the ZIP whenever the
 * folder route is unavailable or fails before anything was written. */
export async function downloadCollection(kind: CollectionKind, key: string | number, notify: Notify) {
  if (supportsDirectoryDownload()) {
    const folder = await chooseFolder()
    if (folder === 'cancelled') return
    if (folder) {
      try {
        if (await downloadIntoFolder(folder, kind, key, notify)) return
      } catch {
        notify('Could not write to that folder - downloading a ZIP instead.', 'warning', 'download-progress')
      }
    }
  }
  startLinkDownload(zipDownloadUrl(kind, key))
  notify('Download started', 'success')
}
