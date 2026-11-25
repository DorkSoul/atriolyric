import { api } from '../api'
import { TopBarBack } from '../headerControlsPortal'
import { errorMessage } from '../errorMessage'
import { KEPT_TAB_STALE_MS, useAsync } from '../useAsync'
import { Art } from './Art'
import { ErrorState, LoadingState } from './ErrorState'
import { VideoList } from './VideoList'
import { FolderIcon } from './BrowseIcons'
import { VideoIcon } from './Icons'

interface Props {
  selectedFolder: string | null
  onSelectFolder: (folder: string | null) => void
  /** The back link: up to the parent folder (null is the Videos tab). */
  onUp: (folder: string | null) => void
  onOpenArtist?: (artist: string) => void
  onOpenAlbum?: (artist: string | null, album: string) => void
}

export function VideoFoldersView({ selectedFolder, onSelectFolder, onUp, onOpenArtist, onOpenAlbum }: Props) {
  const path = selectedFolder ?? ''
  // Keyed on the folder, so a folder's contents never show under another's
  // name. The folders (one quick request) show first; the videos, a longer
  // list, fill in below them.
  const foldersState = useAsync(() => api.listVideoFolders(path), [path], { staleMs: KEPT_TAB_STALE_MS })
  const videosState = useAsync(() => api.listVideos({ dir: path, limit: 500 }), [path], { staleMs: KEPT_TAB_STALE_MS })
  const loading = foldersState.data === undefined && foldersState.status !== 'error'
  const folders = foldersState.data ?? []
  const videos = videosState.data ?? []
  const videosLoading = videosState.data === undefined && videosState.status !== 'error'
  const videosFailed = videosState.data === undefined && videosState.status === 'error'
  const failed = foldersState.status === 'error' && foldersState.data === undefined
  const retry = () => {
    foldersState.retry()
    videosState.retry()
  }
  const parentPath = path.includes('/') ? path.slice(0, path.lastIndexOf('/')) : null
  const heading = path.split('/').pop() ?? ''

  return (
    <div className="videos-view">
      {path && (
        <>
          <TopBarBack label={parentPath ? (parentPath.split('/').pop() ?? 'Videos') : 'Videos'} onBack={() => onUp(parentPath)} />
          <h2 className="home-section-title">{heading}</h2>
        </>
      )}
      {failed ? (
        <ErrorState message={errorMessage(foldersState.error, 'Could not load your videos.')} onRetry={retry} />
      ) : loading || (folders.length === 0 && videosLoading) ? (
        <LoadingState />
      ) : folders.length === 0 && videosFailed ? (
        <ErrorState message={errorMessage(videosState.error, 'Could not load your videos.')} onRetry={videosState.retry} />
      ) : folders.length === 0 && videos.length === 0 ? (
        <p className="empty">No videos yet.</p>
      ) : (
        <>
          {folders.length > 0 && (
            <section className="videos-section" aria-labelledby="videos-folders-heading">
              <div className="sec-head">
                <h3 id="videos-folders-heading">Folders</h3>
              </div>
              <div className="browse-grid video-folder-grid">
                {folders.map((f) => (
                  <button key={f.path} className="browse-card video-folder-tile" onClick={() => onSelectFolder(f.path)}>
                    <span className="video-folder-icon" aria-hidden="true">
                      {f.representative_media_item_id != null ? (
                        <Art
                          mediaItemId={f.representative_media_item_id}
                          className="video-folder-art"
                          fallback={<VideoIcon />}
                          thumbnail
                        />
                      ) : (
                        <FolderIcon />
                      )}
                    </span>
                    <span className="video-folder-text">
                      <span className="browse-title">{f.folder}</span>
                      <span className="browse-subtitle">
                        {f.video_count} video{f.video_count === 1 ? '' : 's'}
                      </span>
                    </span>
                  </button>
                ))}
              </div>
            </section>
          )}
          {folders.length > 0 && videosLoading && <LoadingState />}
          {folders.length > 0 && videosFailed && (
            <ErrorState message={errorMessage(videosState.error, 'Could not load your videos.')} onRetry={videosState.retry} />
          )}
          {videos.length > 0 && (
            <section className="videos-section" aria-labelledby={folders.length > 0 ? 'videos-list-heading' : undefined}>
              {folders.length > 0 && (
                <div className="sec-head">
                  <h3 id="videos-list-heading">Videos</h3>
                </div>
              )}
              <VideoList videos={videos} onOpenArtist={onOpenArtist} onOpenAlbum={onOpenAlbum} />
            </section>
          )}
        </>
      )}
    </div>
  )
}
