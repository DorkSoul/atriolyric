package io.github.dorksoul.atriolyric

import android.app.Notification
import android.content.Context
import android.net.Uri
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.webkit.WebResourceResponse
import androidx.annotation.OptIn
import androidx.media3.common.C
import androidx.media3.common.util.UnstableApi
import androidx.media3.database.StandaloneDatabaseProvider
import androidx.media3.datasource.cache.NoOpCacheEvictor
import androidx.media3.datasource.cache.SimpleCache
import androidx.media3.exoplayer.offline.Download
import androidx.media3.exoplayer.offline.DownloadManager
import androidx.media3.exoplayer.offline.DownloadNotificationHelper
import androidx.media3.exoplayer.offline.DownloadRequest
import androidx.media3.exoplayer.offline.DownloadService
import androidx.media3.exoplayer.scheduler.PlatformScheduler
import androidx.media3.exoplayer.scheduler.Requirements
import androidx.media3.exoplayer.scheduler.Scheduler
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.util.concurrent.CountDownLatch
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit

/**
 * Songs and videos kept on the device. Each is one Media3 download (id
 * "m<media id>") in a cache that is never evicted; what the item is (title,
 * artist, quality, ...) travels as JSON in the request's data, so the list can
 * be shown with no server. The artwork is a small file beside the cache.
 *
 * The download manager lives on the main thread (see [manager]): Media3 runs
 * its callbacks on the thread that made it, and the download service, the
 * progress ticks and the players all work on the main thread.
 */
@OptIn(UnstableApi::class)
object Downloads {
    private const val PREFS = "atrio_downloads"
    private const val KEY_WIFI_ONLY = "wifi_only"
    private const val KEY_PENDING = "pending"

    private var cacheInstance: SimpleCache? = null
    private var databaseInstance: StandaloneDatabaseProvider? = null

    @Volatile
    private var managerInstance: DownloadManager? = null
    private val main = Handler(Looper.getMainLooper())
    private val io = Executors.newSingleThreadExecutor()

    /** Where the full list is read and handed on: one thread, so lists reach
     * the page in the order they were read. */
    val listExecutor = Executors.newSingleThreadExecutor()

    /** Told (on the main thread) whenever the list itself changed: an item
     * added or removed, a state change, artwork saved. */
    @Volatile
    var onChanged: (() -> Unit)? = null

    /** Told (on the main thread) once a second while anything is being
     * fetched, with the progress of what is (see [progress]). */
    @Volatile
    var onProgress: ((JSONArray) -> Unit)? = null

    /** Requests Android would not let the app hand to the download service
     * just then (an app in the background may not start a service): sent
     * again when the app is next in front. Main thread only. */
    private val deferredAdds = ArrayList<DownloadRequest>()
    private val deferredRemoves = LinkedHashSet<String>()

    /** Set when Android's daily limit on background downloading ran out and
     * the downloads were paused for it; they carry on once the app is opened. */
    @Volatile
    var pausedForTimeout = false

    fun id(mediaId: Long) = "m$mediaId"

    private fun prefs(context: Context) = context.applicationContext.getSharedPreferences(PREFS, Context.MODE_PRIVATE)

    /** Shared with [StreamCache]: one database holds both caches' indexes. */
    fun database(context: Context): StandaloneDatabaseProvider =
        databaseInstance ?: StandaloneDatabaseProvider(context.applicationContext).also { databaseInstance = it }

    private fun root(context: Context): File =
        context.applicationContext.getExternalFilesDir(null) ?: context.applicationContext.filesDir

    @Synchronized
    fun cache(context: Context): SimpleCache =
        cacheInstance ?: SimpleCache(File(root(context), "downloads"), NoOpCacheEvictor(), database(context))
            .also { cacheInstance = it }

    fun wifiOnly(context: Context): Boolean = prefs(context).getBoolean(KEY_WIFI_ONLY, false)

    private fun requirementsFor(wifiOnly: Boolean) =
        Requirements(if (wifiOnly) Requirements.NETWORK_UNMETERED else Requirements.NETWORK)

    /** The download manager, made on the main thread whichever thread asks
     * first. The app makes it at start-up ([AtrioApp]), so a wait here is
     * only a fallback. */
    fun manager(context: Context): DownloadManager {
        managerInstance?.let { return it }
        val app = context.applicationContext
        if (Looper.myLooper() == Looper.getMainLooper()) return create(app)
        val made = CountDownLatch(1)
        main.post {
            try {
                create(app)
            } finally {
                made.countDown()
            }
        }
        made.await(10, TimeUnit.SECONDS)
        return managerInstance ?: throw IllegalStateException("The download manager could not be started")
    }

    @Synchronized
    private fun create(app: Context): DownloadManager {
        managerInstance?.let { return it }
        val manager = DownloadManager(app, database(app), cache(app), Net.http(), Executors.newFixedThreadPool(3))
        manager.maxParallelDownloads = 2
        manager.requirements = requirementsFor(wifiOnly(app))
        manager.addListener(object : DownloadManager.Listener {
            override fun onDownloadChanged(downloadManager: DownloadManager, download: Download, finalException: Exception?) {
                notifyChanged()
                scheduleProgress(app)
            }

            override fun onDownloadRemoved(downloadManager: DownloadManager, download: Download) {
                val next = takePending(app, download.request.id)
                if (next != null) send(app, next) else artFile(app, download.request.id).delete()
                notifyChanged()
            }
        })
        managerInstance = manager
        // A replacement whose earlier copy was removed while the app was not
        // running (or whose removal never reached the service) is carried on.
        io.execute { resumePending(app) }
        return manager
    }

    private fun notifyChanged() {
        main.post { onChanged?.invoke() }
    }

    private var progressScheduled = false

    private fun isActive(download: Download) =
        download.state == Download.STATE_DOWNLOADING || download.state == Download.STATE_QUEUED || download.state == Download.STATE_RESTARTING

    /** The progress of what is being fetched now, from the manager's own list
     * (no reading of the index): what the page needs every second. */
    private fun progress(manager: DownloadManager): JSONArray {
        val items = JSONArray()
        for (download in manager.currentDownloads) {
            if (!isActive(download)) continue
            val mediaId = download.request.id.removePrefix("m").toLongOrNull() ?: continue
            val percent = download.percentDownloaded
            items.put(
                JSONObject()
                    .put("mediaId", mediaId)
                    .put("state", stateName(download.state))
                    .put("bytes", download.bytesDownloaded)
                    .put("totalBytes", if (download.contentLength == C.LENGTH_UNSET.toLong()) JSONObject.NULL else download.contentLength)
                    .put("percent", if (percent == C.PERCENTAGE_UNSET.toFloat()) JSONObject.NULL else percent.toDouble()),
            )
        }
        return items
    }

    /** Media3 reports state changes, not progress: while anything is being
     * fetched, send its progress once a second. */
    private fun scheduleProgress(context: Context) {
        main.post {
            if (progressScheduled) return@post
            progressScheduled = true
            main.postDelayed(object : Runnable {
                override fun run() {
                    val items = progress(manager(context))
                    if (items.length() > 0) onProgress?.invoke(items)
                    if (items.length() > 0) main.postDelayed(this, 1000) else progressScheduled = false
                }
            }, 1000)
        }
    }

    /** Hands a request to the download service. From the background Android
     * may refuse to start the service at all: the request is then kept and
     * sent when the app is next opened ([onForeground]). */
    private fun send(context: Context, request: DownloadRequest) {
        val app = context.applicationContext
        main.post {
            if (!startService { foreground -> DownloadService.sendAddDownload(app, AtrioDownloadService::class.java, request, foreground) }) {
                deferredAdds.removeAll { it.id == request.id }
                deferredAdds.add(request)
            }
        }
    }

    private fun sendRemove(context: Context, downloadId: String) {
        val app = context.applicationContext
        main.post {
            if (!startService { foreground -> DownloadService.sendRemoveDownload(app, AtrioDownloadService::class.java, downloadId, foreground) }) {
                deferredRemoves.add(downloadId)
            }
        }
    }

    /** Tries a plain start, then a foreground one; false when Android allows
     * neither now (an app in the background on Android 12+). */
    private fun startService(start: (Boolean) -> Unit): Boolean {
        for (foreground in listOf(false, true)) {
            try {
                start(foreground)
                return true
            } catch (_: IllegalStateException) {
                // Not allowed from the background (ForegroundServiceStartNotAllowedException is one too).
            } catch (_: SecurityException) {
                return false
            }
        }
        return false
    }

    /** The app is in front again: what could not be sent before is sent now,
     * and downloads paused at Android's background limit carry on. */
    fun onForeground(context: Context) {
        val app = context.applicationContext
        main.post {
            val removes = deferredRemoves.toList()
            deferredRemoves.clear()
            removes.forEach { sendRemove(app, it) }
            val adds = deferredAdds.toList()
            deferredAdds.clear()
            adds.forEach { send(app, it) }
            if (pausedForTimeout) {
                pausedForTimeout = false
                startService { foreground -> DownloadService.sendResumeDownloads(app, AtrioDownloadService::class.java, foreground) }
            }
        }
        io.execute { resumePending(app) }
    }

    // --- Replacements waiting for their earlier copy to go -------------------
    // Kept on disk: if the app dies between removing the old copy and adding
    // the new one, the new one is still added on the next start.

    @Synchronized
    private fun pendingAll(context: Context): JSONObject = try {
        JSONObject(prefs(context).getString(KEY_PENDING, "{}") ?: "{}")
    } catch (_: Exception) {
        JSONObject()
    }

    @Synchronized
    private fun putPending(context: Context, request: DownloadRequest) {
        val all = pendingAll(context)
        all.put(
            request.id,
            JSONObject()
                .put("uri", request.uri.toString())
                .put("key", request.customCacheKey ?: JSONObject.NULL)
                .put("data", String(request.data, Charsets.UTF_8)),
        )
        prefs(context).edit().putString(KEY_PENDING, all.toString()).apply()
    }

    @Synchronized
    private fun dropPending(context: Context, downloadId: String) {
        val all = pendingAll(context)
        if (all.remove(downloadId) != null) prefs(context).edit().putString(KEY_PENDING, all.toString()).apply()
    }

    @Synchronized
    private fun takePending(context: Context, downloadId: String): DownloadRequest? {
        val entry = pendingAll(context).optJSONObject(downloadId) ?: return null
        dropPending(context, downloadId)
        return requestFrom(downloadId, entry)
    }

    private fun requestFrom(downloadId: String, entry: JSONObject): DownloadRequest? = try {
        DownloadRequest.Builder(downloadId, Uri.parse(entry.getString("uri")))
            .setCustomCacheKey(if (entry.isNull("key")) null else entry.getString("key"))
            .setData(entry.optString("data").toByteArray(Charsets.UTF_8))
            .build()
    } catch (_: Exception) {
        null
    }

    /** Off the main thread (reads the index). */
    private fun resumePending(context: Context) {
        val all = pendingAll(context)
        val index = try {
            manager(context).downloadIndex
        } catch (_: Exception) {
            return
        }
        for (downloadId in all.keys().asSequence().toList()) {
            val stored = try {
                index.getDownload(downloadId)
            } catch (_: Exception) {
                continue
            }
            when {
                // The old copy is gone: add the new one now.
                stored == null -> takePending(context, downloadId)?.let { send(context, it) }
                // Its removal is under way: onDownloadRemoved adds it.
                stored.state == Download.STATE_REMOVING -> Unit
                // The removal never reached the service: ask again.
                else -> sendRemove(context, downloadId)
            }
        }
    }

    // --- Starting and removing -------------------------------------------------

    /** Starts the download of one item. `meta` needs mediaId, kind, title and
     * quality; the rest is optional. Something on the device or on its way is
     * not fetched again unless `replace` is set (a new quality); a failed or
     * stopped one is tried again. Off the main thread (reads the index). */
    fun start(context: Context, meta: JSONObject, url: String, artUrl: String?, replace: Boolean) {
        val app = context.applicationContext
        val mediaId = meta.getLong("mediaId")
        val downloadId = id(mediaId)
        meta.put("addedAt", System.currentTimeMillis())
        val request = DownloadRequest.Builder(downloadId, Uri.parse(url))
            .setCustomCacheKey("dl-$mediaId-${meta.optString("quality")}-${System.currentTimeMillis()}")
            .setData(meta.toString().toByteArray(Charsets.UTF_8))
            .build()
        val existing = manager(app).downloadIndex.getDownload(downloadId)
        val retry = existing != null && (
            existing.state == Download.STATE_FAILED || existing.state == Download.STATE_STOPPED || existing.state == Download.STATE_REMOVING
            )
        if (existing != null && !replace && !retry) return
        if (existing != null) {
            putPending(app, request)
            sendRemove(app, downloadId)
        } else {
            dropPending(app, downloadId)
            send(app, request)
        }
        if (!artUrl.isNullOrEmpty()) {
            io.execute {
                val answer = Net.fetch(artUrl, 20_000)
                if (answer != null && answer.code == 200 && answer.body.isNotEmpty() && Net.isImageType(answer.contentType)) {
                    val file = artFile(app, downloadId)
                    file.parentFile?.mkdirs()
                    val part = File(file.path + ".part")
                    part.writeBytes(answer.body)
                    if (part.renameTo(file)) notifyChanged() else part.delete()
                }
            }
        }
    }

    fun remove(context: Context, mediaId: Long) {
        val app = context.applicationContext
        dropPending(app, id(mediaId))
        main.post { deferredAdds.removeAll { it.id == id(mediaId) } }
        sendRemove(app, id(mediaId))
    }

    /** Downloads only on Wi-Fi (an unmetered network), or on any network. */
    fun setWifiOnly(context: Context, wifiOnly: Boolean) {
        val app = context.applicationContext
        prefs(app).edit().putBoolean(KEY_WIFI_ONLY, wifiOnly).apply()
        main.post { manager(app).requirements = requirementsFor(wifiOnly) }
    }

    // --- Artwork ---------------------------------------------------------------

    fun artFile(context: Context, downloadId: String): File =
        File(File(context.applicationContext.filesDir, "art"), downloadId)

    /** The image type of saved artwork, from its first bytes. */
    fun artMime(head: ByteArray): String = when {
        head.size > 3 && head[0] == 0x89.toByte() && head[1] == 'P'.code.toByte() -> "image/png"
        head.size > 11 && String(head, 8, 4, Charsets.US_ASCII) == "WEBP" -> "image/webp"
        else -> "image/jpeg"
    }

    private val artRoute = Regex("^/__atrio/download-art/(\\d+)$")

    /** `<server>/__atrio/download-art/<media id>` for the page: the saved
     * artwork of a download as an ordinary image address, so a long list
     * loads only what it shows and the WebView caches it like any image
     * (rather than every picture crossing the bridge as text). Null for any
     * other address. */
    fun artResponse(context: Context, serverUrl: String, url: Uri): WebResourceResponse? {
        val server = Uri.parse(serverUrl)
        if (url.host != server.host || url.port != server.port) return null
        val mediaId = artRoute.matchEntire(url.path ?: return null)?.groupValues?.get(1)?.toLongOrNull() ?: return null
        val file = artFile(context, id(mediaId))
        val headers = hashMapOf("Cache-Control" to "no-cache")
        if (!file.isFile) {
            return WebResourceResponse("text/plain", "utf-8", 404, "Not Found", headers, java.io.ByteArrayInputStream(ByteArray(0)))
        }
        return try {
            val head = ByteArray(12)
            val read = file.inputStream().use { it.read(head) }
            WebResourceResponse(artMime(head.copyOf(maxOf(0, read))), null, 200, "OK", headers, file.inputStream())
        } catch (_: Exception) {
            null
        }
    }

    // --- Reading the list --------------------------------------------------------

    /** The finished download of an item, or null. Reads the index: not for the main thread. */
    fun completed(context: Context, mediaId: Long): Download? =
        manager(context).downloadIndex.getDownload(id(mediaId))?.takeIf { it.state == Download.STATE_COMPLETED }

    fun meta(download: Download): JSONObject = try {
        JSONObject(String(download.request.data, Charsets.UTF_8))
    } catch (_: Exception) {
        JSONObject()
    }

    private fun stateName(state: Int) = when (state) {
        Download.STATE_COMPLETED -> "completed"
        Download.STATE_DOWNLOADING -> "downloading"
        Download.STATE_FAILED -> "failed"
        Download.STATE_REMOVING -> "removing"
        Download.STATE_STOPPED -> "stopped"
        else -> "queued"
    }

    fun describe(context: Context, download: Download): JSONObject {
        val item = meta(download)
        item.put("state", stateName(download.state))
        item.put("bytes", download.bytesDownloaded)
        item.put("totalBytes", if (download.contentLength == C.LENGTH_UNSET.toLong()) JSONObject.NULL else download.contentLength)
        val percent = download.percentDownloaded
        item.put("percent", if (percent == C.PERCENTAGE_UNSET.toFloat()) JSONObject.NULL else percent.toDouble())
        val art = artFile(context, download.request.id)
        val artVersion = if (art.isFile) art.lastModified() else 0L
        item.put("hasArt", artVersion > 0)
        item.put("artVersion", artVersion)
        return item
    }

    /** Every download, newest first. Reads the index: not for the main thread. */
    fun list(context: Context): JSONArray {
        val manager = manager(context)
        val live = manager.currentDownloads.associateBy { it.request.id }
        val items = ArrayList<JSONObject>()
        manager.downloadIndex.getDownloads().use { cursor ->
            while (cursor.moveToNext()) {
                val stored = cursor.download
                if (stored.state == Download.STATE_REMOVING) continue
                items.add(describe(context, live[stored.request.id] ?: stored))
            }
        }
        items.sortByDescending { it.optLong("addedAt") }
        return JSONArray(items)
    }
}

/** Keeps downloads running while the app is in the background. */
@OptIn(UnstableApi::class)
class AtrioDownloadService : DownloadService(NOTIFICATION_ID, 1000, CHANNEL_ID, R.string.download_channel, 0) {
    override fun getDownloadManager(): DownloadManager = Downloads.manager(this)

    /** Restarts downloads that waited for a network (or for Wi-Fi) once it
     * is there, without the service sitting in the foreground meanwhile. */
    override fun getScheduler(): Scheduler = PlatformScheduler(this, JOB_ID)

    override fun getForegroundNotification(downloads: MutableList<Download>, notMetRequirements: Int): Notification =
        DownloadNotificationHelper(this, CHANNEL_ID).buildProgressNotification(
            this, android.R.drawable.stat_sys_download, null, null, downloads, notMetRequirements,
        )

    /** Android 15 allows a data-sync service six hours a day in the
     * background, then asks it to stop within seconds. The downloads pause
     * and carry on when the app is next opened. */
    override fun onTimeout(startId: Int, fgsType: Int) {
        if (Build.VERSION.SDK_INT >= 35) {
            Downloads.pausedForTimeout = true
            try {
                Downloads.manager(this).pauseDownloads()
            } catch (_: Exception) {
            }
            stopSelf()
        }
    }

    companion object {
        const val NOTIFICATION_ID = 4101
        const val CHANNEL_ID = "downloads"
        const val JOB_ID = 4102
    }
}
