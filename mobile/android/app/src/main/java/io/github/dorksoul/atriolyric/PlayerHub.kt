package io.github.dorksoul.atriolyric

import android.app.PendingIntent
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import androidx.annotation.OptIn
import androidx.media3.common.AudioAttributes
import androidx.media3.common.C
import androidx.media3.common.ForwardingPlayer
import androidx.media3.common.MediaItem
import androidx.media3.common.MediaMetadata
import androidx.media3.common.PlaybackException
import androidx.media3.common.Player
import androidx.media3.common.util.UnstableApi
import androidx.media3.datasource.DataSourceBitmapLoader
import androidx.media3.exoplayer.ExoPlayer
import androidx.media3.exoplayer.source.DefaultMediaSourceFactory
import androidx.media3.session.CacheBitmapLoader
import androidx.media3.session.MediaController
import androidx.media3.session.MediaSession
import androidx.media3.session.MediaSessionService
import androidx.media3.session.SessionToken
import com.google.common.util.concurrent.ListenableFuture
import com.google.common.util.concurrent.MoreExecutors
import org.json.JSONObject
import java.util.concurrent.Executors

/** What a slot is asked to hold. */
data class SlotSource(
    val loadId: Int,
    val mediaId: Long,
    val uri: Uri,
    val cacheKey: String?,
    val positionMs: Long,
    val title: String?,
    val artist: String?,
    val album: String?,
    val artwork: Uri?,
)

/**
 * The music players. The web player works with two `<audio>` elements, the
 * one playing and a standby holding the next track; here each is a "slot"
 * with its own ExoPlayer, so the web side's logic applies unchanged. The slot
 * last told to play is the one the media session (notification, lock screen,
 * headset buttons) controls. Everything here runs on the main thread.
 *
 * One thing is decided here rather than by the page: when the playing music
 * slot reaches its end and the other one holds the queue's next track (the
 * page "armed" it, see [Slot.armed]), the other one starts at once. With the
 * screen off the page may be asleep at that moment (Chromium freezes a
 * hidden page), and the queue would stop there until it woke. The page hears
 * both changes whenever it runs again and carries on from there, as it does
 * after its own gapless hand-over.
 */
@OptIn(UnstableApi::class)
object PlayerHub {
    val main = Handler(Looper.getMainLooper())
    private val slots = HashMap<String, Slot>()
    private var controller: ListenableFuture<MediaController>? = null

    var activeSlot = "a"
        private set
    var session: MediaSession? = null

    /** A slot's state changed (or its position moved on). */
    var onSlot: ((JSONObject) -> Unit)? = null

    /** "next" or "prev" from the notification, lock screen or a headset, and
     * the slot that was playing. */
    var onCommand: ((String, String) -> Unit)? = null

    /** A music slot (a or b) was told to play or to stop, or reached its end. */
    var onMusicIntent: (() -> Unit)? = null

    /** Whether the music is meant to be playing: a music slot is told to
     * play, including one that just reached its end and waits for the page to
     * start the next track. Radio and a video's sound are not counted. */
    fun musicWantsToPlay(): Boolean =
        listOf("a", "b").any { name -> slots[name]?.player?.let { it.playWhenReady && it.mediaItemCount > 0 } == true }

    /** Until when (elapsedRealtime) the page must run although no music
     * plays: it was handed Next or Previous from a headset, the notification
     * or the lock screen, and it is the page that carries those out. */
    var pageNeededUntil = 0L
        private set

    /** Whether the page has work to do in the background: the music plays,
     * or a command from outside the app is on its way to it. */
    fun pageNeeded(): Boolean = musicWantsToPlay() || SystemClock.elapsedRealtime() < pageNeededUntil

    private var eqGains = DoubleArray(Equaliser.BANDS.size)
    private var eqLimiter = false

    /** The account's equaliser, for every music slot (now and later). */
    fun setEqualiser(gains: DoubleArray, limiter: Boolean) {
        eqGains = gains
        eqLimiter = limiter
        for (slot in slots.values) slot.equaliser?.setBands(gains, limiter)
    }

    fun slot(context: Context, name: String): Slot =
        slots.getOrPut(name) { Slot(name, context.applicationContext) }

    fun existing(): Collection<Slot> = slots.values

    fun sessionPlayer(context: Context): Player = slot(context, activeSlot).sessionPlayer

    private fun activate(context: Context, name: String) {
        if (activeSlot == name) return
        activeSlot = name
        session?.player = slot(context, name).sessionPlayer
    }

    /** The service owns the media session; a bound controller keeps it alive
     * and lets Media3 move it to the foreground when playback starts. */
    private fun ensureService(context: Context) {
        if (controller != null) return
        val app = context.applicationContext
        val token = SessionToken(app, ComponentName(app, PlaybackService::class.java))
        controller = MediaController.Builder(app, token).buildAsync()
    }

    fun load(context: Context, name: String, source: SlotSource, next: Boolean) {
        slot(context, name).load(source, next)
    }

    fun play(context: Context, name: String) {
        val slot = slot(context, name)
        activate(context, name)
        ensureService(context)
        slot.clearError()
        val player = slot.player
        if (player.playbackState == Player.STATE_ENDED) player.seekTo(0)
        if (player.playbackState == Player.STATE_IDLE && player.mediaItemCount > 0) player.prepare()
        player.play()
    }

    fun pauseAll() {
        for (slot in slots.values) slot.player.pause()
    }

    /** The playing music slot `ended` reached its end: start the other one
     * if it holds the queue's next track, ready to go. */
    private fun advanceFrom(ended: Slot) {
        val other = slots[if (ended.name == "a") "b" else "a"] ?: return
        val player = other.player
        val ready = player.playbackState == Player.STATE_READY || player.playbackState == Player.STATE_BUFFERING
        if (!other.armed || player.mediaItemCount == 0 || !ready || player.playWhenReady) return
        other.armed = false
        play(other.context, other.name)
    }

    fun stopService() {
        controller?.let { MediaController.releaseFuture(it) }
        controller = null
    }

    class Slot(val name: String, val context: Context) : Player.Listener {
        /** The music slots carry the equaliser and the levelling gain; radio
         * and a video's sound play as they are, as in the web player. */
        val equaliser: Equaliser? = if (name == "a" || name == "b") Equaliser() else null
        val player: ExoPlayer = (
            if (equaliser != null) ExoPlayer.Builder(context, EqualisedRenderersFactory(context, EqualiserProcessor(equaliser)))
            else ExoPlayer.Builder(context)
            )
            .setMediaSourceFactory(DefaultMediaSourceFactory(context).setDataSourceFactory(Net.playback(context)))
            .setAudioAttributes(
                AudioAttributes.Builder().setUsage(C.USAGE_MEDIA).setContentType(C.AUDIO_CONTENT_TYPE_MUSIC).build(),
                true,
            )
            .setHandleAudioBecomingNoisy(true)
            .setWakeMode(C.WAKE_MODE_NETWORK)
            .build()
        val sessionPlayer: Player = SessionPlayer(player)
        var loadId = 0
            private set
        var mediaId = 0L

        /** Whether the source is the copy on the device, not a stream. */
        var downloaded = false
            private set
        private var error: String? = null

        /** How many errors this slot has had. The page compares counts, not
         * messages: the same failure twice in a row must be heard twice. */
        private var errorCount = 0

        /** This slot holds the queue's next track (the page's standby), so it
         * may start by itself when the playing slot ends (see [advanceFrom]). */
        var armed = false

        /** A radio or video-sound slot paused for this long lets go of its
         * decoder and buffers; told to play again, it prepares again where
         * it was. The music slots keep theirs: the standby must stay ready. */
        private val releaseIdle = Runnable {
            if (!player.playWhenReady && player.playbackState != Player.STATE_IDLE) player.stop()
        }

        private val tick = object : Runnable {
            override fun run() {
                emit()
                if (player.isPlaying) main.postDelayed(this, 500)
            }
        }

        init {
            player.addListener(this)
            equaliser?.setBands(eqGains, eqLimiter)
        }

        fun load(source: SlotSource, next: Boolean) {
            loadId = source.loadId
            mediaId = source.mediaId
            downloaded = source.cacheKey != null
            error = null
            armed = next
            val metadata = MediaMetadata.Builder()
                .setTitle(source.title)
                .setArtist(source.artist)
                .setAlbumTitle(source.album)
                .setArtworkUri(source.artwork)
                .build()
            val item = MediaItem.Builder()
                .setMediaId(source.mediaId.toString())
                .setUri(source.uri)
                .setCustomCacheKey(source.cacheKey)
                .setMediaMetadata(metadata)
                .build()
            // Like an <audio> element given a new source: it waits to be told to play.
            player.playWhenReady = false
            player.setMediaItem(item, source.positionMs)
            player.prepare()
        }

        fun unload() {
            player.playWhenReady = false
            player.stop()
            player.clearMediaItems()
            mediaId = 0
            downloaded = false
            error = null
            armed = false
            emit()
        }

        fun clearError() {
            error = null
        }

        /** Arms or disarms the slot for the load the page means (an older
         * load's word does not count). */
        fun arm(forLoadId: Int, value: Boolean) {
            if (forLoadId != loadId) return
            armed = value
            emit()
        }

        override fun onPlayerError(error: PlaybackException) {
            this.error = "${error.errorCodeName}: ${error.message ?: ""}"
            errorCount += 1
        }

        override fun onEvents(player: Player, events: Player.Events) {
            emit()
            if (equaliser != null && events.contains(Player.EVENT_PLAYBACK_STATE_CHANGED) &&
                player.playbackState == Player.STATE_ENDED && player.playWhenReady && activeSlot == name
            ) {
                // After the state above, so the page hears this slot end first.
                advanceFrom(this)
            }
            if (equaliser != null && events.containsAny(Player.EVENT_PLAY_WHEN_READY_CHANGED, Player.EVENT_PLAYBACK_STATE_CHANGED)) {
                onMusicIntent?.invoke()
            }
            if (equaliser == null && events.contains(Player.EVENT_PLAY_WHEN_READY_CHANGED)) {
                main.removeCallbacks(releaseIdle)
                if (!player.playWhenReady) main.postDelayed(releaseIdle, IDLE_RELEASE_MS)
            }
            if (player.isPlaying) {
                main.removeCallbacks(tick)
                main.postDelayed(tick, 500)
            }
        }

        fun state(): JSONObject {
            val duration = player.duration
            val effects = equaliser?.describe()
            return JSONObject()
                .put("slot", name)
                .put("loadId", loadId)
                .put("mediaId", mediaId)
                .put(
                    "playbackState",
                    when (player.playbackState) {
                        Player.STATE_BUFFERING -> "buffering"
                        Player.STATE_READY -> "ready"
                        Player.STATE_ENDED -> "ended"
                        else -> "idle"
                    },
                )
                .put("playWhenReady", player.playWhenReady)
                .put("playing", player.isPlaying)
                .put("position", player.currentPosition / 1000.0)
                .put("duration", if (duration == C.TIME_UNSET) JSONObject.NULL else duration / 1000.0)
                .put("buffered", player.bufferedPosition / 1000.0)
                .put("error", error ?: JSONObject.NULL)
                .put("errorCount", errorCount)
                .put("armed", armed)
                .put("active", activeSlot == name)
                .put("downloaded", downloaded)
                .put("eq", effects?.let { org.json.JSONArray(it.first.toList()) } ?: JSONObject.NULL)
                .put("gainDb", effects?.second ?: JSONObject.NULL)
                .put("limiter", effects?.third ?: JSONObject.NULL)
        }

        private fun emit() {
            onSlot?.invoke(state())
        }
    }

    private const val IDLE_RELEASE_MS = 10 * 60 * 1000L

    /** What the session sees: the slot's player, with Next and Previous always
     * offered and handed to the web player, which owns the queue. */
    private class SessionPlayer(player: Player) : ForwardingPlayer(player) {
        override fun getAvailableCommands(): Player.Commands = super.getAvailableCommands().buildUpon()
            .add(Player.COMMAND_SEEK_TO_NEXT)
            .add(Player.COMMAND_SEEK_TO_PREVIOUS)
            .add(Player.COMMAND_SEEK_TO_NEXT_MEDIA_ITEM)
            .add(Player.COMMAND_SEEK_TO_PREVIOUS_MEDIA_ITEM)
            .build()

        override fun isCommandAvailable(command: Int): Boolean = availableCommands.contains(command)
        override fun hasNextMediaItem() = true
        override fun hasPreviousMediaItem() = true
        override fun seekToNext() = command("next")
        override fun seekToNextMediaItem() = command("next")
        override fun seekToPrevious() = command("prev")
        override fun seekToPreviousMediaItem() = command("prev")

        private fun command(name: String) {
            // Wake the page first (a paused app's page may be frozen): the
            // command reaches it as soon as it runs again.
            pageNeededUntil = SystemClock.elapsedRealtime() + 15_000
            onMusicIntent?.invoke()
            onCommand?.invoke(name, activeSlot)
        }
    }
}

/** Holds the media session: the notification, the lock-screen controls and
 * the foreground state that keeps music playing with the screen off. */
@OptIn(UnstableApi::class)
class PlaybackService : MediaSessionService() {
    private var session: MediaSession? = null
    private var artworkExecutor: java.util.concurrent.ExecutorService? = null

    override fun onCreate() {
        super.onCreate()
        val open = packageManager.getLaunchIntentForPackage(packageName) ?: Intent(this, MainActivity::class.java)
        val activity = PendingIntent.getActivity(
            this, 0, open, PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
        val executor = Executors.newSingleThreadExecutor().also { artworkExecutor = it }
        val artwork = CacheBitmapLoader(
            DataSourceBitmapLoader.Builder(this)
                .setExecutorService(MoreExecutors.listeningDecorator(executor))
                .setDataSourceFactory(Net.playback(this))
                .build(),
        )
        session = MediaSession.Builder(this, PlayerHub.sessionPlayer(this))
            .setSessionActivity(activity)
            .setBitmapLoader(artwork)
            .setCallback(ControllerGate())
            .build()
        PlayerHub.session = session
    }

    /**
     * Who may control the music. The service has to be exported for the
     * system to reach it, which by default lets any installed app connect,
     * read what is playing and send commands. Only these are let in: the
     * system and anything it trusts (the notification and lock screen,
     * Bluetooth and so headset buttons: they hold MEDIA_CONTENT_CONTROL), this
     * app itself (PlayerHub's controller), Android Auto and cars, and the Wear
     * OS companion that puts the controls on a watch.
     */
    private inner class ControllerGate : MediaSession.Callback {
        override fun onConnect(session: MediaSession, controller: MediaSession.ControllerInfo): MediaSession.ConnectionResult =
            if (controller.isTrusted ||
                controller.packageName == packageName ||
                session.isMediaNotificationController(controller) ||
                session.isAutomotiveController(controller) ||
                session.isAutoCompanionController(controller) ||
                (controller.isPackageNameVerified && controller.packageName in WATCH_COMPANIONS)
            ) {
                super.onConnect(session, controller)
            } else {
                MediaSession.ConnectionResult.reject()
            }
    }

    private companion object {
        val WATCH_COMPANIONS = setOf("com.google.android.wearable.app", "com.samsung.android.app.watchmanager")
    }

    override fun onGetSession(controllerInfo: MediaSession.ControllerInfo): MediaSession? = session

    /** The app was swiped away: the web player that owns the queue is gone,
     * so the music stops with it. */
    override fun onTaskRemoved(rootIntent: Intent?) {
        PlayerHub.pauseAll()
        PlayerHub.stopService()
        stopSelf()
    }

    override fun onDestroy() {
        PlayerHub.session = null
        session?.release()
        session = null
        // One per service: without this a thread would stay behind each time
        // the service is made again after a swipe-away.
        artworkExecutor?.shutdown()
        artworkExecutor = null
        super.onDestroy()
    }
}
