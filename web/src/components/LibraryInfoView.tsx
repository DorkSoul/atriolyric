import { useEffect, useState, type ReactNode } from 'react'
import { api } from '../api'
import { errorMessage } from '../errorMessage'
import { formatDuration } from '../formatDuration'
import {
  formatAddedDate,
  formatBytes,
  formatPlayTime,
  formatShare,
} from '../libraryInfoFormat'
import type { LibraryStats, LibraryStorage } from '../types'
import { useAsync, type AsyncState } from '../useAsync'
import { Art } from './Art'
import { ErrorState } from './ErrorState'
import { DiscIcon } from './Icons'

/** How many recently added albums show before "All N" is used. */
const RECENT_PREVIEW = 7

interface Props {
  onOpenAlbum: (artist: string | null, album: string) => void
  /** Hands the loaded numbers up, so the sidebar counts stay current. */
  onStats?: (stats: LibraryStats) => void
}

function count(n: number, one: string, many = `${one}s`): string {
  return `${n.toLocaleString()} ${n === 1 ? one : many}`
}

function Rows({ children }: { children: ReactNode }) {
  return <dl className="li-kv">{children}</dl>
}

function Row({ label, children }: { label: ReactNode; children: ReactNode }) {
  return (
    <>
      <dt>{label}</dt>
      <dd title={typeof children === 'string' ? children : undefined}>{children}</dd>
    </>
  )
}

function Card({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="li-card">
      <h3>{title}</h3>
      <div className="li-panel">{children}</div>
    </div>
  )
}

function Bars({ items }: { items: { name: string; songs: number }[] }) {
  const max = Math.max(1, ...items.map((item) => item.songs))
  return (
    <div className="li-bars">
      {items.map((item) => (
        <div className="li-bar-row" key={item.name}>
          <span className="li-bar-name" title={item.name}>{item.name}</span>
          <div className="li-bar-track" aria-hidden="true">
            <i style={{ width: `${Math.max(2, (item.songs / max) * 100)}%` }} />
          </div>
          <b>{item.songs.toLocaleString()}</b>
        </div>
      ))}
    </div>
  )
}

/** The screen's shape, shown while the numbers load. */
function Placeholder() {
  const tiles = ['Albums', 'Songs', 'Artists', 'Videos', 'On disk', 'Play time']
  return (
    <div className="li-body" aria-busy="true">
      <p className="loading">Loading…</p>
      <div className="li-stats">
        {tiles.map((label) => (
          <div className="li-stat" key={label}>
            <b className="li-skeleton li-skeleton-value" />
            <span>{label}</span>
          </div>
        ))}
      </div>
      <div className="li-cols">
        {['Recently added', "What's in it"].map((title) => (
          <section key={title}>
            <h2>{title}</h2>
            <div className="li-panel li-skeleton-panel">
              {[0, 1, 2, 3, 4].map((i) => <span className="li-skeleton" key={i} />)}
            </div>
          </section>
        ))}
      </div>
    </div>
  )
}

function storageRows(storage: AsyncState<LibraryStorage[]>): ReactNode {
  const label = (roots: string[], several: boolean) => (several ? `Free on ${roots.join(', ')}` : 'Free space left')
  if (storage.status === 'loading') return <Row label="Free space left"><span className="li-dim">checking…</span></Row>
  if (storage.status === 'error' || !storage.data || storage.data.length === 0) {
    return <Row label="Free space left">unknown</Row>
  }
  const several = storage.data.length > 1
  return storage.data.map((entry) => (
    <Row key={entry.roots.join('\u0000')} label={label(entry.roots, several)}>
      {entry.free_bytes == null
        ? 'unknown'
        : entry.total_bytes != null
          ? `${formatBytes(entry.free_bytes)} of ${formatBytes(entry.total_bytes)}`
          : formatBytes(entry.free_bytes)}
    </Row>
  ))
}

/** Library info: totals, what was added lately, what is in the library, how it
 * sounds and how it is used. Everything comes from one stats request; free disk
 * space is a second one, asked for after the page is showing. */
export function LibraryInfoView({ onOpenAlbum, onStats }: Props) {
  const stats = useAsync((signal) => api.libraryStats(signal), [])
  const ready = stats.status === 'ready' && stats.data !== undefined
  const storage = useAsync((signal) => api.libraryStorage(signal), [], { enabled: ready })
  const [showAll, setShowAll] = useState(false)
  const data = stats.data

  useEffect(() => {
    if (data) onStats?.(data)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data])

  if (data === undefined) {
    if (stats.status === 'error') {
      return <ErrorState message={errorMessage(stats.error, 'Could not load the library info.')} onRetry={stats.retry} />
    }
    return <Placeholder />
  }

  const { totals, contents, quality, listening, growth } = data
  if (totals.songs === 0 && totals.albums === 0 && totals.videos === 0) {
    return <p className="empty">Your library is empty. Add music, then run a scan from Admin.</p>
  }

  const recent = showAll ? data.recently_added : data.recently_added.slice(0, RECENT_PREVIEW)
  const tiles: [string, string][] = [
    ['Albums', totals.albums.toLocaleString()],
    ['Songs', totals.songs.toLocaleString()],
    ['Artists', totals.artists.toLocaleString()],
    ['Videos', totals.videos.toLocaleString()],
    ['On disk', formatBytes(totals.total_bytes)],
    ['Play time', formatPlayTime(totals.total_seconds)],
  ]
  const decades = [
    // Newest decade first ("2020s" before "1990s"); "Unknown" stays last.
    ...data.by_decade.decades
      .map((d) => ({ name: d.decade, songs: d.songs }))
      .sort((a, b) => Number.parseInt(b.name, 10) - Number.parseInt(a.name, 10)),
    ...(data.by_decade.unknown > 0 ? [{ name: 'Unknown', songs: data.by_decade.unknown }] : []),
  ]
  const unscanned = quality.quality_scanned < quality.quality_total

  return (
    <div className="li-body">
      <div className="li-stats">
        {tiles.map(([label, value]) => (
          <div className="li-stat" key={label}>
            <b>{value}</b>
            <span>{label}</span>
          </div>
        ))}
      </div>

      <div className="li-cols">
        <section aria-labelledby="li-recent-h">
          <div className="li-sec-h">
            <h2 id="li-recent-h">Recently added</h2>
            {data.recently_added.length > RECENT_PREVIEW && (
              <button type="button" className="li-link" aria-expanded={showAll} onClick={() => setShowAll(!showAll)}>
                {showAll ? 'Show fewer' : `All ${data.recently_added.length}`}
              </button>
            )}
          </div>
          <div className="li-panel li-recent">
            {recent.length === 0 ? (
              <p className="li-dim li-none">Nothing added yet.</p>
            ) : (
              <>
                <div className="li-row li-head" aria-hidden="true">
                  <span className="li-c-cover" />
                  <span className="li-c-album">Album</span>
                  <span className="li-c-artist">Artist</span>
                  <span className="li-c-year">Year</span>
                  <span className="li-c-format">Format</span>
                  <span className="li-c-added">Added</span>
                </div>
                <ul>
                  {recent.map((album) => (
                    <li key={`${album.artist ?? ''}\u0000${album.album}`}>
                      <button type="button" className="li-row" onClick={() => onOpenAlbum(album.artist, album.album)}>
                        <span className="li-c-cover">
                          <Art
                            mediaItemId={album.representative_media_item_id}
                            artRef={
                              album.art_key
                                ? { type: 'album', key: album.art_key, custom_art: album.custom_art, art_version: album.art_version }
                                : undefined
                            }
                            className="art-xs li-art"
                            fallback={<DiscIcon />}
                          />
                        </span>
                        <b className="li-c-album">{album.album}</b>
                        <span className="li-c-artist">{album.artist ?? 'Unknown artist'}</span>
                        <span className="li-c-year">{album.year ?? ''}</span>
                        <span className="li-c-format">{album.format_label ?? ''}</span>
                        <span className="li-c-added">{formatAddedDate(album.added_at)}</span>
                      </button>
                    </li>
                  ))}
                </ul>
              </>
            )}
          </div>
        </section>

        <section aria-labelledby="li-contents-h">
          <div className="li-sec-h"><h2 id="li-contents-h">What's in it</h2></div>
          <div className="li-panel">
            <Rows>
              {contents.formats.map((f) => (
                <Row key={f.format} label={f.lossless ? `${f.label} (lossless)` : f.label}>
                  {count(f.songs, 'song')} · {formatBytes(f.bytes)}
                </Row>
              ))}
              <Row label="With lyrics">{formatShare(contents.with_lyrics, totals.songs)}</Row>
              <Row label="Loudness measured">{formatShare(contents.loudness_measured, totals.songs)}</Row>
              <Row label="Liked songs">{contents.liked_songs.toLocaleString()}</Row>
              <Row label="Genres">{contents.genre_count.toLocaleString()}</Row>
            </Rows>
          </div>
        </section>
      </div>

      <section aria-labelledby="li-more-h">
        <div className="li-sec-h"><h2 id="li-more-h">Listening and make-up</h2></div>
        <div className="li-grid">
          <Card title="Your listening">
            <Rows>
              <Row label="Plays this week">{listening.plays_this_week.toLocaleString()}</Row>
              <Row label="Plays this year">{listening.plays_this_year.toLocaleString()}</Row>
              <Row label="Listening time this year">
                {formatPlayTime(listening.listening_seconds_this_year)}
                {listening.listening_capped && <span className="li-note">based on stored history</span>}
              </Row>
              <Row label="Most played artist">
                {listening.most_played_artist ? (
                  <span title={`${listening.most_played_artist.plays.toLocaleString()} plays`}>{listening.most_played_artist.name}</span>
                ) : 'none yet'}
              </Row>
              <Row label="Most played album">
                {listening.most_played_album ? (
                  <span title={`${listening.most_played_album.plays.toLocaleString()} plays`}>{listening.most_played_album.album}</span>
                ) : 'none yet'}
              </Row>
              <Row label="Never played">{count(listening.never_played_songs, 'song')}</Row>
            </Rows>
          </Card>

          <Card title="Songs by genre">
            {data.by_genre.top.length === 0 ? <p className="li-dim li-none">No genres yet.</p> : <Bars items={data.by_genre.top} />}
          </Card>

          <Card title="Songs by decade">
            {decades.length === 0 ? <p className="li-dim li-none">No release years yet.</p> : <Bars items={decades} />}
          </Card>

          <Card title="Sound quality">
            <Rows>
              <Row label="Lossless">
                {quality.lossless_share == null
                  ? quality.lossless_songs.toLocaleString()
                  : `${quality.lossless_songs.toLocaleString()} · ${Math.round(quality.lossless_share * 100)}%`}
              </Row>
              <Row label="Hi-res (24-bit)">{count(quality.hi_res_songs, 'song')}</Row>
              <Row label="Average MP3 bitrate">
                {quality.average_mp3_kbps == null ? 'none' : `${Math.round(quality.average_mp3_kbps)} kbps`}
              </Row>
              <Row label="Below 192 kbps">{count(quality.below_192_kbps_songs, 'song')}</Row>
              <Row label="Longest song">
                {quality.longest_song_seconds == null ? 'unknown' : formatDuration(quality.longest_song_seconds)}
              </Row>
              <Row label="Average album length">
                {quality.average_album_seconds == null ? 'unknown' : `${Math.round(quality.average_album_seconds / 60)} min`}
              </Row>
            </Rows>
            {unscanned && (
              <p className="li-note">
                Audio details are known for {quality.quality_scanned.toLocaleString()} of {quality.quality_total.toLocaleString()} songs. Run a scan from Admin to fill in the rest.
              </p>
            )}
          </Card>

          <Card title="Growth and storage">
            <Rows>
              <Row label="Added this month">{count(growth.albums_added_this_month, 'album')}</Row>
              <Row label="Added this year">{count(growth.albums_added_this_year, 'album')}</Row>
              <Row label="Largest album">
                {growth.largest_album ? (
                  <span title={`${growth.largest_album.album}${growth.largest_album.artist ? ` – ${growth.largest_album.artist}` : ''}`}>
                    {formatBytes(growth.largest_album.bytes)}
                  </span>
                ) : 'none'}
              </Row>
              <Row label="Videos take up">{formatBytes(growth.video_bytes)}</Row>
              {storageRows(storage)}
            </Rows>
          </Card>
        </div>
      </section>
    </div>
  )
}
