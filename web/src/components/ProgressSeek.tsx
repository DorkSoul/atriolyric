import { useEffect, useRef } from 'react'
import { formatDuration } from '../formatDuration'
import { usePlayer, usePlayerTime } from '../PlayerContext'
import { SeekTooltip } from './SeekPreview'
import { seekKeyTarget, useSeekPreview } from '../useSeekPreview'

/** The player's seek line (a role="slider"): hover shows the time under the
 * pointer, a click seeks, the arrow/Page/Home/End keys step, and on a touch
 * screen a press held for a moment turns into scrubbing that seeks on release
 * (a quick tap does nothing, so brushing the line while scrolling is safe).
 * Shared by the player bar (a thin line across its top edge) and the desktop
 * side panel (an inline bar between the two times). */
export function ProgressSeek({ className = '' }: { className?: string }) {
  const { seekTo } = usePlayer()
  const { currentTime, duration } = usePlayerTime()
  const {
    trackRef: seekTrackRef,
    tooltipRef: seekTooltipRef,
    preview: seekPreview,
    setPreview: setSeekPreview,
    offset: seekTooltipOffset,
    valueAt: seekValueAt,
  } = useSeekPreview(duration)
  const touchScrubRef = useRef<{
    pointerId: number
    clientX: number
    activated: boolean
    timer: number
  } | null>(null)
  const suppressTouchClickUntilRef = useRef(0)
  const previewTimerRef = useRef<number | null>(null)
  const progressPct = duration > 0 ? Math.min(100, (currentTime / duration) * 100) : 0

  function showSeekPreview(clientX: number) {
    const next = seekValueAt(clientX)
    if (!next) return
    if (previewTimerRef.current != null) window.clearTimeout(previewTimerRef.current)
    previewTimerRef.current = null
    setSeekPreview(next)
  }

  function handleSeekPointerEnter(event: React.PointerEvent<HTMLDivElement>) {
    if (event.pointerType === 'mouse' || event.pointerType === 'pen') {
      showSeekPreview(event.clientX)
    }
  }

  function handleSeekPointerMove(event: React.PointerEvent<HTMLDivElement>) {
    const touchScrub = touchScrubRef.current
    if (touchScrub?.pointerId === event.pointerId) {
      touchScrub.clientX = event.clientX
      if (touchScrub.activated) showSeekPreview(event.clientX)
    } else if (event.pointerType === 'mouse' || event.pointerType === 'pen') {
      showSeekPreview(event.clientX)
    }
  }

  function handleSeekPointerDown(event: React.PointerEvent<HTMLDivElement>) {
    if (event.pointerType !== 'touch' || duration <= 0) return
    event.preventDefault()
    const touchScrub = {
      pointerId: event.pointerId,
      clientX: event.clientX,
      activated: false,
      timer: 0,
    }
    touchScrub.timer = window.setTimeout(() => {
      if (touchScrubRef.current !== touchScrub) return
      touchScrub.activated = true
      showSeekPreview(touchScrub.clientX)
    }, 350)
    touchScrubRef.current = touchScrub
  }

  function handleSeekPointerUp(event: React.PointerEvent<HTMLDivElement>) {
    const touchScrub = touchScrubRef.current
    if (touchScrub?.pointerId !== event.pointerId) return
    window.clearTimeout(touchScrub.timer)
    const bounds = seekTrackRef.current?.getBoundingClientRect()
    const releasedOverTrack = Boolean(
      bounds &&
        event.clientX >= bounds.left &&
        event.clientX <= bounds.right &&
        event.clientY >= bounds.top &&
        event.clientY <= bounds.bottom,
    )
    const target = releasedOverTrack ? seekValueAt(event.clientX) : null
    if (touchScrub.activated && target) {
      seekTo(target.time)
      showSeekPreview(event.clientX)
      previewTimerRef.current = window.setTimeout(() => setSeekPreview(null), 900)
    } else {
      setSeekPreview(null)
    }
    touchScrubRef.current = null
    suppressTouchClickUntilRef.current = Date.now() + 700
  }

  function cancelTouchScrub(pointerId?: number) {
    const touchScrub = touchScrubRef.current
    if (!touchScrub || (pointerId != null && touchScrub.pointerId !== pointerId)) return
    window.clearTimeout(touchScrub.timer)
    touchScrubRef.current = null
    setSeekPreview(null)
  }

  function handleSeekClick(event: React.MouseEvent<HTMLDivElement>) {
    if (event.detail === 0 || Date.now() < suppressTouchClickUntilRef.current) return
    const target = seekValueAt(event.clientX)
    if (target) seekTo(target.time)
  }

  function handleSeekKeyDown(event: React.KeyboardEvent<HTMLDivElement>) {
    const nextTime = seekKeyTarget(event.key, currentTime, duration)
    if (nextTime == null) return
    event.preventDefault()
    seekTo(nextTime)
  }

  useEffect(() => () => {
    if (touchScrubRef.current) window.clearTimeout(touchScrubRef.current.timer)
    if (previewTimerRef.current != null) window.clearTimeout(previewTimerRef.current)
  }, [])

  return (
    <div
      ref={seekTrackRef}
      className={`player-progress-track ${className}`}
      role="slider"
      tabIndex={duration > 0 ? 0 : -1}
      aria-label="Seek"
      aria-valuemin={0}
      aria-valuemax={Math.round(duration)}
      aria-valuenow={Math.round(currentTime)}
      aria-valuetext={`${formatDuration(seekPreview?.time ?? currentTime)} of ${formatDuration(duration)}`}
      aria-disabled={duration <= 0}
      onPointerEnter={handleSeekPointerEnter}
      onPointerMove={handleSeekPointerMove}
      onPointerLeave={(event) => {
        if (event.pointerType === 'mouse' || event.pointerType === 'pen') setSeekPreview(null)
      }}
      onPointerDown={handleSeekPointerDown}
      onPointerUp={handleSeekPointerUp}
      onPointerCancel={(event) => cancelTouchScrub(event.pointerId)}
      onClick={handleSeekClick}
      onKeyDown={handleSeekKeyDown}
    >
      <div className="player-progress-fill" style={{ width: `${progressPct}%` }} />
      {seekPreview && <SeekTooltip preview={seekPreview} offset={seekTooltipOffset} tooltipRef={seekTooltipRef} />}
    </div>
  )
}

/** The elapsed or total time beside a seek bar; re-renders on its own. */
export function PlayerTime({ total = false }: { total?: boolean }) {
  const { currentTime, duration } = usePlayerTime()
  return <span className="player-time">{formatDuration(total ? duration : currentTime)}</span>
}
