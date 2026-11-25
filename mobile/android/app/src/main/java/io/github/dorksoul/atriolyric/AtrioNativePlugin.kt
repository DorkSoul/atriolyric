package io.github.dorksoul.atriolyric

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.util.Base64
import androidx.annotation.OptIn
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat
import androidx.media3.common.util.UnstableApi
import com.getcapacitor.JSArray
import com.getcapacitor.JSObject
import com.getcapacitor.Plugin
import com.getcapacitor.PluginCall
import com.getcapacitor.PluginMethod
import com.getcapacitor.annotation.CapacitorPlugin
import org.json.JSONArray
import org.json.JSONObject
import java.util.concurrent.Executors

/**
 * Everything the web side can ask of the app: which server to load, the two
 * music players, and the downloads. Called from the page as
 * `Capacitor.nativePromise('AtrioNative', method, options)`; events arrive
 * through `Capacitor.addListener('AtrioNative', name, callback)`:
 *   slot              one player's state (see PlayerHub.Slot.state)
 *   command           { command: 'next' | 'prev' } from the notification or a headset
 *   downloads         { items: [...] } when the list itself changes
 *   downloadProgress  { items: [{ mediaId, state, bytes, totalBytes, percent }] }
 *                     once a second while anything is being fetched
 *   streamHeld        { mediaIds: [...] } the songs ahead in the queue held
 *                     whole on the device (see StreamCache), when that changes
 *
 * Every method body runs inside [guard]: Capacitor turns an exception thrown
 * on its worker thread into a crash of the whole app, so none may escape.
 */
@OptIn(UnstableApi::class)
@CapacitorPlugin(name = "AtrioNative")
class AtrioNativePlugin : Plugin() {
    /** Network and file work that must not hold up Capacitor's one worker
     * thread, which also carries every player command. */
    private val work = Executors.newSingleThreadExecutor()

    private val onSlot: (JSONObject) -> Unit = { state -> notifyListeners("slot", JSObject.fromJSONObject(state)) }
    private val onCommand: (String, String) -> Unit = { name, slot ->
        notifyListeners("command", JSObject().put("command", name).put("slot", slot))
    }
    private val onDownloads: () -> Unit = {
        // The index is read from disk: not on the main thread, and on one
        // thread so the lists reach the page in order.
        Downloads.listExecutor.execute {
            try {
                notifyListeners("downloads", JSObject().put("items", Downloads.list(context)))
            } catch (_: Exception) {
            }
        }
    }
    private val onProgress: (JSONArray) -> Unit = { items ->
        notifyListeners("downloadProgress", JSObject().put("items", items))
    }
    private val onStreamHeld: (Set<Long>) -> Unit = { ids ->
        notifyListeners("streamHeld", JSObject().put("mediaIds", JSONArray(ids.toList())))
    }

    override fun load() {
        PlayerHub.onSlot = onSlot
        PlayerHub.onCommand = onCommand
        Downloads.onChanged = onDownloads
        Downloads.onProgress = onProgress
        StreamCache.onHeld = onStreamHeld
    }

    override fun handleOnDestroy() {
        if (PlayerHub.onSlot === onSlot) PlayerHub.onSlot = null
        if (PlayerHub.onCommand === onCommand) PlayerHub.onCommand = null
        if (Downloads.onChanged === onDownloads) Downloads.onChanged = null
        if (Downloads.onProgress === onProgress) Downloads.onProgress = null
        if (StreamCache.onHeld === onStreamHeld) StreamCache.onHeld = null
        work.shutdown()
    }

    private fun reject(call: PluginCall, error: Throwable) {
        call.reject(error.message ?: error.javaClass.simpleName)
    }

    /** Runs a method body; anything it throws becomes the call's rejection. */
    private inline fun guard(call: PluginCall, block: () -> Unit) {
        try {
            block()
        } catch (error: Exception) {
            reject(call, error)
        }
    }

    private fun onMain(call: PluginCall, block: () -> Unit) {
        PlayerHub.main.post { guard(call, block) }
    }

    private fun inBackground(call: PluginCall, block: () -> Unit) {
        try {
            work.execute { guard(call, block) }
        } catch (error: Exception) {
            reject(call, error)
        }
    }

    /** The calls that choose which server the app trusts belong to the
     * bundled set-up page. A server's page (or a script injected into it on
     * the way) must not be able to point the app somewhere else. */
    private fun onlyFromShell(call: PluginCall): Boolean {
        if ((activity as? MainActivity)?.showingServerPage == true) {
            call.reject("Not allowed from the server's page")
            return false
        }
        return true
    }

    // --- Which page the app shows ---------------------------------------

    @PluginMethod
    fun shellState(call: PluginCall) = guard(call) {
        call.resolve(
            JSObject()
                .put("serverUrl", Shell.serverUrl(context) ?: JSONObject.NULL)
                .put("remote", Shell.remote)
                .put("reason", Shell.reason ?: JSONObject.NULL)
                .put("appVersion", BuildConfig.VERSION_NAME),
        )
    }

    @PluginMethod
    fun setServerUrl(call: PluginCall) = guard(call) {
        if (!onlyFromShell(call)) return@guard
        val url = Shell.normalise(call.getString("url") ?: "")
        if (url == null) {
            call.reject("Enter the address of your Atriolyric server")
            return@guard
        }
        Shell.setServerUrl(context, url)
        call.resolve(JSObject().put("serverUrl", url))
    }

    /** Checks that the saved server answers, then loads the app from it. A
     * server that does not answer is still opened when the app has a copy of
     * its page (it then shows what it loaded before and plays the downloads);
     * otherwise this rejects and the shell says the server cannot be reached.
     * The check runs on a thread of its own: it can take seconds. */
    @PluginMethod
    fun connect(call: PluginCall) = guard(call) {
        if (!onlyFromShell(call)) return@guard
        val url = Shell.serverUrl(context)
        if (url == null) {
            call.reject("No server set")
            return@guard
        }
        inBackground(call) {
            // The server itself answers 200 with its version. A server behind a
            // sign-in gateway (a reverse proxy with its own login) answers with a
            // redirect or a refusal until the user has signed in there: that is
            // reachable too, and the page that loads next is the gateway's login.
            val answer = Net.fetch("$url/api/version", 6000, 64 * 1024, followRedirects = false)
            val code = answer?.code
            val isServer = code == 200 && String(answer.body, Charsets.UTF_8).contains("\"version\"")
            val isGateway = code != null && (code in 300..399 || code == 401 || code == 403)
            if (!isServer && !isGateway && !(code == null && WebCache.hasPage(context, url))) {
                call.reject("unreachable")
                return@inBackground
            }
            call.resolve()
            PlayerHub.main.post { (activity as? MainActivity)?.show(remote = true, reason = null) }
        }
    }

    /** Back to the server's page without asking the server first (leaving the
     * change-server page). */
    @PluginMethod
    fun openApp(call: PluginCall) = guard(call) {
        if (!onlyFromShell(call)) return@guard
        if (Shell.serverUrl(context) == null) {
            call.reject("No server set")
            return@guard
        }
        call.resolve()
        PlayerHub.main.post { (activity as? MainActivity)?.show(remote = true, reason = null) }
    }

    /** Has the app fetch the page's built files it does not hold yet, so
     * every view opens with no connection. `paths` are /assets/... paths. */
    @PluginMethod
    fun keepPages(call: PluginCall) = guard(call) {
        val url = Shell.serverUrl(context)
        val list = call.getArray("paths")
        if (url != null && list != null) {
            WebCache.keep(context, url, (0 until list.length()).mapNotNull { list.optString(it).takeIf { path -> path.isNotEmpty() } })
        }
        call.resolve()
    }

    /** Leaves the server's page for the bundled one, to change the server. */
    @PluginMethod
    fun openShell(call: PluginCall) = guard(call) {
        val reason = call.getString("reason")
        call.resolve()
        PlayerHub.main.post { (activity as? MainActivity)?.show(remote = false, reason = reason) }
    }

    /** The page's colours for the areas behind the status bar (`top`) and the
     * navigation bar (`bottom`), as #rrggbb, and whether they are light. */
    @PluginMethod
    fun setSystemBars(call: PluginCall) = guard(call) {
        val top = SystemBarColours.parse(call.getString("top"))
        val bottom = SystemBarColours.parse(call.getString("bottom"))
        if (top == null || bottom == null) {
            call.reject("setSystemBars needs top and bottom as #rrggbb")
            return@guard
        }
        SystemBarColours.save(context, SystemBarColours.Colours(top, bottom, call.getBoolean("light", false) == true))
        call.resolve()
        PlayerHub.main.post { (activity as? MainActivity)?.applySystemBars() }
    }

    /** Downloads a newer APK from the app's own server and hands it to
     * Android's installer, which asks the user before replacing the app.
     * Only from the saved server's address, and only an APK of this app,
     * signed with the same key and newer than what is installed: a page that
     * is not what it seems cannot use this to offer some other app. Resolves
     * once the installer is showing. */
    @PluginMethod
    fun installUpdate(call: PluginCall) = guard(call) {
        val url = call.getString("url")
        if (url.isNullOrEmpty()) {
            call.reject("installUpdate needs a url")
            return@guard
        }
        if (!Net.sameOrigin(url, Shell.serverUrl(context))) {
            call.reject("The app only updates from its own server")
            return@guard
        }
        val target = java.io.File(java.io.File(context.cacheDir, "update"), "atriolyric.apk")
        inBackground(call) {
            val code = Net.download(url, target, 60_000)
            // An APK is a ZIP: anything else (a login page, an error) is not installable.
            val looksLikeApk = code == 200 && target.length() > 4 &&
                target.inputStream().use { it.read() == 'P'.code && it.read() == 'K'.code }
            if (!looksLikeApk) {
                target.delete()
                call.reject(if (code == null) "Could not reach the server" else "The server did not send the app (status $code)")
                return@inBackground
            }
            val problem = UpdateCheck.problem(context, target)
            if (problem != null) {
                target.delete()
                call.reject(problem)
                return@inBackground
            }
            val uri = androidx.core.content.FileProvider.getUriForFile(context, context.packageName + ".fileprovider", target)
            val install = Intent(Intent.ACTION_VIEW)
                .setDataAndType(uri, "application/vnd.android.package-archive")
                .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_ACTIVITY_NEW_TASK)
            activity.startActivity(install)
            call.resolve(JSObject().put("bytes", target.length()))
        }
    }

    @PluginMethod
    fun requestNotifications(call: PluginCall) = guard(call) {
        if (Build.VERSION.SDK_INT >= 33 &&
            ContextCompat.checkSelfPermission(context, Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED
        ) {
            ActivityCompat.requestPermissions(activity, arrayOf(Manifest.permission.POST_NOTIFICATIONS), 7001)
        }
        call.resolve()
    }

    /** Whether there is a network and whether it is metered (mobile data). */
    @PluginMethod
    fun networkInfo(call: PluginCall) = guard(call) {
        val (connected, metered) = Net.networkInfo(context)
        call.resolve(JSObject().put("connected", connected).put("metered", metered))
    }

    // --- The music players ----------------------------------------------

    @PluginMethod
    fun playerLoad(call: PluginCall) = guard(call) {
        val slot = call.getString("slot") ?: "a"
        val mediaId = (call.getDouble("mediaId") ?: 0.0).toLong()
        val url = call.getString("url")
        // A downloaded copy always wins, whatever quality the page asked for.
        val download = if (mediaId > 0) Downloads.completed(context, mediaId) else null
        if (download == null && url.isNullOrEmpty()) {
            call.reject("Not downloaded")
            return@guard
        }
        val savedArt = if (download != null) Downloads.artFile(context, download.request.id).takeIf { it.isFile } else null
        val source = SlotSource(
            loadId = call.getInt("loadId") ?: 0,
            mediaId = mediaId,
            uri = download?.request?.uri ?: Uri.parse(url),
            cacheKey = download?.request?.customCacheKey,
            positionMs = ((call.getDouble("position") ?: 0.0) * 1000).toLong(),
            title = call.getString("title"),
            artist = call.getString("artist"),
            album = call.getString("album"),
            artwork = savedArt?.let { Uri.fromFile(it) } ?: call.getString("artUrl")?.takeIf { it.isNotEmpty() }?.let { Uri.parse(it) },
        )
        val next = call.getBoolean("next", false) == true
        onMain(call) {
            PlayerHub.load(context, slot, source, next)
            call.resolve(JSObject().put("downloaded", download != null))
        }
    }

    /** Marks a slot's current load as the queue's next track (or not), so it
     * may start by itself when the playing slot ends. */
    @PluginMethod
    fun playerArm(call: PluginCall) = guard(call) {
        val slot = call.getString("slot") ?: "a"
        val loadId = call.getInt("loadId") ?: 0
        val armed = call.getBoolean("armed", false) == true
        onMain(call) {
            PlayerHub.slot(context, slot).arm(loadId, armed)
            call.resolve()
        }
    }

    @PluginMethod
    fun playerPlay(call: PluginCall) = guard(call) {
        val slot = call.getString("slot") ?: "a"
        onMain(call) {
            PlayerHub.play(context, slot)
            call.resolve()
        }
    }

    @PluginMethod
    fun playerPause(call: PluginCall) = guard(call) {
        val slot = call.getString("slot") ?: "a"
        onMain(call) {
            PlayerHub.slot(context, slot).player.pause()
            call.resolve()
        }
    }

    @PluginMethod
    fun playerSeek(call: PluginCall) = guard(call) {
        val slot = call.getString("slot") ?: "a"
        val position = ((call.getDouble("position") ?: 0.0) * 1000).toLong()
        onMain(call) {
            PlayerHub.slot(context, slot).player.seekTo(position)
            call.resolve()
        }
    }

    @PluginMethod
    fun playerVolume(call: PluginCall) = guard(call) {
        val slot = call.getString("slot") ?: "a"
        val volume = (call.getDouble("volume") ?: 1.0).toFloat().coerceIn(0f, 1f)
        onMain(call) {
            PlayerHub.slot(context, slot).player.volume = volume
            call.resolve()
        }
    }

    /** The equaliser's ten band gains in dB, and whether the limiter runs
     * (it does while the equaliser or volume levelling is in use). */
    @PluginMethod
    fun playerEqualiser(call: PluginCall) = guard(call) {
        val list = call.getArray("gains")
        val gains = DoubleArray(Equaliser.BANDS.size) { index -> list?.optDouble(index, 0.0) ?: 0.0 }
        val limiter = call.getBoolean("limiter") ?: false
        onMain(call) {
            PlayerHub.setEqualiser(gains, limiter)
            call.resolve()
        }
    }

    /** A music slot's volume-levelling gain in dB for the track it holds. */
    @PluginMethod
    fun playerGain(call: PluginCall) = guard(call) {
        val slot = call.getString("slot") ?: "a"
        val gainDb = call.getDouble("gainDb") ?: 0.0
        onMain(call) {
            PlayerHub.slot(context, slot).equaliser?.setGainDb(gainDb)
            call.resolve()
        }
    }

    @PluginMethod
    fun playerUnload(call: PluginCall) = guard(call) {
        val slot = call.getString("slot") ?: "a"
        onMain(call) {
            PlayerHub.slot(context, slot).unload()
            call.resolve()
        }
    }

    /** The songs after the playing and the next one, in play order
     * (`items: [{ mediaId, url }]`), to hold on the device for a loss of
     * signal (see StreamCache). Resolves with the ones already held whole. */
    @PluginMethod
    fun streamPrefetch(call: PluginCall) = guard(call) {
        val list = call.getArray("items") ?: JSArray()
        val items = ArrayList<StreamCache.Wanted>()
        for (i in 0 until list.length()) {
            val item = list.optJSONObject(i) ?: continue
            val url = item.optString("url")
            val mediaId = item.optLong("mediaId")
            if (url.isEmpty() || mediaId <= 0) continue
            items.add(StreamCache.Wanted(mediaId, url))
        }
        StreamCache.want(context, items)
        call.resolve(JSObject().put("mediaIds", JSONArray(StreamCache.held().toList())))
    }

    /** The songs ahead in the queue held whole now, as the event above. */
    @PluginMethod
    fun streamHeld(call: PluginCall) = guard(call) {
        call.resolve(JSObject().put("mediaIds", JSONArray(StreamCache.held().toList())))
    }

    /** Both slots as they are now (a page that just loaded asks once). */
    @PluginMethod
    fun playerState(call: PluginCall) = guard(call) {
        onMain(call) {
            val slots = JSArray()
            for (slot in PlayerHub.existing()) slots.put(slot.state())
            call.resolve(JSObject().put("slots", slots).put("activeSlot", PlayerHub.activeSlot))
        }
    }

    // --- Downloads --------------------------------------------------------

    @PluginMethod
    fun downloadStart(call: PluginCall) = guard(call) {
        val url = call.getString("url")
        val mediaId = (call.getDouble("mediaId") ?: 0.0).toLong()
        if (url.isNullOrEmpty() || mediaId <= 0) {
            call.reject("downloadStart needs mediaId and url")
            return@guard
        }
        val meta = JSONObject()
            .put("mediaId", mediaId)
            .put("kind", call.getString("kind") ?: "track")
            .put("title", call.getString("title") ?: "")
            .put("artist", call.getString("artist") ?: "")
            .put("album", call.getString("album") ?: "")
            .put("quality", call.getString("quality") ?: "original")
            .put("qualityLabel", call.getString("qualityLabel") ?: "")
            .put("duration", call.getDouble("duration") ?: 0.0)
        Downloads.start(context, meta, url, call.getString("artUrl"), call.getBoolean("replace", false) == true)
        call.resolve()
    }

    @PluginMethod
    fun downloadRemove(call: PluginCall) = guard(call) {
        val mediaId = (call.getDouble("mediaId") ?: 0.0).toLong()
        Downloads.remove(context, mediaId)
        call.resolve()
    }

    @PluginMethod
    fun downloadList(call: PluginCall) = guard(call) {
        call.resolve(JSObject().put("items", Downloads.list(context)))
    }

    /** Downloads only on Wi-Fi (an unmetered network), or on any network. */
    @PluginMethod
    fun downloadSetRequirements(call: PluginCall) = guard(call) {
        Downloads.setWifiOnly(context, call.getBoolean("wifiOnly", false) == true)
        call.resolve()
    }

    /** The saved artwork of a download as a data: URL, or null, for
     * pages that do not use `/__atrio/download-art/<id>` (see Downloads.artResponse). */
    @PluginMethod
    fun downloadArt(call: PluginCall) = guard(call) {
        val mediaId = (call.getDouble("mediaId") ?: 0.0).toLong()
        val file = Downloads.artFile(context, Downloads.id(mediaId))
        if (!file.isFile) {
            call.resolve(JSObject().put("dataUrl", JSONObject.NULL))
            return@guard
        }
        val bytes = file.readBytes()
        val type = Downloads.artMime(bytes.copyOf(minOf(bytes.size, 12)))
        call.resolve(JSObject().put("dataUrl", "data:$type;base64," + Base64.encodeToString(bytes, Base64.NO_WRAP)))
    }

    /** Plays a downloaded video in the native full-screen player. */
    @PluginMethod
    fun playVideo(call: PluginCall) = guard(call) {
        val mediaId = (call.getDouble("mediaId") ?: 0.0).toLong()
        if (Downloads.completed(context, mediaId) == null) {
            call.reject("Not downloaded")
            return@guard
        }
        onMain(call) {
            PlayerHub.pauseAll()
            activity.startActivity(Intent(activity, VideoActivity::class.java).putExtra(VideoActivity.EXTRA_MEDIA_ID, mediaId))
            call.resolve()
        }
    }

    /** What the native video player is doing (for the page and the tests). */
    @PluginMethod
    fun videoState(call: PluginCall) = guard(call) {
        onMain(call) { call.resolve(JSObject.fromJSONObject(VideoActivity.state())) }
    }
}
