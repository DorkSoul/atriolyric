package io.github.dorksoul.atriolyric

import android.os.Bundle
import android.view.WindowManager
import androidx.annotation.OptIn
import androidx.appcompat.app.AppCompatActivity
import androidx.core.view.WindowCompat
import androidx.core.view.WindowInsetsCompat
import androidx.core.view.WindowInsetsControllerCompat
import androidx.media3.common.AudioAttributes
import androidx.media3.common.C
import androidx.media3.common.MediaItem
import androidx.media3.common.MediaMetadata
import androidx.media3.common.util.UnstableApi
import androidx.media3.exoplayer.ExoPlayer
import androidx.media3.exoplayer.source.DefaultMediaSourceFactory
import androidx.media3.ui.PlayerView
import org.json.JSONObject

/**
 * Full-screen player for a downloaded video. Native rather than the page's
 * `<video>`: the file sits in the download cache, and an "Original" download
 * can be in a container or codec the WebView cannot play.
 */
@OptIn(UnstableApi::class)
class VideoActivity : AppCompatActivity() {
    private var player: ExoPlayer? = null

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val mediaId = intent.getLongExtra(EXTRA_MEDIA_ID, 0)
        val view = PlayerView(this)
        view.setBackgroundColor(0xFF000000.toInt())
        setContentView(view)
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        WindowCompat.getInsetsController(window, view).apply {
            systemBarsBehavior = WindowInsetsControllerCompat.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE
            hide(WindowInsetsCompat.Type.systemBars())
        }

        // Like the music players: other apps' sound pauses or ducks for the
        // video, a call pauses it, and unplugging headphones does not move
        // the sound to the speaker.
        val exo = ExoPlayer.Builder(this)
            .setMediaSourceFactory(DefaultMediaSourceFactory(this).setDataSourceFactory(Net.playback(this)))
            .setAudioAttributes(
                AudioAttributes.Builder().setUsage(C.USAGE_MEDIA).setContentType(C.AUDIO_CONTENT_TYPE_MOVIE).build(),
                true,
            )
            .setHandleAudioBecomingNoisy(true)
            .build()
        player = exo
        current = this
        currentMediaId = mediaId
        view.player = exo
        // Back to the app when the video is over, as when Back is pressed.
        exo.addListener(object : androidx.media3.common.Player.Listener {
            override fun onPlaybackStateChanged(playbackState: Int) {
                if (playbackState == androidx.media3.common.Player.STATE_ENDED) finish()
            }
        })
        val startAt = savedInstanceState?.getLong(STATE_POSITION) ?: 0L
        // The index is on disk; a video is opened rarely, so read it off the main thread.
        Thread {
            val download = Downloads.completed(this, mediaId)
            runOnUiThread {
                if (download == null || isFinishing || isDestroyed) {
                    finish()
                    return@runOnUiThread
                }
                val item = MediaItem.Builder()
                    .setMediaId(mediaId.toString())
                    .setUri(download.request.uri)
                    .setCustomCacheKey(download.request.customCacheKey)
                    .setMediaMetadata(MediaMetadata.Builder().setTitle(Downloads.meta(download).optString("title")).build())
                    .build()
                exo.setMediaItem(item, startAt)
                exo.prepare()
                exo.play()
            }
        }.start()
    }

    override fun onSaveInstanceState(outState: Bundle) {
        super.onSaveInstanceState(outState)
        player?.let { outState.putLong(STATE_POSITION, it.currentPosition) }
    }

    override fun onStop() {
        super.onStop()
        player?.pause()
    }

    override fun onDestroy() {
        if (current === this) {
            current = null
            currentMediaId = 0
        }
        player?.release()
        player = null
        super.onDestroy()
    }

    companion object {
        const val EXTRA_MEDIA_ID = "mediaId"
        private const val STATE_POSITION = "position"
        private var current: VideoActivity? = null
        private var currentMediaId = 0L

        /** Main thread only. */
        fun state(): JSONObject {
            val exo = current?.player
            val format = exo?.videoFormat
            return JSONObject()
                .put("open", current != null)
                .put("mediaId", currentMediaId)
                .put("playing", exo?.isPlaying ?: false)
                .put("position", (exo?.currentPosition ?: 0L) / 1000.0)
                .put("width", format?.width ?: 0)
                .put("height", format?.height ?: 0)
                .put("error", exo?.playerError?.errorCodeName ?: JSONObject.NULL)
        }
    }
}
