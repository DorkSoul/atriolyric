package io.github.dorksoul.atriolyric

import android.content.Context
import androidx.annotation.OptIn
import androidx.media3.common.C
import androidx.media3.common.audio.AudioProcessor
import androidx.media3.common.audio.BaseAudioProcessor
import androidx.media3.common.util.UnstableApi
import androidx.media3.exoplayer.DefaultRenderersFactory
import androidx.media3.exoplayer.audio.AudioSink
import androidx.media3.exoplayer.audio.DefaultAudioSink
import java.nio.ByteBuffer

/** Runs a player's decoded 16-bit audio through an [Equaliser]. */
@OptIn(UnstableApi::class)
class EqualiserProcessor(val equaliser: Equaliser) : BaseAudioProcessor() {
    private var samples = FloatArray(0)

    override fun onConfigure(inputAudioFormat: AudioProcessor.AudioFormat): AudioProcessor.AudioFormat {
        if (inputAudioFormat.encoding != C.ENCODING_PCM_16BIT) {
            throw AudioProcessor.UnhandledAudioFormatException(inputAudioFormat)
        }
        equaliser.configure(inputAudioFormat.sampleRate, inputAudioFormat.channelCount)
        return inputAudioFormat
    }

    override fun queueInput(inputBuffer: ByteBuffer) {
        val bytes = inputBuffer.remaining()
        // The sink also calls with nothing to give (while draining).
        if (bytes == 0) return
        val output = replaceOutputBuffer(bytes)
        if (equaliser.isNeutral()) {
            output.put(inputBuffer)
            output.flip()
            return
        }
        val count = bytes / 2
        if (samples.size < count) samples = FloatArray(count)
        for (index in 0 until count) samples[index] = inputBuffer.short / 32768f
        equaliser.process(samples, count)
        for (index in 0 until count) {
            val value = (samples[index] * 32768f).toInt().coerceIn(-32768, 32767)
            output.putShort(value.toShort())
        }
        output.flip()
    }
}

/** A renderers factory whose audio passes through `processor` on its way out. */
@OptIn(UnstableApi::class)
class EqualisedRenderersFactory(context: Context, private val processor: EqualiserProcessor) : DefaultRenderersFactory(context) {
    override fun buildAudioSink(context: Context, enableFloatOutput: Boolean, enableAudioTrackPlaybackParams: Boolean): AudioSink =
        DefaultAudioSink.Builder(context)
            .setEnableFloatOutput(false)
            .setAudioProcessors(arrayOf<AudioProcessor>(processor))
            .build()
}
