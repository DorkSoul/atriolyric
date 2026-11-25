// Playback diagnostics in the console and the client debug log.

import { debugLog } from '../clientDebugLog'

export interface PlaybackEndDiagnostic {
  media_item_id: number | null
  current_time: number | null
  duration: number | null
  paused: boolean
  ready_state: number
  visibility: 'visible' | 'hidden'
  is_active_device: boolean
}

/** What PlayerContext keeps about the pending track end (see its
 * `pendingEndMetaRef`). */
export interface PendingEndMeta {
  sentGeneration: number | null
  at: number
  advancedToId: number | null
  endedQueueItemId: number | null
  advancedToQueueItemId: number | null
  earlierEndedQueueItemIds: number[]
}

/** The most track ends kept for one reconnect replay. */
export const MAX_CHAINED_ENDS = 20

export function logPlaybackDiagnostic(event: string, details: Record<string, unknown> = {}) {
  console.info('[Atriolyric playback diagnostic]', JSON.stringify({
    at: new Date().toISOString(),
    event,
    ...details,
  }))
  debugLog(`pb.${event}`, details)
}
