// The seek bar's time tooltip (see useSeekPreview).

import type { RefObject } from 'react'
import { formatDuration } from '../formatDuration'
import type { SeekPreview, TooltipOffset } from '../useSeekPreview'

export function SeekTooltip({
  preview,
  offset,
  tooltipRef,
}: {
  preview: SeekPreview
  offset: TooltipOffset
  tooltipRef: RefObject<HTMLSpanElement | null>
}) {
  return (
    <span
      ref={tooltipRef}
      className="player-seek-tooltip"
      role="tooltip"
      style={{
        left: `${preview.percent}%`,
        transform: `translateX(calc(-50% + ${offset.shift}px))`,
      }}
    >
      <span className="player-seek-tooltip-arrow" style={{ left: `${offset.arrow}px` }} aria-hidden="true" />
      <span className="player-seek-tooltip-arrow-inner" style={{ left: `${offset.arrow}px` }} aria-hidden="true" />
      {formatDuration(preview.time)}
    </span>
  )
}
