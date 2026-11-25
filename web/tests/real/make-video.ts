import { execFileSync } from 'node:child_process'
import { mkdirSync, readFileSync, unlinkSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'

/** The title the scanner gives the video (its file name, MKV is not tagged). */
export const VIDEO_TITLE = 'HLS Clip'
/** Its length in seconds: twelve 6 s HLS segments. Longer than hls.js's
 * 30 s forward buffer plus a few seconds of playing, so a seek to 50 s lands
 * beyond what is buffered and has to fetch segments it does not have. */
export const VIDEO_SECONDS = 72

/** Write one tiny video no browser plays natively, so the app has to take the
 * server's HLS transcode path: MPEG-2 video and MP2 audio in Matroska
 * (server/app/video_meta.py is_browser_playable accepts only H.264 yuv420p,
 * or VP8/VP9 in WebM, with AAC/MP3/Opus/Vorbis audio). Only needs `ffmpeg`
 * (lavfi test sources, built-in encoders). */
export function makeVideo(dir: string): string {
  return encode(dir, VIDEO_TITLE, VIDEO_SECONDS)
}

/** The damaged video's title, and the length its header still claims. */
export const CUT_TITLE = 'Cut Clip'
export const CUT_CLAIMED_SECONDS = 24

/** A video cut off part-way, like an interrupted download: its header still
 * says 24 s (that is the duration the scanner stores and the HLS playlist is
 * built from), but the picture and sound stop at about 13 s. The server's
 * encoder writes an empty segment past the real end and answers 404 for it
 * (server/app/hls.py get_segment). */
export function makeCutVideo(dir: string): string {
  const whole = encode(dir, `${CUT_TITLE} (whole)`, CUT_CLAIMED_SECONDS)
  const path = join(dir, `${CUT_TITLE}.mkv`)
  const bytes = readFileSync(whole)
  writeFileSync(path, bytes.subarray(0, Math.floor(bytes.length * 0.55)))
  unlinkSync(whole)
  return path
}

function encode(dir: string, title: string, seconds: number): string {
  mkdirSync(dir, { recursive: true })
  const path = join(dir, `${title}.mkv`)
  execFileSync('ffmpeg', [
    '-nostdin', '-hide_banner', '-loglevel', 'error', '-y',
    '-f', 'lavfi', '-i', `testsrc=size=160x90:rate=25:duration=${seconds}`,
    '-f', 'lavfi', '-i', `sine=frequency=440:sample_rate=48000:duration=${seconds}`,
    '-c:v', 'mpeg2video', '-q:v', '8', '-g', '25',
    '-c:a', 'mp2', '-b:a', '64k', '-ac', '1',
    path,
  ], { stdio: 'inherit' })
  return path
}
