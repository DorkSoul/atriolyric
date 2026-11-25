package io.github.dorksoul.atriolyric

import android.content.Context
import android.net.Uri
import android.os.Handler
import android.os.Looper
import androidx.annotation.OptIn
import androidx.media3.common.C
import androidx.media3.common.util.UnstableApi
import androidx.media3.datasource.DataSource
import androidx.media3.datasource.DataSpec
import androidx.media3.datasource.cache.CacheDataSource
import androidx.media3.datasource.cache.CacheWriter
import androidx.media3.datasource.cache.ContentMetadata
import androidx.media3.datasource.cache.LeastRecentlyUsedCacheEvictor
import androidx.media3.datasource.cache.SimpleCache
import java.io.File
import java.io.IOException
import java.util.concurrent.Executors

/**
 * Songs further ahead in the queue, kept on the device so that a short loss
 * of signal (a lift, a tunnel) does not stop the music. The browser holds the
 * same tracks in memory (web/src/player/prefetch.ts); here they go to a disk
 * cache that the players read from before the network.
 *
 * The playing track and the next one need no copy here: the music players
 * load each of them whole (see [PlayerHub]). The page names the tracks after
 * those ([want]); they are fetched one at a time, in play order, within
 * [AHEAD_BUDGET]. The players read this cache without writing to it, so they
 * never hold a lock that would stop a fetch here (and a track this is still
 * fetching is streamed as usual).
 */
@OptIn(UnstableApi::class)
object StreamCache {
    /** The most the cache keeps; the oldest copies go first. */
    private const val MAX_BYTES = 400L * 1024 * 1024

    /** The most the wanted tracks may take together before no further one
     * is fetched, and the most one track may take (as in prefetch.ts). */
    private const val AHEAD_BUDGET = 150L * 1024 * 1024
    private const val MAX_TRACK_BYTES = 96L * 1024 * 1024

    /** A fetch that failed (no signal) is tried again this much later, as
     * long as the same tracks are wanted, up to [MAX_RETRIES] times. */
    private const val RETRY_MS = 20_000L
    private const val MAX_RETRIES = 30

    class Wanted(val mediaId: Long, val url: String)

    private var instance: SimpleCache? = null
    private val worker = Executors.newSingleThreadExecutor()
    private val main = Handler(Looper.getMainLooper())
    private val lock = Any()

    // Guarded by `lock`.
    private var wanted: List<Wanted> = emptyList()
    private var generation = 0
    private var runningUrl: String? = null
    private var runningWriter: CacheWriter? = null
    private var retries = 0
    private var lastHeld: Set<Long> = emptySet()

    /** Told (on the main thread) which wanted tracks are held whole, each
     * time that changes. */
    @Volatile
    var onHeld: ((Set<Long>) -> Unit)? = null

    @Synchronized
    fun cache(context: Context): SimpleCache =
        instance ?: SimpleCache(
            File(context.applicationContext.cacheDir, "stream"),
            LeastRecentlyUsedCacheEvictor(MAX_BYTES),
            Downloads.database(context),
        ).also { instance = it }

    /** What the music players read through below the downloads: a copy from
     * here if there is one, the network otherwise. Never written from here. */
    fun reader(context: Context, upstream: DataSource.Factory): DataSource.Factory =
        CacheDataSource.Factory()
            .setCache(cache(context))
            .setUpstreamDataSourceFactory(upstream)
            .setCacheWriteDataSinkFactory(null)
            .setFlags(CacheDataSource.FLAG_IGNORE_CACHE_ON_ERROR)

    /** The tracks to hold, in play order. Any fetch for a track no longer in
     * the list stops; what it already has stays for the players to use. */
    fun want(context: Context, items: List<Wanted>) {
        val app = context.applicationContext
        synchronized(lock) {
            if (items.map { it.url } == wanted.map { it.url }) return
            wanted = items
            generation += 1
            retries = 0
            val running = runningUrl
            if (running != null && items.none { it.url == running }) runningWriter?.cancel()
        }
        worker.execute { drain(app) }
    }

    fun held(): Set<Long> = synchronized(lock) { lastHeld }

    private fun isComplete(cache: SimpleCache, key: String): Boolean {
        val length = ContentMetadata.getContentLength(cache.getContentMetadata(key))
        return length > 0 && cache.isCached(key, 0, length)
    }

    /** Fetches the wanted tracks that are not held yet, one at a time. Runs
     * on [worker] only. */
    private fun drain(context: Context) {
        val cache = cache(context)
        while (true) {
            val (items, gen) = synchronized(lock) { wanted to generation }
            reportHeld(cache, items)
            var used = 0L
            var target: Wanted? = null
            for (item in items) {
                if (isComplete(cache, item.url)) {
                    used += cache.getCachedBytes(item.url, 0, C.LENGTH_UNSET.toLong())
                    continue
                }
                // A downloaded copy is what plays; nothing to fetch.
                val downloaded = try {
                    Downloads.completed(context, item.mediaId) != null
                } catch (_: Exception) {
                    false
                }
                if (downloaded) continue
                target = item
                break
            }
            if (target == null || used >= AHEAD_BUDGET) return
            val room = minOf(MAX_TRACK_BYTES, AHEAD_BUDGET - used)
            val ok = fetch(context, cache, target, gen, room)
            synchronized(lock) {
                if (gen != generation) return@synchronized
                if (!ok) {
                    if (retries < MAX_RETRIES) {
                        retries += 1
                        main.postDelayed({ retry(context, gen) }, RETRY_MS)
                    }
                    reportHeld(cache, items)
                    return
                }
            }
        }
    }

    private fun retry(context: Context, gen: Int) {
        if (synchronized(lock) { gen != generation }) return
        worker.execute { drain(context) }
    }

    /** One track into the cache; false when it could not be fetched now. A
     * track too large for `room` is dropped and counts as done. */
    private fun fetch(context: Context, cache: SimpleCache, item: Wanted, gen: Int, room: Long): Boolean {
        val source = CacheDataSource.Factory()
            .setCache(cache)
            .setUpstreamDataSourceFactory(Net.http())
            .createDataSourceForDownloading()
        var tooLarge = false
        val writer = CacheWriter(source, DataSpec(Uri.parse(item.url)), null) { requestLength, bytesCached, _ ->
            if ((requestLength != C.LENGTH_UNSET.toLong() && requestLength > room) || bytesCached > room) {
                tooLarge = true
                synchronized(lock) { runningWriter }?.cancel()
            }
        }
        synchronized(lock) {
            if (gen != generation && wanted.none { it.url == item.url }) return true
            runningUrl = item.url
            runningWriter = writer
        }
        return try {
            writer.cache()
            true
        } catch (_: IOException) {
            if (tooLarge) {
                cache.removeResource(item.url)
                // Not fetched again while the list is the same.
                synchronized(lock) { wanted = wanted.filter { it.url != item.url } }
                true
            } else {
                // Cancelled for a new list (that list's own run carries on),
                // or no network: tried again later.
                synchronized(lock) { gen != generation }
            }
        } catch (_: Exception) {
            false
        } finally {
            synchronized(lock) {
                if (runningWriter === writer) {
                    runningWriter = null
                    runningUrl = null
                }
            }
        }
    }

    private fun reportHeld(cache: SimpleCache, items: List<Wanted>) {
        val held = items.filter { isComplete(cache, it.url) }.map { it.mediaId }.toSet()
        val changed = synchronized(lock) {
            if (held == lastHeld) false else {
                lastHeld = held
                true
            }
        }
        if (changed) main.post { onHeld?.invoke(held) }
    }
}
