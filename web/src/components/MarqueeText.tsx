import { useEffect, useRef, useState, type ReactNode } from 'react'

// px/second the text scrolls at once it's moving - deliberately slow/readable
// rather than a fast ticker.
const SCROLL_SPEED = 35
// Seconds held still at each end before it travels again.
const END_PAUSE = 1.5
// How long a pointer rests on a row before its text starts to move: a mouse
// sweeping across a list, or a finger starting a scroll, must not wake every
// row it passes. Keyboard focus starts it at once.
const HOVER_DELAY_MS = 120
const TOUCH_HOLD_MS = 350

interface Props {
  /** Plain text; use `children` instead when the content has links. */
  text?: string
  children?: ReactNode
  className?: string
  /** Scrolls whenever it overflows (the player bar, the current track). Left
   * off, it scrolls only while its row is hovered or has focus (or, on a
   * touch screen, is being held). */
  always?: boolean
}

function prefersReducedMotion() {
  return typeof window.matchMedia === 'function' && window.matchMedia('(prefers-reduced-motion: reduce)').matches
}

/** A single line that, when too long for its container, ends in "…" at rest;
 * and, while it is `always` on or its row is hovered / focused / held, scrolls
 * left to reveal the tail, pauses, scrolls back to the start, pauses, and
 * repeats. Content short enough to fit stays static.
 *
 * Built for lists of thousands of rows (only the mounted ones exist): a row
 * that is not hovered, focused or current does no measuring, observing or
 * animating at all - its ellipsis is plain CSS. With reduced motion nothing
 * moves; the full text is in the `title` tooltip once the row has been
 * pointed at or focused.
 *
 * The text is an inline span at rest, because an atomic inline-block cannot
 * be cut with an ellipsis (it is clipped hard instead); it becomes an
 * inline-block only while it scrolls, which a transform needs. */
export function MarqueeText({ text, children, className = '', always = false }: Props) {
  const containerRef = useRef<HTMLDivElement>(null)
  const textRef = useRef<HTMLSpanElement>(null)
  const [visible, setVisible] = useState(!always)
  const [distance, setDistance] = useState(0)
  const [reduced, setReduced] = useState(prefersReducedMotion)
  const [engaged, setEngaged] = useState(false)
  const [fullText, setFullText] = useState<string | null>(null)

  useEffect(() => {
    if (typeof window.matchMedia !== 'function') return
    const query = window.matchMedia('(prefers-reduced-motion: reduce)')
    const update = () => setReduced(query.matches)
    query.addEventListener('change', update)
    return () => query.removeEventListener('change', update)
  }, [])

  // Hover or focus on the row this sits in (or on the text itself when it is
  // not in a row) engages it; leaving disengages. A list of long titles that
  // all scroll for ever is noise, and costs an animation per row.
  useEffect(() => {
    const container = containerRef.current
    if (!container) return
    const row = container.closest<HTMLElement>('.track-row, .queue-row, .video-card') ?? container
    let timer: ReturnType<typeof setTimeout> | undefined
    const clear = () => {
      clearTimeout(timer)
      timer = undefined
    }
    const on = () => {
      clear()
      setEngaged(true)
    }
    const off = () => {
      clear()
      setEngaged(false)
    }
    const enter = (event: PointerEvent) => {
      clear()
      timer = setTimeout(on, event.pointerType === 'touch' ? TOUCH_HOLD_MS : HOVER_DELAY_MS)
    }
    const focusOff = (event: FocusEvent) => {
      if (!row.contains(event.relatedTarget as Node | null)) off()
    }
    row.addEventListener('pointerenter', enter)
    row.addEventListener('pointerleave', off)
    row.addEventListener('pointercancel', off)
    row.addEventListener('focusin', on)
    row.addEventListener('focusout', focusOff)
    return () => {
      clear()
      row.removeEventListener('pointerenter', enter)
      row.removeEventListener('pointerleave', off)
      row.removeEventListener('pointercancel', off)
      row.removeEventListener('focusin', on)
      row.removeEventListener('focusout', focusOff)
    }
  }, [])

  // Only a text that scrolls for ever (the player bar's, the current row's)
  // waits to be on screen.
  useEffect(() => {
    const container = containerRef.current
    if (!always || !container) {
      setVisible(true)
      return
    }
    const observer = new IntersectionObserver(([entry]) => setVisible(entry.isIntersecting))
    observer.observe(container)
    return () => observer.disconnect()
  }, [always])

  // Nothing is measured until it matters: always-on text while on screen, and
  // any text once its row is engaged. The span's offsetWidth is the text's full
  // width however it is displayed (inline at rest, inline-block while it
  // scrolls) and, unlike scrollWidth or a bounding box, ignores the transform
  // that moves it.
  const active = visible && (always || engaged)
  useEffect(() => {
    const container = containerRef.current
    const textEl = textRef.current
    if (!active || !container || !textEl) return
    function measure() {
      const overflow = textEl!.offsetWidth - container!.clientWidth
      setDistance(overflow > 1 ? overflow : 0)
      setFullText(overflow > 1 ? textEl!.textContent : null)
    }
    measure()
    const observer = new ResizeObserver(measure)
    observer.observe(container)
    observer.observe(textEl)
    return () => observer.disconnect()
  }, [active])

  // The text itself can change under a mounted component (the player bar's
  // next track), and an inline span reports no resize: measure again after
  // every render, but only for text that is active (a no-op when nothing
  // changed - the setters bail out on equal values).
  // oxlint-disable-next-line react-hooks/exhaustive-deps -- every render on purpose, see above
  useEffect(() => {
    const container = containerRef.current
    const textEl = textRef.current
    if (!active || !container || !textEl) return
    const overflow = textEl.offsetWidth - container.clientWidth
    setDistance(overflow > 1 ? overflow : 0)
    setFullText(overflow > 1 ? textEl.textContent : null)
  })

  const overflowing = distance > 0
  const scrolling = overflowing && active && !reduced && (always || engaged)

  useEffect(() => {
    const textEl = textRef.current
    if (!scrolling || !textEl || typeof textEl.animate !== 'function') return
    // Constant speed regardless of length: the travel time follows the
    // distance, the pauses are fixed, and offsets are their shares of the cycle.
    const travel = distance / SCROLL_SPEED
    const total = 2 * travel + 2 * END_PAUSE
    const holdEnd = END_PAUSE / total
    const arrive = holdEnd + travel / total
    const leave = arrive + END_PAUSE / total
    const start = 'translateX(0)'
    const end = `translateX(${-distance}px)`
    const animation = textEl.animate(
      [
        { transform: start, offset: 0 },
        { transform: start, offset: holdEnd, easing: 'ease-in-out' },
        { transform: end, offset: arrive },
        { transform: end, offset: leave, easing: 'ease-in-out' },
        { transform: start, offset: 1 },
      ],
      { duration: total * 1000, iterations: Infinity },
    )
    return () => animation.cancel()
  }, [scrolling, distance])

  return (
    <div
      ref={containerRef}
      className={`marquee ${overflowing && !reduced ? 'marquee-overflowing' : ''} ${className}`}
      data-marquee={scrolling ? 'scroll' : 'static'}
      // Truncated text is readable in full: on hover, and with reduced motion
      // (where it is cut off with an ellipsis and never moves).
      title={fullText ?? undefined}
    >
      <span ref={textRef} className="marquee-text">
        {children ?? text}
      </span>
    </div>
  )
}
