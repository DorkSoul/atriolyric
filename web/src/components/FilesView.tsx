import { useState } from 'react'
import { api } from '../api'
import { useArtScrollRoot } from '../artScrollRoot'
import { errorMessage } from '../errorMessage'
import { delay, KEPT_TAB_STALE_MS, useAsync } from '../useAsync'
import { SEARCH_DEBOUNCE_MS } from '../useDebouncedValue'
import { usePlayerActions } from '../PlayerContext'
import type { LibraryRootSummary, MediaItem } from '../types'
import { Art } from './Art'
import { ErrorState, LoadingState } from './ErrorState'
import { formatDuration } from '../formatDuration'
import { HelpTip } from './HelpTip'
import { TopBarTitlePortal } from '../headerControlsPortal'
import { useVirtualRange } from '../useVirtualRange'
import { useSettings } from '../SettingsContext'
import { ChevronDownIcon, CloseIcon, MusicNoteIcon, SearchIcon, VideoIcon } from './Icons'
import { FolderIcon } from './BrowseIcons'

const NO_ROOTS: LibraryRootSummary[] = []

interface Props {
  rootId: number | null
  path: string
  onNavigate: (rootId: number, path: string) => void
  onShowLibraries: () => void
  canManageFolderExclusions: boolean
}

export function FilesView({
  rootId: routeRootId,
  path,
  onNavigate,
  onShowLibraries,
  canManageFolderExclusions,
}: Props) {
  const { playQueue } = usePlayerActions()
  // With several library paths the Files tab opens on a list of them; with
  // exactly one it behaves as if that path were the top level.
  // Kept-mounted: shown again soon after, the tab keeps what it has.
  const rootsState = useAsync(() => api.listAccessibleRoots(), [], { staleMs: KEPT_TAB_STALE_MS })
  const roots = rootsState.data ?? NO_ROOTS
  const rootsLoaded = rootsState.data !== undefined
  const [folderAction, setFolderAction] = useState<string | null>(null)
  const [folderMessage, setFolderMessage] = useState('')
  const [searchQuery, setSearchQuery] = useState('')

  const rootId = routeRootId ?? (roots.length === 1 ? roots[0].id : null)
  const showLibraryList = routeRootId == null && roots.length > 1

  // Keyed on the folder: the previous folder's rows never show under a new path.
  const browseState = useAsync(() => api.browse(rootId as number, path), [rootId, path], {
    enabled: rootId != null,
    staleMs: KEPT_TAB_STALE_MS,
  })
  const result = rootId != null ? (browseState.data ?? null) : null
  const loading = rootId != null && browseState.data === undefined && browseState.status !== 'error'
  const browseFailed = rootId != null && browseState.data === undefined && browseState.status === 'error'

  const normalizedSearch = searchQuery.trim()
  const isSearchMode = normalizedSearch.length > 0
  // Typing waits for a pause before asking the server.
  const searchState = useAsync(
    async (signal) => {
      await delay(SEARCH_DEBOUNCE_MS, signal)
      return api.searchFiles(rootId as number, normalizedSearch)
    },
    [rootId, normalizedSearch],
    { enabled: rootId != null && isSearchMode },
  )
  const searchResult = searchState.data ?? null
  const searchLoading = isSearchMode && searchState.data === undefined && searchState.status !== 'error'
  const searchFailed = isSearchMode && searchState.data === undefined && searchState.status === 'error'
  const displayedDirectories = isSearchMode ? searchResult?.folders ?? [] : result?.directories ?? []
  const displayedItems: Array<MediaItem & { relative_path?: string }> = isSearchMode
    ? searchResult?.items ?? []
    : result?.items ?? []

  // Only the rows near the viewport are rendered: a folder can
  // hold thousands of files or subfolders. Folder rows and file rows differ
  // in height, so each kind is its own list with its own window; within a
  // kind every row has one height (see file-browser.css). Nodes in state,
  // not refs: see useVirtualRange's containerNode.
  const scrollRoot = useArtScrollRoot()
  // First guesses only (the real row is measured): see file-browser.css.
  const compact = useSettings().settings.listDensity === 'compact'
  const [folderListNode, setFolderListNode] = useState<HTMLUListElement | null>(null)
  const [fileListNode, setFileListNode] = useState<HTMLUListElement | null>(null)
  const folderWindow = useVirtualRange(displayedDirectories.length, {
    itemHeight: compact ? 36 : 45,
    containerNode: folderListNode,
    scrollContainerRef: scrollRoot,
  })
  const fileWindow = useVirtualRange(displayedItems.length, {
    itemHeight: compact ? 40 : 52,
    containerNode: fileListNode,
    scrollContainerRef: scrollRoot,
  })

  function selectRoot(id: number) {
    onNavigate(id, '')
  }

  function playItem(item: MediaItem, queueItems: readonly MediaItem[] = result?.items ?? []) {
    // Videos play through the same queue/bottom-bar as tracks - the
    // clicked kind just decides which sibling files ride along in the
    // queue with it.
    const siblings = queueItems.filter((i) => (i.kind === 'track') === (item.kind === 'track'))
    const index = siblings.findIndex((i) => i.id === item.id)
    playQueue(siblings, index)
  }

  async function toggleFolderExclusion(dir: NonNullable<typeof result>['directories'][number]) {
    if (rootId == null || dir.is_ignored !== dir.ignored_directly) return
    setFolderAction(dir.path)
    setFolderMessage('')
    try {
      if (dir.ignored_directly) {
        await api.restoreLibraryFolder(rootId, dir.path)
      } else {
        await api.ignoreLibraryFolder(rootId, dir.path)
      }
      // Refetch in place: the rows stay while the new state arrives.
      browseState.reload()
      if (isSearchMode) searchState.reload()
      setFolderMessage(
        dir.ignored_directly
          ? `Restored “${dir.name}” to metadata and discovery.`
          : `New metadata lookups and generated discovery will skip “${dir.name}”. Its files remain playable.`,
      )
    } catch (error) {
      setFolderMessage(error instanceof Error ? error.message : 'Could not update folder exclusion.')
    } finally {
      setFolderAction(null)
    }
  }

  const breadcrumbParts = result?.path ? result.path.split('/') : []
  const currentRootName = roots.find((r) => r.id === rootId)?.display_name ?? 'Root'

  return (
    <div className="files-view">
      {/* The page title is the top bar's; its help tip sits beside it. */}
      <TopBarTitlePortal>
        <HelpTip
          label="About files"
          text="Browse folders and play tracks or videos here. Admins can ignore a folder recursively for everyone using this library root. Newly started metadata lookups and generated mixes, playlists, or recommendations skip its tracks. Ignored files remain visible and playable here; their music files are never changed."
          className="section-help-tip"
          buttonClassName="home-rail-help-button"
          bubbleClassName="section-help-bubble"
          align="start"
        />
      </TopBarTitlePortal>
      {/* Directly under the top bar: this tab's own search (file and folder
          names), with its clear button inside the box. */}
      {!showLibraryList && (
        <div className="files-search-wrap">
          <div className="files-search-control">
            <SearchIcon className="files-search-icon" aria-hidden="true" />
            <input
              type="search"
              aria-label="Search files and folders"
              autoComplete="off"
              placeholder="Search file or folder names in this library…"
              value={searchQuery}
              onChange={(event) => setSearchQuery(event.target.value)}
            />
            {searchQuery && (
              <button
                type="button"
                className="files-search-clear"
                aria-label="Clear file search"
                title="Clear"
                onClick={() => setSearchQuery('')}
              >
                <CloseIcon aria-hidden="true" />
              </button>
            )}
          </div>
        </div>
      )}
      {canManageFolderExclusions && (
        <p className="files-ignore-note">
          Folder exclusions apply to everyone using this library root. New metadata and generated discovery work skips these files; they remain playable.
        </p>
      )}
      {folderMessage && <p className="files-folder-message" role="status">{folderMessage}</p>}

      {result && !isSearchMode && (
        <div className="files-breadcrumb">
          {roots.length > 1 && (
            <>
              <button onClick={onShowLibraries}>Libraries</button>
              <span className="files-breadcrumb-sep">/</span>
            </>
          )}
          <button onClick={() => rootId != null && onNavigate(rootId, '')} disabled={!result.path}>
            {currentRootName}
          </button>
          {breadcrumbParts.map((part, i) => {
            const partPath = breadcrumbParts.slice(0, i + 1).join('/')
            return (
              <span key={partPath} className="files-breadcrumb-part">
                <span className="files-breadcrumb-sep">/</span>
                <button
                  onClick={() => rootId != null && onNavigate(rootId, partPath)}
                  disabled={i === breadcrumbParts.length - 1}
                >
                  {part}
                </button>
              </span>
            )
          })}
        </div>
      )}

      {showLibraryList ? (
        <ul className="files-list">
          {roots.map((r) => (
            <li key={r.id} className="files-row">
              <button className="files-row-btn" onClick={() => selectRoot(r.id)}>
                <span className="files-icon" aria-hidden="true"><FolderIcon /></span>
                <span className="files-item-label">
                  <span className="files-name">{r.display_name}</span>
                </span>
              </button>
            </li>
          ))}
        </ul>
      ) : routeRootId == null && rootsState.status === 'error' && !rootsLoaded ? (
        <ErrorState message={errorMessage(rootsState.error, 'Could not load your libraries.')} onRetry={rootsState.retry} />
      ) : routeRootId == null && !rootsLoaded ? (
        <LoadingState />
      ) : isSearchMode && searchLoading ? (
        <p className="loading" role="status">Searching this library…</p>
      ) : isSearchMode && searchFailed ? (
        <ErrorState message={errorMessage(searchState.error, 'Could not search this library.')} onRetry={searchState.retry} />
      ) : isSearchMode && searchResult && displayedDirectories.length === 0 && displayedItems.length === 0 ? (
        <p className="empty">No matching files or folders.</p>
      ) : isSearchMode && searchResult ? (
        <>
          <p className="files-search-summary" role="status">
            {searchResult.folders.length} folder{searchResult.folders.length === 1 ? '' : 's'} and{' '}
            {searchResult.items.length} file{searchResult.items.length === 1 ? '' : 's'} found
            {searchResult.truncated ? '. More matches may be available; refine your search.' : '.'}
          </p>
          {renderFiles()}
        </>
      ) : loading ? (
        <LoadingState />
      ) : browseFailed ? (
        <ErrorState message={errorMessage(browseState.error, 'Could not open this folder.')} onRetry={browseState.retry} />
      ) : !result ? (
        <p className="empty">No library folders available to browse.</p>
      ) : displayedDirectories.length === 0 && displayedItems.length === 0 ? (
        <p className="empty">This folder is empty.</p>
      ) : (
        renderFiles()
      )}
    </div>
  )

  function renderFiles() {
    return (
      <div className="files-list">
        {!isSearchMode && result?.parent_path != null && (
          <ul className="files-group">
            <li className="files-row">
              <button
                className="files-row-btn"
                onClick={() => rootId != null && onNavigate(rootId, result.parent_path!)}
              >
                <span className="files-icon" aria-hidden="true"><ChevronDownIcon className="files-up-icon" /></span>
                <span className="files-name">..</span>
              </button>
            </li>
          </ul>
        )}
        {displayedDirectories.length > 0 && (
          <ul className="files-group" aria-label="Folders" ref={setFolderListNode}>
            {folderWindow.topPad > 0 && <li className="files-row-spacer" style={{ height: folderWindow.topPad }} aria-hidden />}
            {displayedDirectories.slice(folderWindow.start, folderWindow.end).map((dir) => (
              <li key={dir.path} className="files-row files-folder-row">
                <button
                  className="files-row-btn"
                  onClick={() => {
                    if (rootId == null) return
                    if (isSearchMode) setSearchQuery('')
                    onNavigate(rootId, dir.path)
                  }}
                >
                  <span className="files-icon" aria-hidden="true"><FolderIcon /></span>
                  <span className="files-item-label">
                    <span className="files-name">{dir.name}</span>
                    {isSearchMode && <span className="files-search-path">{dir.path}</span>}
                  </span>
                  {dir.is_ignored && (
                    <span className="files-ignored-label">
                      {dir.ignored_directly
                        ? 'Ignored'
                        : `Ignored by ${dir.ignored_by?.split('/').at(-1) ?? 'parent folder'}`}
                    </span>
                  )}
                </button>
                {canManageFolderExclusions && (dir.ignored_directly || !dir.is_ignored) && (
                  <button
                    className="btn btn-ghost btn-sm files-ignore-button"
                    onClick={() => void toggleFolderExclusion(dir)}
                    disabled={folderAction === dir.path}
                    aria-label={`${dir.ignored_directly ? 'Restore' : 'Ignore'} ${dir.name} ${dir.ignored_directly ? 'for' : 'from'} metadata and discovery`}
                    title={dir.ignored_directly
                      ? 'Allow metadata lookups and generated discovery for this folder again'
                      : 'Recursively skip this folder during metadata lookups and generated discovery; files stay playable'}
                  >
                    {folderAction === dir.path
                      ? 'Saving…'
                      : dir.ignored_directly
                        ? 'Restore folder'
                        : 'Ignore folder'}
                  </button>
                )}
              </li>
            ))}
            {folderWindow.bottomPad > 0 && <li className="files-row-spacer" style={{ height: folderWindow.bottomPad }} aria-hidden />}
          </ul>
        )}
        {displayedItems.length > 0 && (
          <ul className="files-group" aria-label="Files" ref={setFileListNode}>
            {fileWindow.topPad > 0 && <li className="files-row-spacer" style={{ height: fileWindow.topPad }} aria-hidden />}
            {displayedItems.slice(fileWindow.start, fileWindow.end).map((item) => (
              <li key={item.id} className="files-row">
                <button className="files-row-btn" onClick={() => playItem(item, displayedItems)}>
                  <Art
                    mediaItemId={item.id}
                    className="art-xs appearance-list-art"
                    fallback={item.kind === 'track' ? <MusicNoteIcon /> : <VideoIcon />}
                  />
                  <span className="files-item-label">
                    <span className="files-name">{item.title}</span>
                    {isSearchMode && (
                      <span className="files-search-path">
                        {item.relative_path ?? item.title}
                      </span>
                    )}
                  </span>
                  <span className="files-duration">{formatDuration(item.duration_seconds)}</span>
                </button>
              </li>
            ))}
            {fileWindow.bottomPad > 0 && <li className="files-row-spacer" style={{ height: fileWindow.bottomPad }} aria-hidden />}
          </ul>
        )}
      </div>
    )
  }
}
