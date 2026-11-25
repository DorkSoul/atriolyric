import { useEffect, useRef, useState } from 'react'
import { useIsDesktop } from '../useIsDesktop'

const LETTERS = ['#', ...Array.from({ length: 26 }, (_, i) => String.fromCharCode(65 + i))]
// The rail sits this far below the top of .app-main's visible area (always,
// scrolled or not - see railOffset) and ends this far above its bottom edge,
// so the whole strip is inside what is visible above the player bar.
const TOP_OFFSET_PX = 4
const BOTTOM_MARGIN_PX = 4
// A letter is a 24px target. When 27 of them are a little short of fitting
// (an 800px-tall phone has 630px for 648px) they shrink to MIN_LETTER_PX so
// all of them stay in view; on a shorter screen they keep 24px and the strip
// scrolls instead.
const LETTER_PX = 24
const MIN_LETTER_PX = 22

/** Which jump-letter a name falls under - the first alphabetic character,
 * uppercased, or '#' for anything else (leading digit/symbol, or blank). */
export function letterFor(name: string | null | undefined): string {
  const ch = (name ?? '').trim()[0]?.toUpperCase()
  return ch && ch >= 'A' && ch <= 'Z' ? ch : '#'
}

/** The first index in `names` (in its current, already-sorted order) that
 * falls under each letter - used both to know which letters are jumpable
 * and where to jump to. */
export function buildAlphabetIndex(names: (string | null | undefined)[]): Map<string, number> {
  const map = new Map<string, number>()
  names.forEach((name, i) => {
    const letter = letterFor(name)
    if (!map.has(letter)) map.set(letter, i)
  })
  return map
}

interface Props {
  firstIndexByLetter: Map<string, number>
  /** `offsetPx`: how much of the top of the scroller the (sticky) desktop
   * row covers, so the jumped-to item lands just below it. */
  onJump: (index: number, offsetPx?: number) => void
  /** Letters past what is loaded so far that may still exist (a paged list):
   * enabled even though they have no index yet. */
  pendingLetters?: Set<string>
  /** Called for a pending letter instead of onJump. */
  onPendingJump?: (letter: string) => void
}

/** An A-Z strip for jumping (a row inside the toolbar on a desktop-width
 * screen, a vertical rail on the right edge on a phone) straight to a letter while browsing
 * alphabetically - only makes sense in that sort order, so the caller
 * should only render this when sort === 'alphabetical'. Letters with no
 * matching item are shown dimmed and disabled rather than hidden, so the
 * strip's shape stays constant and predictable.
 *
 * Deliberately built as a zero-height `position: sticky` wrapper holding
 * an absolutely-positioned inner box, not `position: fixed` - some mobile
 * browsers position `fixed` against the layout viewport rather than the
 * visual one, which lands the bar half off-screen on a real phone (desktop
 * browsers and Playwright's emulated mobile viewports do not show this).
 * Sticky+absolute avoids that whole class of viewport ambiguity while still
 * tracking scroll and staying out of the grid/list's normal layout flow (so
 * it can't shrink their width - see the comment on .alphabet-jump-bar in
 * index.css).
 *
 * All 27 letters need to actually fit in whatever vertical space is
 * visible (not run off the bottom of the screen), which varies by device
 * and by the header's own height - not something a fixed CSS value can
 * get right everywhere, so this measures .app-main's real clientHeight
 * (already excludes the header and player bar, both outside it) and
 * lets flexbox space the letters evenly across exactly that. A
 * ResizeObserver rather than a window `resize` listener - the header's
 * own height can still change after this mounts (e.g. once the search
 * bar's genre <select> options load in), which doesn't fire a window
 * resize event but does change .app-main's clientHeight. */
export function AlphabetJumpBar({ firstIndexByLetter, onJump, pendingLetters, onPendingJump }: Props) {
  const [availableHeight, setAvailableHeight] = useState<number | null>(null)
  // How far below the top of the scroller the (zero-height) wrapper sits at
  // rest, i.e. the page toolbar above it. The wrapper sticks at exactly that
  // distance and the inner box is pulled back up by it, so the rail starts at
  // the top of the visible area before and after scrolling instead of
  // hanging past the bottom until the toolbar has scrolled away.
  const [railOffset, setRailOffset] = useState(0)
  const isDesktop = useIsDesktop()
  const rowRef = useRef<HTMLDivElement>(null)
  const barRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (isDesktop) return
    const main = document.querySelector('.app-main')
    if (!main) return
    const measure = () => {
      setAvailableHeight(main.clientHeight - TOP_OFFSET_PX - BOTTOM_MARGIN_PX)
      const prev = barRef.current?.previousElementSibling
      setRailOffset(
        prev
          ? Math.max(
              0,
              prev.getBoundingClientRect().bottom -
                main.getBoundingClientRect().top +
                main.scrollTop +
                (parseFloat(getComputedStyle(prev).marginBottom) || 0),
            )
          : 0,
      )
    }
    const observer = new ResizeObserver(measure)
    observer.observe(main)
    const prev = barRef.current?.previousElementSibling
    if (prev) observer.observe(prev)
    return () => observer.disconnect()
  }, [isDesktop])

  if (firstIndexByLetter.size === 0 && !pendingLetters?.size) return null

  const fit = availableHeight != null ? availableHeight / LETTERS.length : LETTER_PX
  const letterPx = fit >= MIN_LETTER_PX ? Math.min(LETTER_PX, fit) : LETTER_PX

  function letterButton(letter: string, className: string, offset: () => number) {
    const index = firstIndexByLetter.get(letter)
    const pending = index == null && Boolean(pendingLetters?.has(letter))
    return (
      <button
        key={letter}
        type="button"
        className={className}
        disabled={index == null && !pending}
        onClick={() => {
          if (index != null) onJump(index, offset())
          else if (pending) onPendingJump?.(letter)
        }}
        aria-label={`Jump to ${letter}`}
      >
        {letter}
      </button>
    )
  }

  // Desktop: a row of letters the caller puts inside the page toolbar (next
  // to the genre picker); the toolbar stays at the top of the scroller, so a
  // jump lands the item just below the whole toolbar.
  if (isDesktop) {
    const coveredHeight = () =>
      (rowRef.current?.closest('.page-toolbar') ?? rowRef.current)?.getBoundingClientRect().height ?? 0
    return (
      <div className="alphabet-jump-row" ref={rowRef} role="group" aria-label="Jump to letter">
        {LETTERS.map((letter) => letterButton(letter, 'alphabet-jump-letter', coveredHeight))}
      </div>
    )
  }

  return (
    <div className="alphabet-jump-bar" ref={barRef} role="group" aria-label="Jump to letter" style={{ top: railOffset }}>
      <div
        className="alphabet-jump-bar-inner"
        style={{
          top: TOP_OFFSET_PX - railOffset,
          ...(availableHeight != null ? { height: `${availableHeight}px` } : null),
          ['--alphabet-letter-min' as string]: `${letterPx}px`,
        }}
      >
        {LETTERS.map((letter) => letterButton(letter, 'alphabet-jump-letter', () => 0))}
      </div>
    </div>
  )
}
