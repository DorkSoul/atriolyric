import type { MediaItem } from '../types'
import { Art } from './Art'

/** Up to 4 tracks from different artists, so a mix's cover collage gives a
 * real sense of what's in it rather than repeating one artist. */
export function collageTrackIds(tracks: MediaItem[]): number[] {
  const seenArtists = new Set<string>()
  const ids: number[] = []
  for (const track of tracks) {
    const key = track.artist ?? `id:${track.id}`
    if (seenArtists.has(key)) continue
    seenArtists.add(key)
    ids.push(track.id)
    if (ids.length === 4) break
  }
  return ids
}

export function MixCollage({ mediaItemIds }: { mediaItemIds: number[] }) {
  return (
    <div className={`mix-collage count-${mediaItemIds.length}`}>
      {mediaItemIds.map((id, index) => (
        <div key={`${id}:${index}`} className="mix-collage-item">
          <Art mediaItemId={id} className="mix-collage-art" alt="" />
        </div>
      ))}
    </div>
  )
}
