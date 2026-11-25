import type { Album } from './types'

/** Artist-page headings, in order (the server's AlbumOut.section). */
const SECTION_HEADINGS: Record<string, string> = {
  Album: 'Albums',
  EP: 'EPs',
  Single: 'Singles',
  Live: 'Live',
  Compilation: 'Compilations',
  Other: 'Other releases',
}

export interface AlbumSection<T> {
  /** The section key (Album, EP, ...); '' for the single flat list. */
  key: string
  heading: string
  albums: Album[]
  /** Extra items shown after the albums, e.g. missing releases. */
  extra: T[]
}

/** An artist's albums split into release-type sections, keeping the order
 * they arrived in within each, with `extra` items (missing releases, keyed
 * the same way) placed in their own type's section. `bySection` false gives
 * one unheaded list. Untyped albums count as albums; when everything is an
 * album the lone section needs no heading either. */
export function groupAlbumSections<T = never>(
  albums: Album[],
  bySection: boolean,
  extra: Map<string, T[]> = new Map(),
): AlbumSection<T>[] {
  const allExtra = [...extra.values()].flat()
  if (!bySection) return albums.length || allExtra.length ? [{ key: '', heading: '', albums, extra: allExtra }] : []
  const groups = new Map<string, Album[]>()
  for (const album of albums) {
    const key = album.section && album.section in SECTION_HEADINGS ? album.section : 'Album'
    groups.set(key, [...(groups.get(key) ?? []), album])
  }
  const extraFor = (key: string) =>
    key === 'Other'
      ? [...extra.entries()].filter(([k]) => k === 'Other' || !(k in SECTION_HEADINGS)).flatMap(([, v]) => v)
      : extra.get(key) ?? []
  const sections = Object.keys(SECTION_HEADINGS)
    .map((key) => ({ key, heading: SECTION_HEADINGS[key], albums: groups.get(key) ?? [], extra: extraFor(key) }))
    .filter((section) => section.albums.length > 0 || section.extra.length > 0)
  if (sections.length === 1 && sections[0].key === 'Album') sections[0].heading = ''
  return sections
}
