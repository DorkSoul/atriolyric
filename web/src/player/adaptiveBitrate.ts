// Adaptive stream quality: the bitrate ladder and bandwidth estimates.

import { STREAM_QUALITIES, type StreamQuality } from '../settings'

export const ADAPTIVE_SWITCH_COOLDOWN_MS = 10_000
export const ADAPTIVE_MIN_BUFFER_SECONDS = 1.5
export const ADAPTIVE_UPGRADE_BUFFER_SECONDS = 30
export const ADAPTIVE_MEASURED_UPGRADE_STABLE_WINDOW_MS = 20_000
export const ADAPTIVE_MEASURED_UPGRADE_BUFFER_SECONDS = 12
export const ADAPTIVE_BANDWIDTH_SAMPLE_WINDOW_MS = 240_000
// Pick the highest rung that needs at most this share of measured bandwidth.
export const ADAPTIVE_BANDWIDTH_USE_FRACTION = 0.75
export const ADAPTIVE_CURRENT_STREAM_HEADROOM_FACTOR = 1.15

export function adaptiveQualityLadder(maxBitrate: StreamQuality | null): Array<StreamQuality | null> {
  const qualities = [...STREAM_QUALITIES].filter((quality) => maxBitrate == null || quality <= maxBitrate)
  qualities.sort((a, b) => b - a)
  return maxBitrate == null ? [null, ...qualities] : qualities
}

// The highest rung whose bitrate fits within ADAPTIVE_BANDWIDTH_USE_FRACTION
// of the measured bandwidth (the lowest rung if none does). Rungs whose
// bitrate is unknown are skipped.
export function sustainableAdaptiveQuality(
  ladder: Array<StreamQuality | null>,
  measuredBandwidthKbps: number,
  sourceKbps: number | undefined,
): StreamQuality | null {
  const fit = ladder.find((quality) => {
    const bitrate = expectedAdaptiveBitrateKbps(quality, sourceKbps)
    return bitrate != null && bitrate <= measuredBandwidthKbps * ADAPTIVE_BANDWIDTH_USE_FRACTION
  })
  return fit !== undefined ? fit : ladder[ladder.length - 1]
}

// Straight to the sustainable rung when it is lower than `current` (no
// stepping through every rung while the buffer drains). Falls back to one
// rung down when bandwidth is unknown or claims the current rung is fine
// even though the buffer is running dry.
export function adaptiveDowngradeQuality(
  current: StreamQuality | null,
  maxBitrate: StreamQuality | null,
  measuredBandwidthKbps: number | undefined,
  sourceKbps: number | undefined,
): StreamQuality | null | undefined {
  const ladder = adaptiveQualityLadder(maxBitrate)
  const index = ladder.indexOf(current)
  if (index < 0 || index >= ladder.length - 1) return undefined
  if (measuredBandwidthKbps == null) return ladder[index + 1]
  const target = sustainableAdaptiveQuality(ladder, measuredBandwidthKbps, sourceKbps)
  return ladder.indexOf(target) > index ? target : ladder[index + 1]
}

// The rung to move up to, or undefined if the sustainable rung is not higher.
export function adaptiveUpgradeQuality(
  current: StreamQuality | null,
  maxBitrate: StreamQuality | null,
  measuredBandwidthKbps: number | undefined,
  sourceKbps: number | undefined,
): StreamQuality | null | undefined {
  if (measuredBandwidthKbps == null) return undefined
  const ladder = adaptiveQualityLadder(maxBitrate)
  const index = ladder.indexOf(current)
  if (index <= 0) return undefined
  const target = sustainableAdaptiveQuality(ladder, measuredBandwidthKbps, sourceKbps)
  return ladder.indexOf(target) < index ? target : undefined
}

// Upgrades decided from a download of an upcoming track (see usePrefetch).
// After a downgrade every following track is fetched whole, ahead of time, at
// the lower quality and played from that copy in memory, so the playing
// element never touches the network again and the buffer monitor has nothing
// to measure: the quality would stay reduced for the rest of the queue. The
// download itself is the measurement: its bytes over the time they took.

// A smaller download says too little about the connection to act on.
export const ADAPTIVE_PREFETCH_MIN_SAMPLE_BYTES = 64 * 1024
// How long a quality that stalled when it was tried (a rejected upgrade) is
// not offered again by a download's measurement. Without it a connection that
// downloads fast in bursts but cannot hold a stream would go up and down
// every few tracks.
export const ADAPTIVE_REJECTED_UPGRADE_HOLD_MS = 10 * 60_000

/** One whole-track download: its size, the time from the request to its last
 * byte, and the source's own bitrate if the server named it. */
export interface TrackDownloadSample {
  bytes: number
  elapsedMs: number
  sourceKbps?: number
}

/** The download's speed, or undefined when it is too small to judge by. The
 * time includes the wait for the first byte, so this errs on the low side. */
export function downloadThroughputKbps(sample: TrackDownloadSample): number | undefined {
  if (!Number.isFinite(sample.bytes) || !Number.isFinite(sample.elapsedMs)) return undefined
  if (sample.bytes < ADAPTIVE_PREFETCH_MIN_SAMPLE_BYTES || sample.elapsedMs < 0) return undefined
  return (sample.bytes * 8) / Math.max(1, sample.elapsedMs)
}

/** The quality to move up to after a download at `quality` (what the playing
 * track is at too), or undefined to stay: the highest rung the measured speed
 * sustains (the same margin as every other upgrade) that is above `quality`
 * and not in `rejected` - never within the cooldown after a switch. */
export function downloadUpgradeQuality({
  quality,
  maxBitrate,
  sample,
  rejected,
  msSinceLastSwitch,
}: {
  quality: StreamQuality | null
  maxBitrate: StreamQuality | null
  sample: TrackDownloadSample
  rejected: ReadonlyArray<StreamQuality | null>
  msSinceLastSwitch: number
}): StreamQuality | null | undefined {
  if (msSinceLastSwitch < ADAPTIVE_SWITCH_COOLDOWN_MS) return undefined
  const target = adaptiveUpgradeQuality(quality, maxBitrate, downloadThroughputKbps(sample), sample.sourceKbps)
  if (target === undefined) return undefined
  const ladder = adaptiveQualityLadder(maxBitrate)
  const from = ladder.indexOf(quality)
  for (let index = ladder.indexOf(target); index >= 0 && index < from; index += 1) {
    if (!rejected.includes(ladder[index])) return ladder[index]
  }
  return undefined
}

export interface StreamBandwidthEstimate {
  downloadKbps: number
  sourceKbps?: number
  streamKbps?: number
}

export function recentStreamBandwidth(
  source: HTMLMediaElement,
  sourceStartedAt: number,
  now: number,
): StreamBandwidthEstimate | undefined {
  const sourceUrl = source.src || source.currentSrc
  if (!sourceUrl) return undefined

  const sampleCutoff = Math.max(sourceStartedAt - 2_000, now - ADAPTIVE_BANDWIDTH_SAMPLE_WINDOW_MS)
  const samples = performance
    .getEntriesByName(sourceUrl, 'resource')
    .filter((entry): entry is PerformanceResourceTiming =>
      entry.entryType === 'resource' && entry.startTime >= sampleCutoff,
    )
    .map((entry) => {
      const transferMs = entry.responseEnd - entry.responseStart
      const bytes = entry.encodedBodySize || Math.max(0, entry.transferSize - 300)
      const serverTiming = entry.serverTiming ?? []
      const sourceKbps = Number(serverTiming.find((metric) => metric.name === 'source-bitrate')?.description)
      const streamKbps = Number(serverTiming.find((metric) => metric.name === 'stream-bitrate')?.description)
      return {
        downloadKbps: transferMs > 0 && bytes > 0 ? (bytes * 8) / transferMs : 0,
        sourceKbps: Number.isFinite(sourceKbps) && sourceKbps > 0 ? sourceKbps : undefined,
        streamKbps: Number.isFinite(streamKbps) && streamKbps > 0 ? streamKbps : undefined,
      }
    })
    .filter((sample) => Number.isFinite(sample.downloadKbps) && sample.downloadKbps > 0)

  // One completed range request can be an unusually fast burst. Requiring
  // two recent samples and using the lower quartile gives upgrades a useful
  // safety margin without trusting one optimistic request.
  if (samples.length < 2) return undefined
  samples.sort((a, b) => a.downloadKbps - b.downloadKbps)
  const sourceKbps = [...samples].reverse().find((sample) => sample.sourceKbps != null)?.sourceKbps
  const streamKbps = [...samples].reverse().find((sample) => sample.streamKbps != null)?.streamKbps
  return {
    downloadKbps: samples[Math.floor((samples.length - 1) * 0.25)].downloadKbps,
    sourceKbps,
    streamKbps,
  }
}

export function expectedAdaptiveBitrateKbps(
  quality: StreamQuality | null,
  sourceKbps: number | undefined,
): number | undefined {
  if (sourceKbps == null) return quality ?? undefined
  if (quality == null || sourceKbps <= quality * 1.1) return sourceKbps
  return quality
}
