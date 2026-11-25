import { useId, useRef, useState, type ReactNode } from 'react'
import type { BrowseDirection, BrowseViewMode } from '../browsePrefs'
import { menuTriggerProps } from '../menuTrigger'
import type { BrowseSort } from '../types'
import { ArrowDownIcon, ArrowUpIcon, GridViewIcon, ListViewIcon, SortIcon } from './Icons'
import { GenreMenu } from './GenreMenu'
import { Menu, MenuItem } from './Menu'
import { SearchChip } from './SearchChip'

export const SORT_LABELS: Record<BrowseSort, string> = {
  alphabetical: 'A–Z',
  date_added: 'Date added',
  last_played: 'Last played',
  most_played: 'Most played',
  date_published: 'Date released',
}

interface Props {
  genre: string
  onGenreChange: (value: string) => void
  genres: string[]
  /** The search Home's "View full search" opened the tab with ('' for none). */
  search: string
  onClearSearch: () => void
  sort: BrowseSort
  direction: BrowseDirection
  // Picking the sort that's already active flips `direction` instead of
  // doing nothing - the caller owns that logic (it owns the state), this
  // just reports which option was clicked.
  onSortPick: (sort: BrowseSort) => void
  viewMode: BrowseViewMode
  onViewModeChange: (value: BrowseViewMode) => void
  /** The A-Z jump letters (desktop), placed right after the genre picker. */
  jump?: ReactNode
}

/** The toolbar at the top of the Albums and Artists tabs:
 * the genre filter and an applied search on the left, then the sort menu
 * (picking the current sort again flips its direction) and Grid / List.
 * The tabs have no search box of their own: Home searches the whole library. */
/** The sort button and its menu (Albums, Artists, Songs): the button names
 * the sort and shows its direction; picking the current sort again flips
 * the direction (the caller owns that state - this only reports the pick). */
export function SortMenu<S extends string>({
  labels,
  menuLabels,
  sort,
  direction,
  onSortPick,
}: {
  /** Every option, in menu order, and the button's name for it. */
  labels: Record<S, string>
  /** A longer name in the menu, where the button's would be ambiguous. */
  menuLabels?: Partial<Record<S, string>>
  sort: S
  direction: BrowseDirection
  onSortPick: (sort: S) => void
}) {
  const [sortOpen, setSortOpen] = useState(false)
  const sortToggleRef = useRef<HTMLButtonElement>(null)
  const sortMenuId = useId()
  const DirectionIcon = direction === 'asc' ? ArrowUpIcon : ArrowDownIcon
  const keys = Object.keys(labels) as S[]
  return (
    <div className="browse-sort-popover">
      <button
        ref={sortToggleRef}
        type="button"
        className="chip browse-sort-toggle"
        onClick={() => setSortOpen((o) => !o)}
        title={`Sorted by ${menuLabels?.[sort] ?? labels[sort]}, ${direction === 'asc' ? 'ascending' : 'descending'}`}
        {...menuTriggerProps(sortOpen, sortMenuId)}
      >
        <SortIcon aria-hidden="true" />
        {/* Named "Sort: A–Z" - the visible text is part of the name. */}
        <span className="visually-hidden">Sort: </span>
        <span>{labels[sort]}</span>
        <DirectionIcon aria-hidden="true" className="browse-sort-direction" />
      </button>
      {sortOpen && (
        <Menu
          id={sortMenuId}
          className="browse-sort-panel"
          label="Sort by"
          triggerRef={sortToggleRef}
          onClose={() => setSortOpen(false)}
        >
          {keys.map((s) => (
            <MenuItem
              key={s}
              choice="radio"
              checked={sort === s}
              className={sort === s ? 'active' : ''}
              onSelect={() => {
                onSortPick(s)
                setSortOpen(false)
              }}
            >
              {menuLabels?.[s] ?? labels[s]}
              {sort === s && (direction === 'asc' ? <ArrowUpIcon /> : <ArrowDownIcon />)}
            </MenuItem>
          ))}
        </Menu>
      )}
    </div>
  )
}

export function BrowseToolbar({
  genre,
  onGenreChange,
  genres,
  search,
  onClearSearch,
  sort,
  direction,
  onSortPick,
  viewMode,
  onViewModeChange,
  jump,
}: Props) {
  return (
    <div className="page-toolbar browse-toolbar" role="group" aria-label="Filter and sort">
      <GenreMenu genre={genre} genres={genres} onGenreChange={onGenreChange} />
      {jump}
      <SearchChip query={search} onClear={onClearSearch} />
      <span className="page-toolbar-gap" />
      <div className="page-toolbar-group">
        <SortMenu labels={SORT_LABELS} sort={sort} direction={direction} onSortPick={onSortPick} />
        <ViewModeChips viewMode={viewMode} onViewModeChange={onViewModeChange} />
      </div>
    </div>
  )
}

/** Grid / List as two pressed-state chips. */
export function ViewModeChips({
  viewMode,
  onViewModeChange,
}: {
  viewMode: BrowseViewMode
  onViewModeChange: (value: BrowseViewMode) => void
}) {
  return (
    <>
      <button type="button" className="chip" aria-pressed={viewMode === 'grid'} onClick={() => onViewModeChange('grid')}>
        <GridViewIcon aria-hidden="true" />
        Grid
      </button>
      <button type="button" className="chip" aria-pressed={viewMode === 'list'} onClick={() => onViewModeChange('list')}>
        <ListViewIcon aria-hidden="true" />
        List
      </button>
    </>
  )
}
