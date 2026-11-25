import { useEffect, useId, useState } from 'react'
import { api } from '../api'
import type { BackgroundPass, BackgroundStatus } from '../types'

// The counts change slowly (a track takes seconds) and the server caches them
// for 15 s, so this polls at the Admin panel's idle pace whatever runs - and,
// like the rest of the panel, only while the page is visible and the Admin
// tab is shown (a hidden tab's effects are disconnected).
const POLL_MS = 30_000

function stateText(pass: BackgroundPass): string {
  switch (pass.state) {
    case 'running':
      return 'Measuring now'
    case 'waiting':
      return 'Waiting for volume levelling to finish'
    case 'disabled':
      return 'Turned off'
    default:
      return pass.pending > 0 ? 'Not running right now' : 'Up to date'
  }
}

/** A rough time left, only while the pass runs and the server knows its rate
 * (it reports one once a run has gone on for a few minutes). */
function timeLeft(pass: BackgroundPass): string | null {
  if (pass.state !== 'running' || !pass.rate_per_minute || pass.pending <= 0) return null
  const hours = pass.pending / pass.rate_per_minute / 60
  if (hours < 1) return 'less than an hour left'
  const rounded = Math.round(hours)
  return `about ${rounded.toLocaleString()} ${rounded === 1 ? 'hour' : 'hours'} left`
}

function PassRow({ title, pass, failedText }: { title: string; pass: BackgroundPass; failedText: string }) {
  const titleId = useId()
  const counts = `${pass.done.toLocaleString()} of ${pass.total.toLocaleString()} tracks`
  const details = [
    counts,
    pass.pending > 0 && pass.state !== 'disabled' ? `${pass.pending.toLocaleString()} to go` : null,
    timeLeft(pass),
  ].filter(Boolean)
  return (
    <li className="admin-background-row">
      <div className="admin-background-heading">
        <strong id={titleId}>{title}</strong>
        <span>{stateText(pass)}</span>
      </div>
      <progress
        aria-labelledby={titleId}
        aria-valuetext={counts}
        value={pass.done}
        max={Math.max(pass.total, 1)}
      />
      <small>{details.join(' · ')}</small>
      {pass.failed > 0 && <small>{pass.failed.toLocaleString()} {failedText}</small>}
    </li>
  )
}

/** Admin → Background work: how far the loudness pass (volume levelling)
 * and audio analysis have got over the library. Shown once loaded. */
export function BackgroundWorkSection() {
  const [status, setStatus] = useState<BackgroundStatus | null>(null)

  useEffect(() => {
    let cancelled = false
    async function load() {
      if (document.visibilityState !== 'visible') return
      try {
        const next = await api.adminBackgroundStatus()
        if (!cancelled) setStatus(next)
      } catch {
        // Keep what was shown; the next poll tries again.
      }
    }
    void load()
    const pollId = window.setInterval(() => void load(), POLL_MS)
    document.addEventListener('visibilitychange', load)
    return () => {
      cancelled = true
      window.clearInterval(pollId)
      document.removeEventListener('visibilitychange', load)
    }
  }, [])

  if (!status) return null
  return (
    <section className="admin-section" aria-labelledby="admin-background-work">
      <div className="admin-section-heading">
        <h2 id="admin-background-work">Background work</h2>
      </div>
      <ul className="admin-background-list">
        <PassRow title="Volume levelling" pass={status.loudness} failedText="could not be measured" />
        <PassRow title="Sound analysis" pass={status.audio_analysis} failedText="could not be analysed" />
      </ul>
    </section>
  )
}
