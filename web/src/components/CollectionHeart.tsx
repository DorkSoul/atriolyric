import { useEffect, useState } from 'react'
import { api } from '../api'
import { useToast } from '../ToastContext'
import type { CollectionPreference } from '../types'
import { PreferenceHeart } from './PreferenceHeart'

type Props =
  | { kind: 'album'; artist: string | null; album: string; onChanged?: () => void }
  | { kind: 'artist'; artist: string; onChanged?: () => void }

/** The three-state heart in an album or artist page header. An album is
 * keyed by its owner (album artist, else track artist) as the server works
 * it out from the page's (artist, album) - so the tooltip names the owner,
 * which for a compilation opened from one of its track artists isn't the
 * artist in the page title. Hidden until the server answers, and for
 * anything it doesn't know. */
export function CollectionHeart(props: Props) {
  const { showToast } = useToast()
  // Tagged with the page it was loaded for, so a different page never
  // shows the previous page's heart while its own state loads.
  const [loaded, setLoaded] = useState<{ key: string; value: CollectionPreference } | null>(null)
  const album = props.kind === 'album' ? props.album : null
  const { kind, artist } = props
  const key = JSON.stringify([kind, artist, album])

  useEffect(() => {
    let cancelled = false
    const load =
      kind === 'album' ? api.getAlbumPreference(artist, album ?? '') : api.getArtistPreference(artist ?? '')
    load.then(
      (value) => {
        if (!cancelled) setLoaded({ key, value })
      },
      () => {},
    )
    return () => {
      cancelled = true
    }
  }, [kind, artist, album, key])

  const loadedPreference = loaded?.key === key ? loaded.value : null
  if (!loadedPreference) return null
  const preference: CollectionPreference = loadedPreference
  const setPreference = (value: CollectionPreference) => setLoaded({ key, value })

  // The heart shows `next` while this runs, and goes back by itself if it fails.
  async function change(next: CollectionPreference['state']) {
    try {
      const saved =
        kind === 'album'
          ? await api.setAlbumPreference(artist, album ?? '', next)
          : await api.setArtistPreference(artist ?? '', next)
      setPreference(saved)
    } catch (err) {
      showToast(`Couldn't update this ${kind}.`, 'error')
      throw err
    }
    // Refetch the page's tracks so their rows' dimming follows.
    props.onChanged?.()
  }

  const title =
    kind === 'album' ? `Album: ${preference.album} by ${preference.artist}` : `Artist: ${preference.artist}`
  return (
    <PreferenceHeart
      state={preference.state}
      onChange={change}
      className="favorite-btn collection-heart"
      activeClassName="favorite-active"
      title={title}
      subject={kind === 'album' ? `album ${preference.album ?? album ?? ''}` : `artist ${preference.artist}`}
    />
  )
}
