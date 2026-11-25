package io.github.dorksoul.atriolyric

import kotlin.math.PI
import kotlin.math.abs
import kotlin.math.cos
import kotlin.math.exp
import kotlin.math.log10
import kotlin.math.pow
import kotlin.math.sin
import kotlin.math.sqrt

/**
 * The sound settings of the web player (web/src/audioEffects.ts), applied to
 * decoded audio: the same ten bands (a low shelf at 31 Hz, peaking filters
 * with Q 1.41, a high shelf at 16 kHz, each -12..+12 dB), then the volume
 * levelling gain, then a gentle peak limiter while either is in use. The
 * filters are the standard "Audio EQ Cookbook" biquads that Web Audio's
 * BiquadFilterNode uses, so a preset sounds the same here as in a browser.
 * The limiter stands in for Web Audio's compressor (threshold -3 dB, ratio
 * 12, 3 ms attack, 250 ms release) without its make-up gain.
 *
 * Plain Kotlin with no Android types, so it is unit-tested on the JVM.
 * `configure` and the setters may be called from another thread than `process`.
 */
class Equaliser {
    private class Coefficients(val b0: Double, val b1: Double, val b2: Double, val a1: Double, val a2: Double)

    private class Settings(val gains: DoubleArray, val gainDb: Double, val limiter: Boolean)

    @Volatile
    private var settings = Settings(DoubleArray(BANDS.size), 0.0, false)

    private var sampleRate = 0
    private var channels = 0
    private var applied: Settings? = null
    private var filters: List<Coefficients> = emptyList()
    private var linearGain = 1.0

    /** Filter memory: [band][channel][x1, x2, y1, y2]. */
    // Sized by configure(). (Written as emptyArray(): R8 9.5 crashes on the
    // equivalent nested `Array(0) { ... }` initialiser.)
    private var memory: Array<Array<DoubleArray>> = emptyArray()
    private var limiterGain = 1.0
    private var attack = 0.0
    private var release = 0.0

    fun setBands(gainsDb: DoubleArray, limiter: Boolean) {
        val current = settings
        val gains = DoubleArray(BANDS.size) { index -> (gainsDb.getOrNull(index) ?: 0.0).coerceIn(-MAX_DB, MAX_DB) }
        settings = Settings(gains, current.gainDb, limiter)
    }

    fun setGainDb(gainDb: Double) {
        val current = settings
        settings = Settings(current.gains, gainDb.coerceIn(-40.0, 12.0), current.limiter)
    }

    /** The band gains, the levelling gain and whether the limiter runs, as set. */
    fun describe(): Triple<DoubleArray, Double, Boolean> {
        val current = settings
        return Triple(current.gains.copyOf(), current.gainDb, current.limiter)
    }

    fun configure(sampleRate: Int, channels: Int) {
        this.sampleRate = sampleRate
        this.channels = channels
        memory = Array(BANDS.size) { Array(channels) { DoubleArray(4) } }
        attack = exp(-1.0 / (0.003 * sampleRate))
        release = exp(-1.0 / (0.25 * sampleRate))
        limiterGain = 1.0
        applied = null
    }

    /** True when the settings change nothing, so the caller can pass audio through. */
    fun isNeutral(): Boolean {
        val current = settings
        return !current.limiter && abs(current.gainDb) < 0.01 && current.gains.all { abs(it) < 0.05 }
    }

    private fun refresh(current: Settings) {
        filters = BANDS.mapIndexed { index, frequency ->
            coefficients(
                when (index) {
                    0 -> Kind.LOW_SHELF
                    BANDS.size - 1 -> Kind.HIGH_SHELF
                    else -> Kind.PEAKING
                },
                frequency, current.gains[index], sampleRate.toDouble(),
            )
        }
        linearGain = 10.0.pow(current.gainDb / 20.0)
        applied = current
    }

    /** Processes interleaved samples in -1..1 in place. */
    fun process(samples: FloatArray, count: Int) {
        val current = settings
        if (applied !== current) refresh(current)
        val frames = count / channels
        for (frame in 0 until frames) {
            var peak = 0.0
            for (channel in 0 until channels) {
                val at = frame * channels + channel
                var value = samples[at].toDouble()
                for (band in filters.indices) {
                    val c = filters[band]
                    val m = memory[band][channel]
                    val out = c.b0 * value + c.b1 * m[0] + c.b2 * m[1] - c.a1 * m[2] - c.a2 * m[3]
                    m[1] = m[0]
                    m[0] = value
                    m[3] = m[2]
                    m[2] = out
                    value = out
                }
                value *= linearGain
                samples[at] = value.toFloat()
                val size = abs(value)
                if (size > peak) peak = size
            }
            if (!current.limiter) continue
            // The gain this frame's peak calls for, approached fast on the way down and slowly back up.
            var target = 1.0
            if (peak > THRESHOLD) {
                val overDb = 20 * log10(peak / THRESHOLD)
                target = 10.0.pow(-overDb * (1 - 1 / RATIO) / 20.0)
            }
            val smoothing = if (target < limiterGain) attack else release
            limiterGain = target + (limiterGain - target) * smoothing
            if (limiterGain < 0.9999) {
                for (channel in 0 until channels) {
                    val at = frame * channels + channel
                    samples[at] = (samples[at] * limiterGain).toFloat()
                }
            }
        }
    }

    private enum class Kind { LOW_SHELF, PEAKING, HIGH_SHELF }

    companion object {
        val BANDS = doubleArrayOf(31.0, 62.0, 125.0, 250.0, 500.0, 1000.0, 2000.0, 4000.0, 8000.0, 16000.0)
        const val MAX_DB = 12.0
        private const val PEAKING_Q = 1.41
        private val THRESHOLD = 10.0.pow(-3.0 / 20.0)
        private const val RATIO = 12.0
        private val FLAT = Coefficients(1.0, 0.0, 0.0, 0.0, 0.0)

        private fun coefficients(kind: Kind, frequency: Double, gainDb: Double, sampleRate: Double): Coefficients {
            // A band the sample rate cannot carry (16 kHz in an 8 kHz file) is left out.
            if (abs(gainDb) < 0.01 || frequency >= sampleRate * 0.49) return FLAT
            val a = 10.0.pow(gainDb / 40.0)
            val w0 = 2 * PI * frequency / sampleRate
            val cosine = cos(w0)
            val sine = sin(w0)
            val b0: Double
            val b1: Double
            val b2: Double
            val a0: Double
            val a1: Double
            val a2: Double
            when (kind) {
                Kind.PEAKING -> {
                    val alpha = sine / (2 * PEAKING_Q)
                    b0 = 1 + alpha * a
                    b1 = -2 * cosine
                    b2 = 1 - alpha * a
                    a0 = 1 + alpha / a
                    a1 = -2 * cosine
                    a2 = 1 - alpha / a
                }
                Kind.LOW_SHELF -> {
                    val beta = 2 * sqrt(a) * (sine / 2 * sqrt(2.0))
                    b0 = a * ((a + 1) - (a - 1) * cosine + beta)
                    b1 = 2 * a * ((a - 1) - (a + 1) * cosine)
                    b2 = a * ((a + 1) - (a - 1) * cosine - beta)
                    a0 = (a + 1) + (a - 1) * cosine + beta
                    a1 = -2 * ((a - 1) + (a + 1) * cosine)
                    a2 = (a + 1) + (a - 1) * cosine - beta
                }
                Kind.HIGH_SHELF -> {
                    val beta = 2 * sqrt(a) * (sine / 2 * sqrt(2.0))
                    b0 = a * ((a + 1) + (a - 1) * cosine + beta)
                    b1 = -2 * a * ((a - 1) + (a + 1) * cosine)
                    b2 = a * ((a + 1) + (a - 1) * cosine - beta)
                    a0 = (a + 1) - (a - 1) * cosine + beta
                    a1 = 2 * ((a - 1) - (a + 1) * cosine)
                    a2 = (a + 1) - (a - 1) * cosine - beta
                }
            }
            return Coefficients(b0 / a0, b1 / a0, b2 / a0, a1 / a0, a2 / a0)
        }
    }
}
