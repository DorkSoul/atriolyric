import { useEffect, useMemo, useRef } from 'react'
import { api } from '../api'
import { errorMessage } from '../errorMessage'
import { scrollBehavior } from '../motion'
import { noteLyrics } from '../useHasLyrics'
import { usePendingAsync } from '../usePendingAsync'
import { ErrorState, LoadingState } from './ErrorState'
import { usePlayerTime } from '../PlayerContext'
import type { MediaItem } from '../types'
import { activeLyricIndex, parseLyrics } from '../lyrics'
import { MusicNoteIcon } from './Icons'
import { LibraryLinks } from './LibraryLinks'
import { closeModalThen } from '../modalHistory'
import { Modal } from './Modal'

/** Lyrics for a track: asked again while the server is still looking them up
 * (`pending`), and the answer shared with the Lyrics control (useHasLyrics). */
function useLyrics(trackId: number) {
  const state = usePendingAsync(async (signal) => {
    const result = await api.getLyrics(trackId, signal)
    noteLyrics(trackId, result)
    return result
  }, [trackId])
  // Still being looked up: neither the lyrics nor "none" yet. After the time
  // limit an answer that never settled reads as "none found".
  const looking = state.status !== 'error' && (state.data === undefined || state.fetchingMore)
  return { ...state, looking }
}

interface Props {
  track: MediaItem
  onClose: () => void
  onOpenArtist?: (artist: string) => void
}

export function LyricsPanel({ track, onClose, onOpenArtist }: Props) {
  // Timed (LRC) lyrics read as clean lines here, without the tags.
  const state = useLyrics(track.id)
  const lyrics = useMemo(() => (state.data ? parseLyrics(state.data.plain, state.data.synced).plain : null), [state.data])
  const source = state.data?.source ?? null

  return (
    <Modal
      title={track.title}
      subtitle={
        <LibraryLinks
          artist={track.artist}
          artists={track.artists}
          // Close first (dropping this dialog's history entry), then open the
          // artist: Back from there returns to the page the lyrics were on.
          onArtist={onOpenArtist && ((artist) => closeModalThen(onClose, () => onOpenArtist(artist)))}
        />
      }
      onClose={onClose}
      className="lyrics-content"
      history
    >
      <div className="lyrics-body">
        {state.status === 'error' ? (
          <ErrorState message={errorMessage(state.error, 'Could not load the lyrics.')} onRetry={state.retry} />
        ) : state.data === undefined ? (
          <LoadingState />
        ) : state.looking ? (
          <p className="loading" role="status">Looking for lyrics…</p>
        ) : lyrics === null ? (
          <p className="empty">No lyrics found for this song.</p>
        ) : (
          <>
            <pre className="lyrics-text">{lyrics}</pre>
            {source && source !== 'cached' && <p className="lyrics-source">Source: {source}</p>}
          </>
        )}
      </div>
    </Modal>
  )
}

/** Lyrics on the Now Playing screen (the lower half of a desktop's left
 * column, the Lyrics tab on a phone): the
 * line being sung is highlighted and kept in view when the lyrics are timed,
 * plain text otherwise. */
export function LyricsInline({ track }: { track: MediaItem }) {
  const { currentTime } = usePlayerTime()
  const state = useLyrics(track.id)
  const result = state.data?.pending ? undefined : state.data
  const bodyRef = useRef<HTMLDivElement>(null)
  const activeRef = useRef<HTMLParagraphElement>(null)

  const parsed = useMemo(() => parseLyrics(result?.synced, result?.plain), [result])
  const synced = parsed.synced
  const activeIndex = activeLyricIndex(synced, currentTime)

  useEffect(() => {
    const body = bodyRef.current
    const line = activeRef.current
    if (!body || !line) return
    body.scrollTo?.({
      top: line.offsetTop - body.clientHeight / 2 + line.clientHeight / 2,
      behavior: scrollBehavior(),
    })
  }, [activeIndex])

  const plain = parsed.plain
  // Where the lyrics came from, as the lyrics dialog says it.
  const source = result?.source
  const sourceLine = source && source !== 'cached' ? <p className="lyrics-inline-source">Source: {source}</p> : null

  return (
    <div className={`lyrics-inline${synced.length > 0 ? ' lyrics-inline-synced' : ''}`} ref={bodyRef} data-testid="lyrics-inline">
      {state.status === 'error' ? (
        <p className="lyrics-inline-empty" role="alert">
          {errorMessage(state.error, 'Could not load the lyrics.')}{' '}
          <button type="button" onClick={state.retry}>
            Retry
          </button>
        </p>
      ) : state.data === undefined ? (
        <p className="lyrics-inline-empty">Loading lyrics…</p>
      ) : state.looking ? (
        <p className="lyrics-inline-empty" role="status">Looking for lyrics…</p>
      ) : synced.length > 0 ? (
        <>
          {synced.map((line, index) => (
            <p
              key={index}
              ref={index === activeIndex ? activeRef : undefined}
              className={`lyrics-inline-line${index === activeIndex ? ' lyrics-inline-active' : ''}`}
            >
              {line.text || <MusicNoteIcon />}
            </p>
          ))}
          {sourceLine}
        </>
      ) : plain ? (
        <>
          <pre className="lyrics-inline-plain">{plain}</pre>
          {sourceLine}
        </>
      ) : (
        <p className="lyrics-inline-empty">No lyrics found for this song.</p>
      )}
    </div>
  )
}
