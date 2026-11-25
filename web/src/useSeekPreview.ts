// The seek bar's time preview, shared by the player bar and Now Playing:
// where the pointer is on the track as a time, and the tooltip
// that shows it, kept inside the track at either end with its arrow still
// pointing at the pointer.

import { useLayoutEffect, useRef, useState } from 'react'

export interface SeekPreview {
  time: number
  percent: number
}

export interface TooltipOffset {
  shift: number
  arrow: number
}

export function useSeekPreview(duration: number) {
  const trackRef = useRef<HTMLDivElement>(null)
  const tooltipRef = useRef<HTMLSpanElement>(null)
  const [preview, setPreview] = useState<SeekPreview | null>(null)
  const [offset, setOffset] = useState<TooltipOffset>({ shift: 0, arrow: 0 })
  const percent = preview?.percent

  useLayoutEffect(() => {
    const track = trackRef.current
    const tooltip = tooltipRef.current
    if (percent == null || !track || !tooltip) return
    const trackWidth = track.getBoundingClientRect().width
    const tooltipWidth = tooltip.getBoundingClientRect().width
    const pointerX = (trackWidth * percent) / 100
    const left = Math.max(0, Math.min(trackWidth - tooltipWidth, pointerX - tooltipWidth / 2))
    setOffset({ shift: left + tooltipWidth / 2 - pointerX, arrow: pointerX - left })
  }, [percent])

  /** The time under `clientX` on the track, or null without a track or a duration. */
  function valueAt(clientX: number): SeekPreview | null {
    const bounds = trackRef.current?.getBoundingClientRect()
    if (!bounds || duration <= 0) return null
    const fraction = Math.max(0, Math.min(1, (clientX - bounds.left) / bounds.width))
    return { time: fraction * duration, percent: fraction * 100 }
  }

  return { trackRef, tooltipRef, preview, setPreview, offset, valueAt }
}

/** Where a seek-slider key moves to: 5 s for the arrows, 30 s for the page
 * keys, the ends for Home/End; null for any other key or no duration. */
export function seekKeyTarget(key: string, currentTime: number, duration: number): number | null {
  if (duration <= 0) return null
  let next: number | null = null
  if (key === 'ArrowLeft' || key === 'ArrowDown') next = currentTime - 5
  if (key === 'ArrowRight' || key === 'ArrowUp') next = currentTime + 5
  if (key === 'PageDown') next = currentTime - 30
  if (key === 'PageUp') next = currentTime + 30
  if (key === 'Home') next = 0
  if (key === 'End') next = duration
  return next == null ? null : Math.max(0, Math.min(duration, next))
}
