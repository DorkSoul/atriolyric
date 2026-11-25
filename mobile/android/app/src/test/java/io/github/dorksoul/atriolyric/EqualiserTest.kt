package io.github.dorksoul.atriolyric

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import kotlin.math.PI
import kotlin.math.log10
import kotlin.math.sin
import kotlin.math.sqrt

class EqualiserTest {
    private val rate = 44100

    /** The level change in dB a sine at `frequency` gets from the equaliser, once settled. */
    private fun responseDb(equaliser: Equaliser, frequency: Double, amplitude: Double = 0.1): Double {
        equaliser.configure(rate, 2)
        val frames = rate
        val samples = FloatArray(frames * 2)
        for (frame in 0 until frames) {
            val value = (amplitude * sin(2 * PI * frequency * frame / rate)).toFloat()
            samples[frame * 2] = value
            samples[frame * 2 + 1] = value
        }
        equaliser.process(samples, samples.size)
        var sum = 0.0
        val from = frames / 2
        for (frame in from until frames) sum += samples[frame * 2].toDouble() * samples[frame * 2]
        val rms = sqrt(sum / (frames - from))
        return 20 * log10(rms / (amplitude / sqrt(2.0)))
    }

    private fun bands(vararg pairs: Pair<Int, Double>): DoubleArray =
        DoubleArray(Equaliser.BANDS.size).also { gains -> pairs.forEach { (band, db) -> gains[band] = db } }

    @Test
    fun flatChangesNothing() {
        val equaliser = Equaliser()
        assertTrue(equaliser.isNeutral())
        assertEquals(0.0, responseDb(equaliser, 1000.0), 0.01)
    }

    @Test
    fun aPeakingBandLiftsItsOwnFrequencyAndLeavesFarOnesAlone() {
        val equaliser = Equaliser().apply { setBands(bands(5 to 12.0), false) }
        assertEquals(12.0, responseDb(equaliser, 1000.0), 0.2)
        assertEquals(0.0, responseDb(equaliser, 60.0), 0.3)
        assertEquals(0.0, responseDb(equaliser, 14000.0), 0.3)
    }

    @Test
    fun aPeakingBandCuts() {
        val equaliser = Equaliser().apply { setBands(bands(4 to -9.0), false) }
        assertEquals(-9.0, responseDb(equaliser, 500.0), 0.2)
    }

    @Test
    fun theShelvesLiftTheirEndOfTheRange() {
        val low = Equaliser().apply { setBands(bands(0 to 6.0), false) }
        // A shelf is half-way (in dB) at its own frequency and flat far above it.
        assertEquals(3.0, responseDb(low, 31.0), 0.3)
        assertEquals(0.0, responseDb(low, 2000.0), 0.2)
        val high = Equaliser().apply { setBands(bands(9 to 6.0), false) }
        assertEquals(3.0, responseDb(high, 16000.0), 0.4)
        assertEquals(0.0, responseDb(high, 200.0), 0.2)
    }

    @Test
    fun theLevellingGainIsAppliedInDecibels() {
        val quieter = Equaliser().apply { setGainDb(-6.0) }
        assertEquals(-6.0, responseDb(quieter, 1000.0), 0.01)
        val louder = Equaliser().apply { setGainDb(6.0) }
        assertEquals(6.0, responseDb(louder, 1000.0), 0.01)
    }

    @Test
    fun theLimiterHoldsABoostedLoudSignalNearItsThreshold() {
        val equaliser = Equaliser().apply {
            setBands(bands(5 to 12.0), true)
        }
        // 0.5 peak lifted 12 dB would reach 2.0, well over full scale.
        equaliser.configure(rate, 1)
        val samples = FloatArray(rate) { frame -> (0.5 * sin(2 * PI * 1000.0 * frame / rate)).toFloat() }
        equaliser.process(samples, samples.size)
        val settledPeak = samples.drop(rate / 2).maxOf { kotlin.math.abs(it) }
        assertTrue("peak $settledPeak", settledPeak < 1.0)
        assertTrue("peak $settledPeak", settledPeak > 0.6)
    }

    @Test
    fun aBandAboveWhatTheSampleRateCarriesIsLeftOut() {
        val equaliser = Equaliser().apply { setBands(bands(9 to 12.0), false) }
        equaliser.configure(8000, 1)
        val samples = FloatArray(8000) { frame -> (0.1 * sin(2 * PI * 1000.0 * frame / 8000)).toFloat() }
        val before = samples.copyOf()
        equaliser.process(samples, samples.size)
        for (index in samples.indices) assertEquals(before[index], samples[index], 1e-6f)
    }
}
