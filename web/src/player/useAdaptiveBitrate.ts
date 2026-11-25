// The refs and state setters passed in are stable, which this rule cannot
// see through the argument object.
/* eslint-disable react-hooks/exhaustive-deps */
import { useEffect, useMemo, useRef, type RefObject, type Dispatch, type SetStateAction } from 'react'
import type { AppSettings } from '../settings'
import type { StreamQuality } from '../settings'
import type { useToast } from '../ToastContext'
import type { MediaItem, PlaybackStateSnapshot } from '../types'
import type { UpcomingTrackDownload } from './usePrefetch'
import {
  ADAPTIVE_CURRENT_STREAM_HEADROOM_FACTOR,
  ADAPTIVE_MEASURED_UPGRADE_BUFFER_SECONDS,
  ADAPTIVE_MEASURED_UPGRADE_STABLE_WINDOW_MS,
  ADAPTIVE_MIN_BUFFER_SECONDS,
  ADAPTIVE_REJECTED_UPGRADE_HOLD_MS,
  ADAPTIVE_SWITCH_COOLDOWN_MS,
  ADAPTIVE_UPGRADE_BUFFER_SECONDS,
  adaptiveDowngradeQuality,
  adaptiveQualityLadder,
  adaptiveUpgradeQuality,
  downloadUpgradeQuality,
  expectedAdaptiveBitrateKbps,
  recentStreamBandwidth,
} from './adaptiveBitrate'

export interface UseAdaptiveBitrateArgs {
  isVideo: boolean
  current: MediaItem | null
  settings: AppSettings
  adaptiveQuality: { trackId: number; quality: StreamQuality | null } | null
  adaptiveNextQuality: { afterTrackId: number; quality: StreamQuality | null } | null
  setAdaptiveQuality: Dispatch<SetStateAction<{ trackId: number; quality: StreamQuality | null } | null>>
  setAdaptiveNextQuality: Dispatch<SetStateAction<{ afterTrackId: number; quality: StreamQuality | null } | null>>
  adaptiveUpgradeStartRef: RefObject<{ fromTrackId: number; quality: StreamQuality | null } | null>
  qualitySwitchPositionRef: RefObject<{ trackId: number; seconds: number } | null>
  adaptiveUpgradeProbeRef: RefObject<{ trackId: number; quality: StreamQuality | null } | null>
  adaptiveRejectedUpgradesRef: RefObject<{
    trackId: number
    qualities: Array<StreamQuality | null>
  } | null>
}

/** The quality the current and next track are fetched at, the notice
 * shown for a downgrade, and the reset on a settings change. */
export function useAdaptiveBitrate({
  isVideo,
  current,
  settings,
  adaptiveQuality,
  adaptiveNextQuality,
  setAdaptiveQuality,
  setAdaptiveNextQuality,
  adaptiveUpgradeStartRef,
  qualitySwitchPositionRef,
  adaptiveUpgradeProbeRef,
  adaptiveRejectedUpgradesRef,
}: UseAdaptiveBitrateArgs) {
  const streamBitrate =
    !isVideo && current && settings.adaptiveBitrateEnabled
      ? adaptiveQuality?.trackId === current.id
        ? adaptiveQuality.quality
        : adaptiveNextQuality && adaptiveNextQuality.afterTrackId !== current.id
          ? adaptiveNextQuality.quality
          : settings.maxBitrateKbps
      : settings.maxBitrateKbps
  // What the track after this one will be fetched at: the pending upgrade
  // if one was decided on this track, else the current quality.
  const nextTrackBitrate =
    settings.adaptiveBitrateEnabled && adaptiveNextQuality && adaptiveNextQuality.afterTrackId === current?.id
      ? adaptiveNextQuality.quality
      : streamBitrate
  const adaptiveQualityNotice = useMemo(() => {
    if (
      !settings.adaptiveBitrateEnabled ||
      !current ||
      adaptiveQuality?.trackId !== current.id ||
      adaptiveQuality.quality === settings.maxBitrateKbps
    ) return null
    const quality = adaptiveQuality.quality == null ? 'original quality' : `${adaptiveQuality.quality} kbps`
    return `Playback quality reduced to ${quality} due to connection speed.`
  }, [adaptiveQuality, current, settings.adaptiveBitrateEnabled, settings.maxBitrateKbps])

  // A manual quality change or turning adaptive mode off/on starts that
  // track from the user's selected quality again. An adaptive downgrade is
  // intentionally kept until the track changes or the user changes a
  // setting, so a temporarily slow connection does not immediately bounce
  // back to the original quality.
  useEffect(() => {
    setAdaptiveQuality(null)
    setAdaptiveNextQuality(null)
    adaptiveUpgradeStartRef.current = null
    qualitySwitchPositionRef.current = null
    adaptiveUpgradeProbeRef.current = null
    adaptiveRejectedUpgradesRef.current = null
  }, [settings.maxBitrateKbps, settings.adaptiveBitrateEnabled])

  return { streamBitrate, nextTrackBitrate, adaptiveQualityNotice }
}

export interface UseAdaptiveBitrateMonitorArgs {
  settings: AppSettings
  isActiveDevice: boolean
  isVideo: boolean
  current: MediaItem | null
  activeAudioRef: RefObject<HTMLAudioElement | null>
  adaptiveRejectedUpgradesRef: RefObject<{
    trackId: number
    qualities: Array<StreamQuality | null>
  } | null>
  adaptiveUpgradeProbeRef: RefObject<{ trackId: number; quality: StreamQuality | null } | null>
  adaptiveUpgradeStartRef: RefObject<{ fromTrackId: number; quality: StreamQuality | null } | null>
  qualitySwitchPositionRef: RefObject<{ trackId: number; seconds: number } | null>
  streamBitrate: StreamQuality | null
  setAdaptiveNextQuality: Dispatch<SetStateAction<{ afterTrackId: number; quality: StreamQuality | null } | null>>
  setAdaptiveQuality: Dispatch<SetStateAction<{ trackId: number; quality: StreamQuality | null } | null>>
  snapshot: PlaybackStateSnapshot | null
  showToast: ReturnType<typeof useToast>['showToast']
}

/** Watches the active audio element's buffer and transfer speed and steps
 * the quality down (now) or up (from the next track). Returns
 * `noteUpcomingDownload`, which steps it up from the track after next when a
 * download of an upcoming track shows the connection can carry more. */
export function useAdaptiveBitrateMonitor({
  settings,
  isActiveDevice,
  isVideo,
  current,
  activeAudioRef,
  adaptiveRejectedUpgradesRef,
  adaptiveUpgradeProbeRef,
  adaptiveUpgradeStartRef,
  qualitySwitchPositionRef,
  streamBitrate,
  setAdaptiveNextQuality,
  setAdaptiveQuality,
  snapshot,
  showToast,
}: UseAdaptiveBitrateMonitorArgs) {
  const adaptiveLastSwitchAtRef = useRef(0)
  const adaptiveSourceStartRef = useRef<{ element: HTMLMediaElement; src: string; at: number } | null>(null)
  // An upgrade decided from a download (noteUpcomingDownload): the track it
  // was decided on and the next one keep their quality, the tracks after
  // them get `quality`. Kept until one of those starts, which is then
  // watched as an upgrade probe like any other upgraded track.
  const downloadUpgradeRef = useRef<{
    decidedOnTrackId: number
    afterTrackId: number
    quality: StreamQuality | null
  } | null>(null)
  // Qualities whose probe stalled, and until when a download's measurement
  // may not offer them again (ADAPTIVE_REJECTED_UPGRADE_HOLD_MS). The
  // per-track list (adaptiveRejectedUpgradesRef) starts empty on every track.
  const rejectedUpgradeHoldRef = useRef(new Map<StreamQuality | null, number>())

  // As for the other adaptive state (see useAdaptiveBitrate): a quality or
  // mode change starts over.
  useEffect(() => {
    downloadUpgradeRef.current = null
    rejectedUpgradeHoldRef.current.clear()
  }, [settings.maxBitrateKbps, settings.adaptiveBitrateEnabled])
  // Use both actual stream-transfer samples and the media buffer: transfer
  // samples estimate whether the next bitrate has headroom, while buffer
  // underruns remain the most direct reason to step down. If the browser
  // cannot provide useful resource timings, retain a conservative buffer-only
  // probe and reject a probe that stalls until the next track.
  useEffect(() => {
    if (!settings.adaptiveBitrateEnabled || !isActiveDevice || isVideo || !current) return
    const activeElement = activeAudioRef.current
    if (!activeElement) return
    const active: HTMLAudioElement = activeElement
    let lowBufferSince = 0
    let stableSince = 0
    // A seek makes the element report waiting/stalled while it jumps to the
    // new position, even on a fast connection or from a fully downloaded
    // blob. That is not a bandwidth problem, so give it time to settle.
    let seekSettleUntil = 0
    const trackId = current.id
    // Bandwidth is measured from the requests made since this source started.
    // That is when the element got this source, not when this effect last
    // ran: it re-runs for things that leave the source alone (a new `current`
    // object after liking the track, a playback-state change), and starting
    // the window again then would leave the next `waiting` with no samples,
    // stepping down one rung blindly instead of to the sustainable rung.
    const sourceKey = active.src
    if (adaptiveSourceStartRef.current?.element !== active || adaptiveSourceStartRef.current.src !== sourceKey) {
      adaptiveSourceStartRef.current = { element: active, src: sourceKey, at: performance.now() }
    }
    const sourceStartedAt = adaptiveSourceStartRef.current.at
    if (adaptiveRejectedUpgradesRef.current?.trackId !== trackId) {
      adaptiveRejectedUpgradesRef.current = { trackId, qualities: [] }
    }
    if (adaptiveUpgradeProbeRef.current?.trackId !== trackId) {
      adaptiveUpgradeProbeRef.current = null
    }
    // This track started at a quality an earlier track decided to upgrade to:
    // treat it as an upgrade probe so a stall on it is remembered as rejected.
    const upgradeStart = adaptiveUpgradeStartRef.current
    if (upgradeStart && upgradeStart.fromTrackId !== trackId) {
      if (upgradeStart.quality === streamBitrate) {
        adaptiveUpgradeProbeRef.current = { trackId, quality: streamBitrate }
      }
      adaptiveUpgradeStartRef.current = null
    }
    // The same for an upgrade a download decided, once a track after the two
    // that keep their quality starts.
    const downloadUpgrade = downloadUpgradeRef.current
    if (downloadUpgrade && downloadUpgrade.decidedOnTrackId !== trackId && downloadUpgrade.afterTrackId !== trackId) {
      if (downloadUpgrade.quality === streamBitrate) {
        adaptiveUpgradeProbeRef.current = { trackId, quality: streamBitrate }
      }
      downloadUpgradeRef.current = null
    }

    function bufferedAhead(): number {
      const time = active.currentTime
      for (let i = 0; i < active.buffered.length; i += 1) {
        const start = active.buffered.start(i)
        const end = active.buffered.end(i)
        if (time >= start - 0.25 && time <= end) return Math.max(0, end - time)
      }
      return 0
    }

    function switchQuality(nextQuality: StreamQuality | null) {
      if (active !== activeAudioRef.current) return
      qualitySwitchPositionRef.current = { trackId, seconds: active.currentTime }
      adaptiveLastSwitchAtRef.current = Date.now()
      adaptiveUpgradeProbeRef.current = null
      // A downgrade supersedes any upgrade that was waiting for the next track.
      adaptiveUpgradeStartRef.current = null
      downloadUpgradeRef.current = null
      setAdaptiveNextQuality(null)
      setAdaptiveQuality({ trackId, quality: nextQuality })
    }

    function evaluateBuffer(forceBuffering = false) {
      // A track held fully in memory (blob:) never touches the network.
      const playingFromMemory = active.src.startsWith('blob:')
      if (
        playingFromMemory ||
        // A seek normally settles in moments; one still pending after the
        // settle window (a resume or scrub on a bad connection) is a real
        // stall and must be able to step quality down, not block the monitor.
        Date.now() < seekSettleUntil ||
        active !== activeAudioRef.current ||
        active.paused ||
        !snapshot?.is_playing
      ) {
        lowBufferSince = 0
        stableSince = 0
        return
      }
      const now = Date.now()
      const ahead = bufferedAhead()
      const bandwidth = recentStreamBandwidth(
        active,
        sourceStartedAt,
        performance.now(),
      )
      const activeStreamBitrateKbps = bandwidth?.streamKbps ??
        expectedAdaptiveBitrateKbps(streamBitrate, bandwidth?.sourceKbps)
      const measuredBandwidthKbps = bandwidth?.downloadKbps
      const nextLower = adaptiveDowngradeQuality(
        streamBitrate,
        settings.maxBitrateKbps,
        measuredBandwidthKbps,
        bandwidth?.sourceKbps,
      )
      const bandwidthCannotSustainCurrent =
        measuredBandwidthKbps != null &&
        activeStreamBitrateKbps != null &&
        measuredBandwidthKbps < activeStreamBitrateKbps * ADAPTIVE_CURRENT_STREAM_HEADROOM_FACTOR
      const buffering =
        forceBuffering ||
        active.readyState < 3 ||
        ahead < ADAPTIVE_MIN_BUFFER_SECONDS ||
        bandwidthCannotSustainCurrent

      const upgradeProbe = adaptiveUpgradeProbeRef.current
      if (
        upgradeProbe?.trackId === trackId &&
        upgradeProbe.quality === streamBitrate &&
        !buffering &&
        ahead >= ADAPTIVE_UPGRADE_BUFFER_SECONDS
      ) {
        // The candidate stream has filled a meaningful buffer, so it has
        // demonstrated that this connection can sustain this bitrate.
        adaptiveUpgradeProbeRef.current = null
      }

      // A track that never got going (nothing buffered at 0:00) is the worst
      // stall of all, so it counts after a startup grace period too.
      const startupStalled = active.currentTime <= 1 && performance.now() - sourceStartedAt > 6_000
      if (buffering && (active.currentTime > 1 || startupStalled)) {
        if (!lowBufferSince) lowBufferSince = now
        // With no completed transfer to measure (a stalled first request), or
        // for a track that never started at all, stepping one rung per
        // cooldown could take minutes: after a long stall go straight to the
        // lowest quality instead (later tracks upgrade again on measured speed).
        const longStall = (measuredBandwidthKbps == null || startupStalled) && active.readyState < 3 && now - lowBufferSince >= 6_000
        const ladderForStall = adaptiveQualityLadder(settings.maxBitrateKbps)
        const lowest = ladderForStall[ladderForStall.length - 1]
        const stallTarget = longStall && streamBitrate !== lowest ? lowest : nextLower
        if (
          stallTarget !== undefined &&
          (forceBuffering || now - lowBufferSince >= 2_000) &&
          now - adaptiveLastSwitchAtRef.current >= ADAPTIVE_SWITCH_COOLDOWN_MS
        ) {
          const failedProbe = adaptiveUpgradeProbeRef.current
          if (failedProbe?.trackId === trackId && failedProbe.quality === streamBitrate) {
            const rejectedUpgrades = adaptiveRejectedUpgradesRef.current
            if (rejectedUpgrades?.trackId === trackId && !rejectedUpgrades.qualities.includes(streamBitrate)) {
              rejectedUpgrades.qualities.push(streamBitrate)
            }
            rejectedUpgradeHoldRef.current.set(streamBitrate, now + ADAPTIVE_REJECTED_UPGRADE_HOLD_MS)
            adaptiveUpgradeProbeRef.current = null
          }
          switchQuality(stallTarget)
          showToast(`Reduced playback quality to ${stallTarget ?? 'original'} due to connection speed.`, 'warning')
          lowBufferSince = 0
          stableSince = 0
          return
        }
      } else {
        lowBufferSince = 0
      }

      // Compute the highest sustainable rung straight from the measured
      // bandwidth, but only queue it: it is applied when the next track
      // starts, never mid-song.
      const upgradeTarget = !buffering
        ? adaptiveUpgradeQuality(streamBitrate, settings.maxBitrateKbps, measuredBandwidthKbps, bandwidth?.sourceKbps)
        : undefined
      const rejectedUpgrades = adaptiveRejectedUpgradesRef.current
      const upgradeWasRejected =
        upgradeTarget !== undefined &&
        rejectedUpgrades?.trackId === trackId &&
        rejectedUpgrades.qualities.includes(upgradeTarget)
      if (
        upgradeTarget !== undefined &&
        !upgradeWasRejected &&
        ahead >= ADAPTIVE_MEASURED_UPGRADE_BUFFER_SECONDS
      ) {
        if (!stableSince) stableSince = now
        if (
          now - stableSince >= ADAPTIVE_MEASURED_UPGRADE_STABLE_WINDOW_MS &&
          now - adaptiveLastSwitchAtRef.current >= ADAPTIVE_SWITCH_COOLDOWN_MS
        ) {
          adaptiveUpgradeStartRef.current = { fromTrackId: trackId, quality: upgradeTarget }
          setAdaptiveNextQuality((prev) =>
            prev?.afterTrackId === trackId && prev.quality === upgradeTarget
              ? prev
              : { afterTrackId: trackId, quality: upgradeTarget },
          )
          stableSince = 0
        }
      } else {
        stableSince = 0
      }
    }

    const onSeeking = () => {
      seekSettleUntil = Date.now() + 4_000
      lowBufferSince = 0
      stableSince = 0
    }
    const onWaiting = () => evaluateBuffer(true)
    const onProgress = () => evaluateBuffer()
    active.addEventListener('seeking', onSeeking)
    active.addEventListener('waiting', onWaiting)
    active.addEventListener('stalled', onWaiting)
    active.addEventListener('progress', onProgress)
    active.addEventListener('timeupdate', onProgress)
    const monitor = window.setInterval(() => evaluateBuffer(), 1_000)
    return () => {
      active.removeEventListener('seeking', onSeeking)
      active.removeEventListener('waiting', onWaiting)
      active.removeEventListener('stalled', onWaiting)
      active.removeEventListener('progress', onProgress)
      active.removeEventListener('timeupdate', onProgress)
      window.clearInterval(monitor)
    }
  }, [current, isActiveDevice, isVideo, settings.adaptiveBitrateEnabled, settings.maxBitrateKbps, showToast, snapshot?.is_playing, streamBitrate])

  // A copy of an upcoming track finished downloading. The monitor above
  // cannot see a connection recover while the playing track comes from memory
  // (it returns at once for a blob: source), which is every track after a
  // downgrade: each next one inherits the lower quality and is fetched whole
  // ahead of time. So the download is the measurement. Never a switch
  // mid-track, and not for the next track either - its copy at the lower
  // quality is the one in memory, and fetching it again would leave the
  // hand-off at the end of this track (on a locked phone, the moment the
  // network is least certain) without a copy. The tracks after it are fetched
  // at the new quality while the next one plays.
  function noteUpcomingDownload(download: UpcomingTrackDownload) {
    if (!settings.adaptiveBitrateEnabled || !isActiveDevice || isVideo || !current) return
    // Only a download at the quality this track plays at says how far above
    // it the connection reaches (one made for an upgrade already decided for
    // the next track is at that quality instead).
    if (download.quality !== streamBitrate || download.nextTrackId === current.id) return
    const now = Date.now()
    const holds = rejectedUpgradeHoldRef.current
    for (const [quality, until] of holds) if (until <= now) holds.delete(quality)
    const rejectedOnThisTrack = adaptiveRejectedUpgradesRef.current?.trackId === current.id
      ? adaptiveRejectedUpgradesRef.current.qualities
      : []
    const quality = downloadUpgradeQuality({
      quality: streamBitrate,
      maxBitrate: settings.maxBitrateKbps,
      sample: download,
      rejected: [...rejectedOnThisTrack, ...holds.keys()],
      msSinceLastSwitch: now - adaptiveLastSwitchAtRef.current,
    })
    if (quality === undefined) return
    const trackId = current.id
    const afterTrackId = download.nextTrackId
    downloadUpgradeRef.current = { decidedOnTrackId: trackId, afterTrackId, quality }
    // This track keeps its quality whatever it got it from (a quality an
    // earlier upgrade set for every later track would otherwise be replaced
    // under it by the line below, mid-song). The next track keeps it too:
    // the track end carries it across (see advanceLocalTrackAfterEnd).
    setAdaptiveQuality((prev) => (prev?.trackId === trackId ? prev : { trackId, quality: streamBitrate }))
    setAdaptiveNextQuality((prev) =>
      prev?.afterTrackId === afterTrackId && prev.quality === quality ? prev : { afterTrackId, quality })
  }

  return { noteUpcomingDownload }
}
