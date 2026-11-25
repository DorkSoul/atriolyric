// Web Audio effects (volume levelling + graphic equaliser) for music tracks.
//
// Design rules, because a MediaElementAudioSourceNode can never be undone and
// a suspended AudioContext silences every element routed through it:
//  - An element is only routed through Web Audio once effects are actually
//    needed AND the context is running. Until then it plays on the plain
//    <audio> path exactly as before.
//  - Once routed, an element stays routed; when effects go inactive its chain
//    is set to a bypass-equivalent (0 dB filters, unity gain, transparent
//    compressor).
//  - element.volume stays the user's volume (it applies before the source
//    node); the levelling gain comes from the chain's GainNode when routed and
//    from element.volume (cut only) otherwise.
//  - Any failure falls back to the plain path; playback is never blocked.

export interface AudioSettings {
  volume_leveling: boolean
  eq_preset: string
  eq_gains: number[]
}

export const EQ_BANDS = [31, 62, 125, 250, 500, 1000, 2000, 4000, 8000, 16000] as const
export const EQ_MAX_DB = 12
export const EQ_FLAT = [0, 0, 0, 0, 0, 0, 0, 0, 0, 0]

export const EQ_PRESETS: { id: string; label: string; gains: number[] }[] = [
  { id: 'flat', label: 'Flat', gains: EQ_FLAT },
  { id: 'bass_boost', label: 'Bass Boost', gains: [6, 5, 4, 2, 0, 0, 0, 0, 0, 0] },
  { id: 'treble_boost', label: 'Treble Boost', gains: [0, 0, 0, 0, 0, 1, 2, 4, 5, 6] },
  { id: 'vocal', label: 'Vocal', gains: [-2, -2, -1, 1, 3, 4, 4, 2, 0, -1] },
  { id: 'rock', label: 'Rock', gains: [5, 4, 2, -1, -2, -1, 2, 4, 5, 5] },
  { id: 'pop', label: 'Pop', gains: [-1, 1, 3, 4, 3, 0, -1, -1, 1, 2] },
  { id: 'jazz', label: 'Jazz', gains: [3, 2, 1, 2, -2, -2, 0, 1, 2, 3] },
  { id: 'classical', label: 'Classical', gains: [4, 3, 2, 1, -1, -1, 0, 2, 3, 4] },
  { id: 'electronic', label: 'Electronic', gains: [5, 4, 1, 0, -2, 2, 1, 1, 4, 5] },
  { id: 'loudness', label: 'Loudness', gains: [6, 4, 0, 0, -1, 0, -1, 0, 4, 2] },
]

export const DEFAULT_AUDIO_SETTINGS: AudioSettings = {
  volume_leveling: false,
  eq_preset: 'flat',
  eq_gains: EQ_FLAT,
}

export function clampGain(db: number): number {
  if (!Number.isFinite(db)) return 0
  return Math.max(-EQ_MAX_DB, Math.min(EQ_MAX_DB, db))
}

/** Coerce an untrusted server/WS payload into valid settings. */
export function normalizeAudioSettings(raw: Partial<AudioSettings> | null | undefined): AudioSettings {
  const gains = Array.isArray(raw?.eq_gains) && raw.eq_gains.length === EQ_BANDS.length
    ? raw.eq_gains.map((g) => clampGain(Number(g)))
    : EQ_FLAT
  return {
    volume_leveling: raw?.volume_leveling === true,
    eq_preset: typeof raw?.eq_preset === 'string' && raw.eq_preset ? raw.eq_preset : 'flat',
    eq_gains: gains,
  }
}

export function eqIsFlat(gains: number[]): boolean {
  return gains.every((g) => Math.abs(g) < 0.05)
}

// ReplayGain values are against -18 LUFS; -14 LUFS is 4 dB louder.
const TARGET_OFFSET_DB = 4
const MIN_GAIN_DB = -20
const MAX_GAIN_DB = 12
const PEAK_CEILING = 0.98

/** The levelling gain in dB for a track (0 when it has no loudness data). */
export function levelingGainDb(
  item:
    | {
        replaygain_track_gain?: number | null
        replaygain_album_gain?: number | null
        replaygain_track_peak?: number | null
      }
    | undefined,
): number {
  if (!item) return 0
  const raw = [item.replaygain_track_gain, item.replaygain_album_gain].find(
    (g) => typeof g === 'number' && Number.isFinite(g),
  )
  if (raw == null) return 0
  let db = Math.max(MIN_GAIN_DB, Math.min(MAX_GAIN_DB, raw + TARGET_OFFSET_DB))
  const peak = item.replaygain_track_peak
  if (typeof peak === 'number' && Number.isFinite(peak) && peak > 0) {
    db = Math.min(db, 20 * Math.log10(PEAK_CEILING / peak))
  }
  return db
}

interface Chain {
  filters: BiquadFilterNode[]
  gain: GainNode
  limiter: DynamicsCompressorNode
}

class AudioEffects {
  private settings: AudioSettings = DEFAULT_AUDIO_SETTINGS
  private context: AudioContext | null = null
  private contextFailed = false
  private chains = new WeakMap<HTMLMediaElement, Chain>()
  private chainList: Chain[] = []
  private lastGainDb = new WeakMap<HTMLMediaElement, number>()
  private onReady: (() => void) | null = null

  /** In the Android app the sound comes from native players, which have the
   * same equaliser and levelling built in (see native/nativeAudio.ts). */
  private native: {
    equaliser: (gains: number[], limiter: boolean) => void
    gain: (element: HTMLMediaElement, gainDb: number) => boolean
  } | null = null

  setNativeSink(sink: AudioEffects['native']) {
    this.native = sink
    sink?.equaliser(this.settings.eq_gains, this.effectsActive())
  }

  /** Called when the context becomes running so elements can be (re)applied. */
  setReadyCallback(fn: (() => void) | null) {
    this.onReady = fn
  }

  configure(settings: AudioSettings) {
    this.settings = settings
    this.native?.equaliser(settings.eq_gains, this.effectsActive())
    this.updateFilters()
  }

  private eqActive(): boolean {
    return !eqIsFlat(this.settings.eq_gains)
  }

  private effectsActive(): boolean {
    return this.settings.volume_leveling || this.eqActive()
  }

  private getContext(): AudioContext | null {
    if (this.context) return this.context
    if (this.contextFailed || typeof window === 'undefined') return null
    try {
      const Ctor: typeof AudioContext | undefined =
        window.AudioContext ?? (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext
      if (!Ctor) {
        this.contextFailed = true
        return null
      }
      const context = new Ctor({ latencyHint: 'playback' })
      context.addEventListener('statechange', () => {
        this.publishDebug()
        if (context.state === 'running') this.onReady?.()
        else if (context.state === 'suspended' || context.state === ('interrupted' as AudioContextState)) {
          // Mobile browsers suspend the context on lock/interruption; routed
          // elements are silent until it runs again.
          if (this.chainList.length > 0 && document.visibilityState === 'visible') this.resume()
        }
      })
      this.context = context
      this.publishDebug()
      return context
    } catch {
      this.contextFailed = true
      return null
    }
  }

  /** Try to (re)start the context. Safe to call from any play/visibility event. */
  resume() {
    const context = this.context ?? (this.effectsActive() ? this.getContext() : null)
    if (!context || context.state === 'running') return
    try {
      context.resume().catch(() => {})
    } catch {
      // ignore
    }
  }

  private publishDebug() {
    try {
      document.documentElement.dataset.audioRouted = String(this.chainList.length)
      if (this.context) document.documentElement.dataset.audioContext = this.context.state
    } catch {
      // ignore
    }
  }

  private route(element: HTMLMediaElement): Chain | null {
    const existing = this.chains.get(element)
    if (existing) return existing
    const context = this.getContext()
    if (!context) return null
    if (context.state !== 'running') {
      this.resume()
      return null
    }
    try {
      const source = context.createMediaElementSource(element)
      const filters = EQ_BANDS.map((frequency, index) => {
        const filter = context.createBiquadFilter()
        filter.type = index === 0 ? 'lowshelf' : index === EQ_BANDS.length - 1 ? 'highshelf' : 'peaking'
        filter.frequency.value = frequency
        if (filter.type === 'peaking') filter.Q.value = 1.41
        filter.gain.value = this.settings.eq_gains[index] ?? 0
        return filter
      })
      const gain = context.createGain()
      const limiter = context.createDynamicsCompressor()
      let node: AudioNode = source
      for (const filter of filters) {
        node.connect(filter)
        node = filter
      }
      node.connect(gain)
      gain.connect(limiter)
      limiter.connect(context.destination)
      const chain = { filters, gain, limiter }
      this.chains.set(element, chain)
      this.chainList.push(chain)
      this.setLimiter(chain, this.effectsActive())
      this.publishDebug()
      return chain
    } catch {
      // e.g. the element was already captured, or the browser refused.
      return null
    }
  }

  private setLimiter(chain: Chain, active: boolean) {
    const l = chain.limiter
    try {
      // Inactive = transparent (ratio 1, threshold 0 dB). Active = gentle peak limiter.
      l.threshold.value = active ? -3 : 0
      l.knee.value = active ? 6 : 0
      l.ratio.value = active ? 12 : 1
      l.attack.value = 0.003
      l.release.value = 0.25
    } catch {
      // ignore
    }
  }

  private updateFilters() {
    const context = this.context
    const active = this.effectsActive()
    for (const chain of this.chainList) {
      chain.filters.forEach((filter, index) => {
        const target = this.settings.eq_gains[index] ?? 0
        if (context) filter.gain.setTargetAtTime(target, context.currentTime, 0.015)
        else filter.gain.value = target
      })
      this.setLimiter(chain, active)
    }
  }

  /**
   * Apply levelling to `element` playing `item` and return the factor the
   * caller must multiply into `element.volume` (1 when the chain handles the
   * level). `canRoute` is false for elements that must stay on the plain path.
   */
  applyElement(
    element: HTMLMediaElement,
    item: Parameters<typeof levelingGainDb>[0],
    canRoute: boolean,
  ): number {
    const gainDb = this.settings.volume_leveling ? levelingGainDb(item) : 0
    if (this.native?.gain(element, gainDb)) return 1
    let chain = canRoute ? (this.chains.get(element) ?? null) : null
    const needsRoute = canRoute && !chain && (this.eqActive() || gainDb > 0)
    if (needsRoute) chain = this.route(element)
    if (chain) {
      const context = this.context
      const linear = 10 ** (gainDb / 20)
      const previous = this.lastGainDb.get(element)
      this.lastGainDb.set(element, gainDb)
      if (context && previous !== undefined && previous !== gainDb) {
        chain.gain.gain.setTargetAtTime(linear, context.currentTime, 0.02)
      } else {
        chain.gain.gain.value = linear
      }
      return 1
    }
    return Math.min(1, 10 ** (gainDb / 20))
  }
}

export const audioEffects = new AudioEffects()
