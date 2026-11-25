// Loading policy and retry budgets for media errors (tracks, videos, HLS and radio).

// The server encodes HLS segments on demand, so the first segment after a
// seek can take a long time on a busy or slow machine. hls.js's default
// (give up on a segment after a few seconds and re-request it) just cancels
// and restarts that wait - give the encoder time instead.
export const SLOW_SEGMENT_LOAD_POLICY = {
  default: {
    maxTimeToFirstByteMs: 150000,
    maxLoadTimeMs: 150000,
    timeoutRetry: { maxNumRetry: 2, retryDelayMs: 0, maxRetryDelayMs: 0 },
    errorRetry: { maxNumRetry: 3, retryDelayMs: 1000, maxRetryDelayMs: 8000 },
  },
}

// Waits before each reload after a network or decode error on the playing
// element (tracks, direct-play videos and radio); one reload per entry.
export const MEDIA_RETRY_DELAYS_MS = [1_000, 3_000, 8_000]
// Playing this far past the position a reload resumed from earns the track
// its full retry budget back.
export const MEDIA_RETRY_RESET_AFTER_SECONDS = 5
// A radio stream that has been waiting for data this long is reconnected.
export const RADIO_STALL_LIMIT_MS = 10_000
// MediaError codes (the constants are not on every engine's global).
export const MEDIA_ERR_ABORTED = 1
export const MEDIA_ERR_NETWORK = 2
export const MEDIA_ERR_DECODE = 3

/** Why a radio stream failed: the HTTP status its URL answers now, or null
 * when nothing answered. A media element reports every failure the same way
 * (an error with no status), so after one the app asks once more itself.
 * Only the status is read: the request is aborted as soon as the headers are
 * in, so no audio is downloaded, and the server's stream relay gives its
 * stream slot back when the client goes away (a 429, 404 or 502 never takes
 * one). */
export async function radioStreamStatus(
  url: string,
  timeoutMs = 10_000,
  headers?: Record<string, string>,
): Promise<number | null> {
  const controller = new AbortController()
  const timer = window.setTimeout(() => controller.abort(), timeoutMs)
  try {
    const response = await fetch(url, { signal: controller.signal, cache: 'no-store', credentials: 'same-origin', headers })
    return response.status
  } catch {
    return null
  } finally {
    window.clearTimeout(timer)
    controller.abort()
  }
}

/** What a failed track or video stream answers now. A media element reports
 * a stream it could not fetch at all as "not supported" (code 4), the same
 * as a file it cannot decode, so the code alone cannot tell a lift, a proxy's
 * 502 during a restart or a captive Wi-Fi from a broken file. One byte is
 * asked for; null means nothing answered in time. */
export function trackStreamStatus(url: string, timeoutMs = 10_000): Promise<number | null> {
  return radioStreamStatus(url, timeoutMs, { Range: 'bytes=0-0' })
}

/** Whether a stream that answered like this (see trackStreamStatus) is worth
 * asking for again: nothing answered, the server or the proxy in front of it
 * failed, or it asked to wait. Anything else (the file is served, or is
 * missing, or the session is gone) will not change by retrying. */
export function streamFailureIsTransient(status: number | null): boolean {
  return status == null || status >= 500 || status === 408 || status === 429
}
