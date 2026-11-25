// Helpers shared by the want list and the library's "Show missing" views
// (kept apart from WantListView so that view can load on demand).
import { ApiError } from './api'
import type { CatalogReleaseGroup } from './wantList'

/** Release-type headings for catalog pages, in order. */
const SECTIONS: [string, string][] = [
  ['Album', 'Albums'],
  ['EP', 'EPs'],
  ['Single', 'Singles'],
  ['Live', 'Live'],
  ['Compilation', 'Compilations'],
  ['Other', 'Other releases'],
]

/** Cover or artist picture for a MusicBrainz id. The server downloads and
 * stores it on first ask (Cover Art Archive / Wikimedia Commons), so the browser never
 * talks to those sites. */
export function catalogArtUrl(kind: 'artist' | 'album', mbid: string, thumb = true) {
  return `/api/catalog/art/${kind}/${mbid}${thumb ? '' : '?thumb=false'}`
}

export function errorMessage(err: unknown, fallback: string) {
  return err instanceof ApiError ? err.message : fallback
}

export function statusLabel(group: { status: string; owned_count: number | null; total_count: number | null }) {
  if (group.status === 'owned') return 'In library'
  if (group.status === 'partial') {
    return group.owned_count != null && group.total_count ? `${group.owned_count}/${group.total_count}` : 'Partly owned'
  }
  return null
}

export function year(date: string | null | undefined) {
  return date ? date.slice(0, 4) : null
}

export function groupCatalogSections(groups: CatalogReleaseGroup[]) {
  return SECTIONS.map(([key, heading]) => ({ heading, groups: groups.filter((g) => (g.section || 'Album') === key) })).filter(
    (section) => section.groups.length > 0,
  )
}
