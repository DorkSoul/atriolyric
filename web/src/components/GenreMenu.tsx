import { useId, useRef, useState, type KeyboardEvent } from 'react'
import { menuTriggerProps } from '../menuTrigger'
import { useMediaQuery } from '../useMediaQuery'
import { ChevronDownIcon, SearchIcon } from './Icons'
import { Menu, MenuItem, MenuNote } from './Menu'

/** The genre filter (Albums, Artists, Songs): a chip naming the current genre
 * that opens a menu with "All genres" first. A menu rather than a native
 * <select> so it looks the same everywhere and, like the sort menu, becomes a
 * sheet on a phone. `genre` is '' for none; a genre that isn't in `genres`
 * (set from a link) is still shown, as the extra checked option.
 *
 * A library can have hundreds of genres, so a search field at the top
 * narrows the list as you type (any part of the name, ignoring case). */
export function GenreMenu({
  genre,
  genres,
  onGenreChange,
}: {
  genre: string
  genres: string[]
  onGenreChange: (value: string) => void
}) {
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState('')
  const toggleRef = useRef<HTMLButtonElement>(null)
  const searchRef = useRef<HTMLInputElement>(null)
  const menuId = useId()
  // With a mouse the field takes focus so typing filters at once; on a touch
  // screen that would throw up the keyboard over the list, so it waits for a tap.
  const finePointer = useMediaQuery('(hover: hover) and (pointer: fine)')
  const options = genre && !genres.includes(genre) ? [genre, ...genres] : genres
  const needle = query.trim().toLowerCase()
  const shown = needle ? options.filter((g) => g.toLowerCase().includes(needle)) : options
  function close() {
    setOpen(false)
    setQuery('')
  }
  function pick(value: string) {
    onGenreChange(value)
    close()
  }
  // Keys that move through the list (arrows), close it (Escape, Tab) or pick
  // (Enter) go on to the menu; everything else is typing, and must not reach
  // the menu's type-ahead, which would move focus off the field.
  function onSearchKeyDown(event: KeyboardEvent<HTMLInputElement>) {
    if (event.key === 'Enter') {
      event.preventDefault()
      event.stopPropagation()
      if (shown.length > 0) pick(shown[0])
      return
    }
    if (['ArrowDown', 'ArrowUp', 'Escape', 'Tab'].includes(event.key)) return
    event.stopPropagation()
  }
  return (
    <div className="browse-sort-popover">
      <button
        ref={toggleRef}
        type="button"
        className="chip genre-menu-toggle"
        onClick={() => (open ? close() : setOpen(true))}
        title={genre ? `Genre: ${genre}` : 'Filter by genre'}
        {...menuTriggerProps(open, menuId)}
      >
        {/* Named "Genre: Rock" - the visible text is part of the name. */}
        <span className="visually-hidden">Genre: </span>
        <span className="genre-menu-label">{genre || 'All genres'}</span>
        <ChevronDownIcon aria-hidden="true" />
      </button>
      {open && (
        <Menu
          id={menuId}
          className="browse-sort-panel genre-menu-panel"
          label="Genre"
          triggerRef={toggleRef}
          onClose={close}
          initialFocusRef={finePointer ? searchRef : undefined}
        >
          <div className="genre-menu-search">
            <SearchIcon aria-hidden="true" />
            <input
              ref={searchRef}
              type="search"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={onSearchKeyDown}
              placeholder="Search genres"
              aria-label="Search genres"
              autoComplete="off"
              spellCheck={false}
            />
          </div>
          {!needle && (
            <MenuItem choice="radio" checked={genre === ''} className={genre === '' ? 'active' : ''} onSelect={() => pick('')}>
              All genres
            </MenuItem>
          )}
          {needle && shown.length === 0 && <MenuNote className="genre-menu-empty">No genres match</MenuNote>}
          {shown.map((g) => (
            <MenuItem key={g} choice="radio" checked={genre === g} className={genre === g ? 'active' : ''} onSelect={() => pick(g)}>
              {g}
            </MenuItem>
          ))}
        </Menu>
      )}
    </div>
  )
}
