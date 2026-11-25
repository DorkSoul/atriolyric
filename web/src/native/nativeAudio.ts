// In the Android app the music is played by native players (Media3), not by
// the page: they keep playing with the screen off, own the notification and
// the lock-screen controls, and read downloaded copies with no network.
//
// The player (PlayerContext and its hooks) is written against two `<audio>`
// elements, the playing one and a standby that holds the next track. Rather
// than give it a second code path, each of those two elements is "backed" by
// one native player here: the element stays in the page and never loads
// anything itself, while its media properties and methods (src, currentTime,
// paused, play(), ...) are answered from the native player's state and its
// media events are dispatched on it as the native state changes. To the
// player it is an audio element like any other.

import { isNativeApp, nativeCall, onNative, type SlotState } from './bridge'

export interface NativeTrackInfo {
  title?: string | null
  artist?: string | null
  album?: string | null
  artUrl?: string | null
}

/** What the notification shows for a source (`mediaId` is the library item
 * in its address, 0 when it has none, as a radio stream); set by the player,
 * which knows the library. */
let describeTrack: (url: string, mediaId: number) => NativeTrackInfo | null = () => null

export function setNativeTrackInfo(lookup: (url: string, mediaId: number) => NativeTrackInfo | null): void {
  describeTrack = lookup
}

/** "a" and "b" are the two music elements (the playing one and the standby);
 * "r" is the radio element and "v" the one that carries a video's sound while
 * the app is in the background. */
export type NativeSlot = 'a' | 'b' | 'r' | 'v'

const backed = new Map<string, BackedAudio>()
const backedElements = new WeakMap<HTMLMediaElement, BackedAudio>()
let listening = false

function mediaIdOf(url: string): number {
  const id = Number(/\/api\/stream\/(\d+)/.exec(url)?.[1])
  return Number.isFinite(id) ? id : 0
}

function absolute(url: string): string {
  try {
    return new URL(url, window.location.href).href
  } catch {
    return url
  }
}

class BackedAudio {
  private src = ''
  // Not from 0: the native players outlive a page (the app swaps pages, and a
  // swiped-away app can come back in the same process), and a state left
  // over from the page before must never match a load of this one.
  private loadId = Math.floor(Math.random() * 1_000_000_000)
  /** A load decided in this task but not yet sent: `src = ...; load();
   * currentTime = start` arrive as three statements and go out as one. */
  private loadPending = false
  private startPosition = 0
  private native: SlotState | null = null
  /** play()/pause() calls the native side has not confirmed yet. While any
   * is out, a state that disagrees with `paused` is merely old, not the
   * system pausing the music. */
  private pendingCommands = 0
  private paused = true
  private readyState = 0
  private position = 0
  private positionAt = 0
  private duration = NaN
  private volume = 1
  private muted = false
  private seeking = false
  private mediaError: { code: number; message: string } | null = null
  private endedFired = false
  /** The native error count last seen for this load: a new error is one
   * that raised the count, so the same failure twice (a retry that fails
   * with the same message) is still reported. */
  private errorsSeen = 0
  /** The page keeps the queue's next track in this element (the standby):
   * the native side may then start it itself the moment the playing track
   * ends, without waiting for the page (see armNativeNext). */
  private armed = false

  private readonly element: HTMLAudioElement
  readonly slot: NativeSlot

  constructor(element: HTMLAudioElement, slot: NativeSlot) {
    this.element = element
    this.slot = slot
    this.install()
  }

  /** The library track this music element holds, 0 when none (or its
   * load failed). The native player has it whole, or is loading it. */
  heldTrackId(): number {
    if ((this.slot !== 'a' && this.slot !== 'b') || !this.src || this.mediaError) return 0
    return mediaIdOf(this.src)
  }

  private fire(type: string) {
    this.element.dispatchEvent(new Event(type))
  }

  private isPlaying(): boolean {
    return !!this.native && this.native.loadId === this.loadId && this.native.playing && !this.paused
  }

  private currentTime(): number {
    if (!this.isPlaying()) return this.position
    const live = this.position + (performance.now() - this.positionAt) / 1000
    return Number.isFinite(this.duration) ? Math.min(live, this.duration) : live
  }

  private install() {
    const define = (name: string, get: () => unknown, set?: (value: never) => void) => {
      Object.defineProperty(this.element, name, { configurable: true, enumerable: true, get, set: set ?? (() => {}) })
    }
    define('src', () => (this.src ? absolute(this.src) : ''), (value: string) => this.setSrc(String(value)))
    define('currentSrc', () => (this.src ? absolute(this.src) : ''))
    define('currentTime', () => this.currentTime(), (value: number) => this.seek(Number(value)))
    define('duration', () => this.duration)
    define('paused', () => this.paused)
    define('ended', () => this.native?.loadId === this.loadId && this.native.playbackState === 'ended')
    define('seeking', () => this.seeking)
    define('readyState', () => this.readyState)
    define('networkState', () => (this.src ? (this.readyState >= 4 ? 1 : 2) : 0))
    define('error', () => this.mediaError)
    define('volume', () => this.volume, (value: number) => {
      this.volume = Math.max(0, Math.min(1, Number(value)))
      this.sendVolume()
    })
    define('muted', () => this.muted, (value: boolean) => {
      this.muted = !!value
      this.sendVolume()
    })
    define('buffered', () => {
      const end = this.native?.loadId === this.loadId ? this.native.buffered : 0
      return { length: end > 0 ? 1 : 0, start: () => 0, end: () => end }
    })
    const element = this.element as HTMLAudioElement & Record<string, unknown>
    element.play = () => this.play()
    element.pause = () => this.pause()
    element.load = () => this.load()
    const removeAttribute = this.element.removeAttribute.bind(this.element)
    element.removeAttribute = (name: string) => {
      if (name.toLowerCase() === 'src') this.setSrc('')
      else removeAttribute(name)
    }
    const getAttribute = this.element.getAttribute.bind(this.element)
    element.getAttribute = (name: string) => (name.toLowerCase() === 'src' ? this.src || null : getAttribute(name))
  }

  private sendVolume() {
    void nativeCall('playerVolume', { slot: this.slot, volume: this.muted ? 0 : this.volume }).catch(() => {})
  }

  private setSrc(value: string) {
    this.src = value
    this.scheduleLoad()
  }

  private load() {
    this.scheduleLoad()
  }

  private scheduleLoad() {
    this.startPosition = 0
    this.position = 0
    this.duration = NaN
    this.readyState = 0
    this.mediaError = null
    this.seeking = false
    this.endedFired = false
    this.native = null
    this.errorsSeen = 0
    // As for a real element: loading a source leaves it paused, with no
    // `pause` event; the caller asks for play() again.
    this.paused = true
    if (this.loadPending) return
    this.loadPending = true
    queueMicrotask(() => this.flushLoad())
  }

  private flushLoad() {
    if (!this.loadPending) return
    this.loadPending = false
    this.loadId += 1
    if (!this.src) {
      void nativeCall('playerUnload', { slot: this.slot }).catch(() => {})
      this.fire('emptied')
      return
    }
    const itemId = mediaIdOf(this.src)
    const info = describeTrack(this.src, itemId) ?? {}
    // Only a music slot may be answered from a downloaded copy of the item:
    // a video's background sound is its own audio-only stream.
    const mediaId = this.slot === 'a' || this.slot === 'b' ? itemId : 0
    this.position = this.startPosition
    this.positionAt = performance.now()
    const loadId = this.loadId
    this.sendVolume()
    void nativeCall('playerLoad', {
      slot: this.slot,
      loadId,
      url: absolute(this.src),
      mediaId,
      next: this.armed,
      position: this.startPosition,
      title: info.title ?? '',
      artist: info.artist ?? '',
      album: info.album ?? '',
      artUrl: info.artUrl ? absolute(info.artUrl) : '',
    }).catch((error: unknown) => {
      if (loadId !== this.loadId) return
      this.mediaError = { code: 4, message: error instanceof Error ? error.message : String(error) }
      this.fire('error')
    })
    this.fire('loadstart')
  }

  private seek(seconds: number) {
    if (!Number.isFinite(seconds)) return
    const target = Math.max(0, seconds)
    this.position = target
    this.positionAt = performance.now()
    this.endedFired = false
    if (this.loadPending) {
      this.startPosition = target
      return
    }
    if (!this.src) return
    this.seeking = true
    this.fire('seeking')
    void nativeCall('playerSeek', { slot: this.slot, position: target }).catch(() => {}).finally(() => {
      this.seeking = false
      this.fire('seeked')
      this.fire('timeupdate')
    })
  }

  private command(method: 'playerPlay' | 'playerPause'): Promise<void> {
    this.pendingCommands += 1
    return nativeCall(method, { slot: this.slot }).finally(() => {
      this.pendingCommands -= 1
      if (this.pendingCommands === 0 && this.native) this.apply(this.native)
    })
  }

  private play(): Promise<void> {
    if (!this.src) return Promise.reject(new DOMException('The element has no supported sources.', 'NotSupportedError'))
    this.flushLoad()
    if (this.paused) {
      this.paused = false
      this.positionAt = performance.now()
      this.fire('play')
    }
    return this.command('playerPlay')
  }

  private pause() {
    if (this.paused) return
    // Freeze the clock where it is before the state says "not playing".
    this.position = this.currentTime()
    this.paused = true
    this.fire('pause')
    void this.command('playerPause').catch(() => {})
  }

  /** Marks this element as holding (or no longer holding) the queue's next
   * track. Sent with the next load, or at once for the source it holds. */
  arm(armed: boolean) {
    if (this.slot !== 'a' && this.slot !== 'b') return
    if (armed === this.armed) return
    this.armed = armed
    // A load still to go out carries the flag itself.
    if (this.loadPending || !this.src) return
    void nativeCall('playerArm', { slot: this.slot, loadId: this.loadId, armed }).catch(() => {})
  }

  /** A state from the native player. */
  apply(state: SlotState) {
    if (state.loadId !== this.loadId || this.loadPending) return
    const before = this.native
    this.native = state
    // The native side started this standby itself (the track before it
    // ended): it is no longer the one waiting.
    if (this.armed && !state.armed && state.playWhenReady) this.armed = false

    if (state.duration != null && state.duration > 0 && state.duration !== this.duration) {
      this.duration = state.duration
      this.fire('durationchange')
      if (this.readyState < 1) {
        this.readyState = 1
        this.fire('loadedmetadata')
      }
    }
    if (!this.seeking) {
      this.position = state.position
      this.positionAt = performance.now()
    }

    // An app too old to count errors: a new message is a new error.
    const isNewError = state.errorCount != null ? state.errorCount > this.errorsSeen : state.error !== before?.error
    if (state.error && isNewError) {
      this.errorsSeen = state.errorCount ?? 0
      const network = /IO_|NETWORK|TIMEOUT/i.test(state.error)
      this.mediaError = { code: network ? 2 : 3, message: state.error }
      this.fire('error')
      return
    }

    if (state.playbackState === 'ready' && this.readyState < 4) {
      if (this.readyState < 1) {
        this.readyState = 1
        this.fire('loadedmetadata')
      }
      this.readyState = 4
      this.fire('loadeddata')
      this.fire('canplay')
      this.fire('canplaythrough')
    } else if (state.playbackState === 'buffering' && this.readyState === 4) {
      this.readyState = 2
      if (!this.paused) this.fire('waiting')
    }

    // The notification, a headset or the system (audio focus, headphones
    // unplugged) changed play/pause without the page asking.
    if (this.pendingCommands === 0 && state.playWhenReady === this.paused) {
      this.paused = !state.playWhenReady
      this.fire(this.paused ? 'pause' : 'play')
    }

    if (state.playing && !before?.playing && !this.paused) this.fire('playing')
    if (state.buffered !== before?.buffered) this.fire('progress')
    if (state.playbackState === 'ended') {
      if (!this.endedFired) {
        this.endedFired = true
        if (Number.isFinite(this.duration)) this.position = this.duration
        this.fire('timeupdate')
        this.fire('ended')
      }
      return
    }
    this.endedFired = false
    if (state.playing || state.position !== before?.position) this.fire('timeupdate')
  }
}

/** Backs a music `<audio>` element with a native player ("a" for the main
 * element, "b" for the standby). Does nothing outside the app. */
export function backWithNativePlayer(element: HTMLAudioElement, slot: NativeSlot): void {
  if (!isNativeApp() || backed.has(slot)) return
  const audio = new BackedAudio(element, slot)
  backed.set(slot, audio)
  backedElements.set(element, audio)
  if (listening) return
  listening = true
  onNative<SlotState>('slot', (state) => backed.get(state.slot)?.apply(state))
}

/** Tells the native side whether `element` (a music element) holds the
 * queue's next track. An armed standby is started natively the moment the
 * playing track ends, so the queue moves on with the screen off even when
 * the page is frozen at that moment; the page catches up when it runs again.
 * Does nothing outside the app or for the radio and video elements. */
export function armNativeNext(element: HTMLMediaElement, armed: boolean): void {
  backedElements.get(element)?.arm(armed)
}

/** Next / Previous from the notification, the lock screen or a headset. */
export function onNativeTransportCommand(handler: (command: 'next' | 'prev') => void): () => void {
  if (!isNativeApp()) return () => {}
  return onNative<{ command: string; slot?: string }>('command', ({ command, slot }) => {
    // Radio has no queue to move in.
    if (slot === 'r') return
    if (command === 'next' || command === 'prev') handler(command)
  })
}

/** Sets the volume-levelling gain of the music element's native player.
 * False when the element is not one (the caller then lowers its volume). */
export function setNativeGain(element: HTMLMediaElement, gainDb: number): boolean {
  const audio = backedElements.get(element)
  if (!audio || (audio.slot !== 'a' && audio.slot !== 'b')) return false
  void nativeCall('playerGain', { slot: audio.slot, gainDb }).catch(() => {})
  return true
}

/** The equaliser's band gains for the native music players. */
export function setNativeEqualiser(gains: number[], limiter: boolean): void {
  if (!isNativeApp()) return
  void nativeCall('playerEqualiser', { gains, limiter }).catch(() => {})
}

// The songs after the playing and the next one that the app holds on the
// device (see StreamCache.kt), so a short loss of signal does not stop the
// music. The playing and next ones are in the two music players already.
let heldAhead = new Set<number>()
let heldListening = false
let lastAhead = ''

function noteHeldAhead(ids: unknown) {
  heldAhead = new Set(Array.isArray(ids) ? ids.map(Number).filter((id) => id > 0) : [])
}

/** Asks the app to hold these tracks (after the next one, in play order) on
 * the device. Does nothing outside the app. */
export function holdNativeAhead(items: { mediaId: number; url: string }[]): void {
  if (!isNativeApp()) return
  if (!heldListening) {
    heldListening = true
    onNative<{ mediaIds?: unknown }>('streamHeld', ({ mediaIds }) => noteHeldAhead(mediaIds))
  }
  const list = items.map((item) => ({ mediaId: item.mediaId, url: absolute(item.url) }))
  const key = JSON.stringify(list)
  if (key === lastAhead) return
  lastAhead = key
  void nativeCall<{ mediaIds?: unknown }>('streamPrefetch', { items: list })
    .then(({ mediaIds }) => noteHeldAhead(mediaIds))
    .catch(() => {
      lastAhead = ''
    })
}

/** The library tracks the app can play with no network: the ones in its two
 * music players and the ones held ahead of the queue. Downloads not counted. */
export function nativeHeldTrackIds(): Set<number> {
  const ids = new Set(heldAhead)
  for (const audio of backed.values()) {
    const id = audio.heldTrackId()
    if (id > 0) ids.add(id)
  }
  return ids
}
