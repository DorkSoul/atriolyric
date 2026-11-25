import { useCallback, useEffect, useId, useLayoutEffect, useRef, useState } from 'react'
import { useOutsideClick } from '../useOutsideClick'
import { createPortal } from 'react-dom'
import { useViewActive } from '../viewActive'
import { InfoIcon } from './Icons'

interface HelpTipProps {
  text: string
  label?: string
  className?: string
  buttonClassName?: string
  bubbleClassName?: string
  align?: 'start' | 'center'
}

/** An accessible help bubble rendered above app scroll/stacking contexts. */
export function HelpTip({
  text,
  label = 'Show help',
  className = 'section-help-tip',
  buttonClassName = 'home-rail-help-button',
  bubbleClassName = 'section-help-bubble',
  align = 'center',
}: HelpTipProps) {
  const [pinned, setPinned] = useState(false)
  const [hovered, setHovered] = useState(false)
  const [focused, setFocused] = useState(false)
  const [position, setPosition] = useState<{ top: number; left: number } | null>(null)
  const tooltipId = useId()
  const buttonRef = useRef<HTMLButtonElement>(null)
  const bubbleRef = useRef<HTMLSpanElement>(null)
  // Where the bubble is portaled: <body>, or the modal <dialog> the button is
  // in (a modal dialog is in the top layer; anything outside it is below it).
  const [host, setHost] = useState<Element | null>(null)
  const setButton = useCallback((element: HTMLButtonElement | null) => {
    buttonRef.current = element
    if (element) setHost(element.closest('dialog') ?? document.body)
  }, [])
  const hoverCloseTimeout = useRef<ReturnType<typeof setTimeout> | null>(null)
  // The bubble is portaled to <body>, which a hidden tab's <Activity> does not
  // hide: its hover/focus state would otherwise leave it showing over another tab.
  const active = useViewActive()
  const visible = (pinned || hovered || focused) && active

  function cancelHoverClose() {
    if (hoverCloseTimeout.current != null) {
      clearTimeout(hoverCloseTimeout.current)
      hoverCloseTimeout.current = null
    }
  }

  function scheduleHoverClose() {
    cancelHoverClose()
    hoverCloseTimeout.current = setTimeout(() => {
      setHovered(false)
      setPinned(false)
      hoverCloseTimeout.current = null
    }, 180)
  }

  useEffect(() => () => {
    if (hoverCloseTimeout.current != null) clearTimeout(hoverCloseTimeout.current)
  }, [])

  useOutsideClick(
    buttonRef,
    () => {
      setPinned(false)
      setFocused(false)
    },
    pinned,
  )

  useLayoutEffect(() => {
    if (!visible) return

    const updatePosition = () => {
      const button = buttonRef.current
      const bubble = bubbleRef.current
      if (!button || !bubble) return

      const anchor = button.getBoundingClientRect()
      const bubbleRect = bubble.getBoundingClientRect()
      const edge = 8
      let top = anchor.top - bubbleRect.height - edge
      if (top < edge) top = anchor.bottom + edge
      top = Math.min(top, window.innerHeight - bubbleRect.height - edge)
      top = Math.max(edge, top)

      const desiredLeft = align === 'center'
        ? anchor.left + (anchor.width - bubbleRect.width) / 2
        : anchor.left
      const left = Math.max(edge, Math.min(desiredLeft, window.innerWidth - bubbleRect.width - edge))
      setPosition({ top, left })
    }

    updatePosition()
    window.addEventListener('resize', updatePosition)
    window.addEventListener('scroll', updatePosition, true)
    return () => {
      window.removeEventListener('resize', updatePosition)
      window.removeEventListener('scroll', updatePosition, true)
    }
  }, [align, visible])

  function toggle() {
    if (pinned) {
      setPinned(false)
      setFocused(false)
      buttonRef.current?.blur()
    } else {
      setPinned(true)
    }
  }

  return (
    <span
      className={`${className}${pinned ? ' is-open' : ''}`}
      onMouseEnter={() => {
        cancelHoverClose()
        setHovered(true)
      }}
      onMouseLeave={scheduleHoverClose}
    >
      <button
        ref={setButton}
        type="button"
        className={buttonClassName}
        aria-label={label}
        aria-expanded={visible}
        aria-describedby={visible ? tooltipId : undefined}
        onClick={toggle}
        onFocus={() => setFocused(true)}
        onBlur={() => setFocused(false)}
        onKeyDown={(event) => {
          if (event.key === 'Escape') {
            setPinned(false)
            setFocused(false)
            buttonRef.current?.blur()
          }
        }}
      >
        <InfoIcon />
      </button>
      {visible && host && createPortal(
        // Hover keeps the bubble open while the pointer crosses to it; the
        // button owns focus, Escape and the click toggle.
        // oxlint-disable-next-line jsx-a11y/no-noninteractive-element-interactions
        <span
          ref={bubbleRef}
          id={tooltipId}
          className={bubbleClassName}
          role="tooltip"
          onMouseEnter={() => {
            cancelHoverClose()
            setHovered(true)
          }}
          onMouseLeave={scheduleHoverClose}
          style={{
            position: 'fixed',
            top: position?.top ?? 0,
            left: position?.left ?? 0,
            visibility: position ? 'visible' : 'hidden',
          }}
        >
          {text}
        </span>,
        host,
      )}
    </span>
  )
}
