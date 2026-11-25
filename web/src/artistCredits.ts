import type { ArtistCredit } from './types'

/** The credits worth offering as separate links: unique names, leaving out
 * album-artist rows whenever the track has performer credits too. */
export function pickableCredits(artists: ArtistCredit[] | undefined | null): ArtistCredit[] {
  if (!artists || artists.length === 0) return []
  const performers = artists.filter((credit) => credit.role !== 'album_artist')
  const source = performers.length > 0 ? performers : artists
  const seen = new Set<string>()
  return source.filter((credit) => {
    if (seen.has(credit.name)) return false
    seen.add(credit.name)
    return true
  })
}

export const ROLE_LABELS: Record<ArtistCredit['role'], string> = {
  main: 'Artist',
  featured: 'Featured',
  album_artist: 'Album artist',
}
