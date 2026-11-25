import { useEffect, useState } from 'react'
import { api } from './api'
import { effectiveArt, useArtRevision, type ArtRef } from './artEntity'
import type { ImageCredit } from './types'

/** The credit to show beside an artist's own picture (a Wikimedia Commons
 * photo's author and licence, as its licence asks); null for any other art.
 * Asked again when the art changes. */
export function useArtCredit(artRef: ArtRef | undefined): ImageCredit | null {
  useArtRevision()
  const custom = artRef ? effectiveArt(artRef).custom : false
  const version = artRef ? effectiveArt(artRef).version : null
  const wanted = artRef && artRef.type === 'artist' && custom ? `${artRef.type}:${artRef.key}:${version}` : null
  const [credit, setCredit] = useState<{ key: string; value: ImageCredit | null } | null>(null)
  useEffect(() => {
    if (!wanted || !artRef) return
    let cancelled = false
    api.getArtCredit(artRef.type, artRef.key).then(
      (value) => {
        if (!cancelled) setCredit({ key: wanted, value })
      },
      () => {},
    )
    return () => {
      cancelled = true
    }
    // `wanted` covers the ref's identity and art version.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [wanted])
  return wanted && credit?.key === wanted ? credit.value : null
}

/** The credit stored with a catalog (want list) artist's picture. */
export function useCatalogArtCredit(mbid: string | null | undefined): ImageCredit | null {
  const [credit, setCredit] = useState<{ mbid: string; value: ImageCredit | null } | null>(null)
  useEffect(() => {
    if (!mbid) return
    let cancelled = false
    api.getCatalogArtCredit('artist', mbid).then(
      (value) => {
        if (!cancelled) setCredit({ mbid, value })
      },
      () => {},
    )
    return () => {
      cancelled = true
    }
  }, [mbid])
  return mbid && credit?.mbid === mbid ? credit.value : null
}
