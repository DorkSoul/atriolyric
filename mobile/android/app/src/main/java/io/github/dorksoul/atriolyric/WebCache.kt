package io.github.dorksoul.atriolyric

import android.content.Context
import android.net.ConnectivityManager
import android.net.Uri
import android.webkit.WebResourceRequest
import android.webkit.WebResourceResponse
import org.json.JSONObject
import java.io.ByteArrayInputStream
import java.io.ByteArrayOutputStream
import java.io.File
import java.io.InputStream
import java.net.HttpURLConnection
import java.net.URL
import java.security.MessageDigest
import java.util.concurrent.Executors

/**
 * The app's own copy of the web app and of the artwork it has shown, so the
 * same page opens and looks the same with no connection. The page is loaded
 * from the user's server; the requests for it pass through here:
 *
 *  - the page and the files around it: asked from the server each time, and
 *    the copy from last time when the server does not answer;
 *  - its built files under /assets/ (their names change with their content):
 *    the copy, once there is one;
 *  - artwork: the copy while it is less than a day old, the server otherwise,
 *    and an older copy when the server does not answer.
 *
 * Everything else (the API, streams, the playback socket) is not touched.
 * Cookies travel through the process's cookie handler ([WebViewCookies]),
 * which reads and writes the WebView's own store.
 */
object WebCache {
    private const val PAGE_DIR = "webapp"
    private const val ART_DIR = "webart"

    /** The page copy is app data: an offline start needs it, so the system
     * must not clear it. Artwork can always be fetched again, so it sits in
     * the cache folder, which "Clear cache" (and the system, when space runs
     * low) may empty. */
    private fun dirFor(context: Context, kind: Kind): File =
        if (kind == Kind.ART) File(context.cacheDir, ART_DIR) else File(context.filesDir, PAGE_DIR)
    private const val MAX_BODY = 40 * 1024 * 1024
    private const val MAX_ERROR_BODY = 1024 * 1024
    private const val ART_FRESH_MS = 24 * 60 * 60 * 1000L
    private const val PAGE_LIMIT = 300L * 1024 * 1024
    private const val ART_LIMIT = 400L * 1024 * 1024

    /** After a request that could not connect, the copies are used without
     * asking again until this time: a page is hundreds of requests, and each
     * would otherwise wait for its own timeout. */
    @Volatile
    private var serverDownUntil = 0L

    private val background = Executors.newSingleThreadExecutor()
    private val artPath = Regex("^/api/(art/[^/]+/[^/]+(/thumb)?|stream/\\d+/(thumbnail|art(/full)?))$")

    internal enum class Kind { PAGE, ASSET, ART }

    private class Copy(val body: File, val meta: File) {
        fun exists() = body.isFile && meta.isFile
    }

    /** What a path of the server is to the copy; null for what it leaves alone. */
    internal fun kindOf(path: String): Kind? = when {
        artPath.matches(path) -> Kind.ART
        path.startsWith("/api/") || path.startsWith("/ws/") || path.startsWith("/__atrio/") -> null
        path.startsWith("/assets/") -> Kind.ASSET
        else -> Kind.PAGE
    }

    private fun copyFor(context: Context, kind: Kind, url: String): Copy {
        val digest = MessageDigest.getInstance("SHA-256").digest(url.toByteArray(Charsets.UTF_8))
        val name = digest.joinToString("") { "%02x".format(it) }.take(40)
        val dir = dirFor(context, kind)
        return Copy(File(dir, name), File(dir, "$name.json"))
    }

    private fun hasNetwork(context: Context): Boolean = try {
        (context.getSystemService(Context.CONNECTIVITY_SERVICE) as ConnectivityManager).activeNetwork != null
    } catch (_: Exception) {
        true
    }

    private fun serve(copy: Copy): WebResourceResponse? = try {
        val meta = JSONObject(copy.meta.readText())
        // The meta file names the length of the body it was written with: a
        // body and meta from different fetches (a crash between the two
        // renames) do not belong together and are not served.
        val length = meta.optLong("length", -1)
        if (length >= 0 && length != copy.body.length()) {
            null
        } else {
            val headers = HashMap<String, String>()
            val saved = meta.optJSONObject("headers")
            saved?.keys()?.forEach { key -> headers[key] = saved.getString(key) }
            // The body keeps its age (how fresh the copy is); pruning goes by the meta file.
            copy.meta.setLastModified(System.currentTimeMillis())
            WebResourceResponse(
                meta.optString("mime", "application/octet-stream"),
                meta.optString("charset").ifEmpty { null },
                200,
                "OK",
                headers,
                copy.body.inputStream(),
            )
        }
    } catch (_: Exception) {
        null
    }

    /** Whether a copy of the page itself exists: the app can open with no connection. */
    fun hasPage(context: Context, serverUrl: String): Boolean =
        copyFor(context, Kind.PAGE, "$serverUrl/").exists()

    /** The answer for a request of the page loaded from `serverUrl`, or null
     * to let the WebView make the request itself. Called off the main thread. */
    fun intercept(context: Context, serverUrl: String, request: WebResourceRequest): WebResourceResponse? {
        if (!request.method.equals("GET", ignoreCase = true)) return null
        val uri = request.url
        val server = Uri.parse(serverUrl)
        if (uri.scheme != server.scheme || uri.host != server.host || uri.port != server.port) return null
        if (request.requestHeaders.keys.any { it.equals("Range", ignoreCase = true) }) return null
        val kind = kindOf(uri.path ?: "/") ?: return null
        // The address without its #fragment: one page, whatever view it opens on.
        val url = uri.buildUpon().fragment(null).build().toString()
        val copy = copyFor(context, kind, url)
        val has = copy.exists()
        if (has) {
            val fresh = when (kind) {
                Kind.ASSET -> true
                Kind.ART -> System.currentTimeMillis() - copy.body.lastModified() < ART_FRESH_MS
                Kind.PAGE -> false
            }
            if (fresh || System.currentTimeMillis() < serverDownUntil || !hasNetwork(context)) {
                serve(copy)?.let { return it }
            }
        }
        val answer = fetch(context, url, request.requestHeaders, copy, quick = has)
        // No answer, or the server in trouble: the copy.
        if (has && (answer.status == null || answer.status >= 500)) serve(copy)?.let { return it }
        // Otherwise the server's own answer, also a refusal or an error, so
        // the WebView does not ask a second time. Only a redirect (a sign-in
        // gateway sending the page to its login) goes back to the WebView,
        // which cannot be handed a redirect and asks again itself.
        return answer.response
    }

    /** `status` is null when the server did not answer at all; `response` is
     * null for a redirect or no answer. */
    private class Answer(val response: WebResourceResponse?, val status: Int?)

    private fun readCapped(input: InputStream?, cap: Int): ByteArray? {
        if (input == null) return ByteArray(0)
        input.use {
            val out = ByteArrayOutputStream()
            val buffer = ByteArray(32 * 1024)
            while (true) {
                val read = it.read(buffer)
                if (read < 0) break
                out.write(buffer, 0, read)
                if (out.size() > cap) return null
            }
            return out.toByteArray()
        }
    }

    private fun headersOf(connection: HttpURLConnection): HashMap<String, String> {
        val headers = HashMap<String, String>()
        for ((name, values) in connection.headerFields) {
            if (name == null || values.isNullOrEmpty()) continue
            // Not the ones that describe the transfer, and no cookies in a file.
            when (name.lowercase()) {
                "content-encoding", "content-length", "transfer-encoding", "set-cookie", "set-cookie2", "connection", "keep-alive" -> continue
            }
            headers[name] = values.joinToString(", ")
        }
        return headers
    }

    /** Asks the server, keeps a 200 answer, and hands back what it said. */
    private fun fetch(
        context: Context,
        url: String,
        requestHeaders: Map<String, String>,
        copy: Copy,
        quick: Boolean,
    ): Answer {
        Net.install()
        var connection: HttpURLConnection? = null
        try {
            connection = URL(url).openConnection() as HttpURLConnection
            connection.instanceFollowRedirects = false
            // With a copy to fall back on, a server that does not answer is given up on sooner.
            connection.connectTimeout = if (quick) 4_000 else 10_000
            connection.readTimeout = 20_000
            for ((name, value) in requestHeaders) {
                // No conditions (there is nothing to answer a 304 with), the
                // encoding is left to the connection (which undoes it), and
                // cookies come from the cookie handler.
                val lower = name.lowercase()
                if (lower.startsWith("if-") || lower == "accept-encoding" || lower == "cookie") continue
                connection.setRequestProperty(name, value)
            }
            val code = try {
                connection.responseCode
            } catch (error: java.io.IOException) {
                serverDownUntil = System.currentTimeMillis() + 10_000
                return Answer(null, null)
            }
            serverDownUntil = 0
            val contentType = connection.contentType ?: "application/octet-stream"
            val mime = contentType.substringBefore(';').trim().ifEmpty { "application/octet-stream" }
            val charset = Regex("charset=([^;]+)", RegexOption.IGNORE_CASE).find(contentType)?.groupValues?.get(1)?.trim()?.trim('"') ?: ""
            val headers = headersOf(connection)
            if (code in 300..399) return Answer(null, code)
            if (code != 200) {
                // Passed on as it is, not kept. (WebResourceResponse takes
                // 1xx-2xx and 4xx-5xx; nothing else reaches here.)
                val body = readCapped(if (code >= 400) connection.errorStream else connection.inputStream, MAX_ERROR_BODY)
                    ?: return Answer(null, code)
                val reason = connection.responseMessage?.takeIf { it.isNotBlank() } ?: if (code >= 400) "Error" else "OK"
                return Answer(WebResourceResponse(mime, charset.ifEmpty { null }, code, reason, headers, ByteArrayInputStream(body)), code)
            }
            val bytes = readCapped(connection.inputStream, MAX_BODY) ?: return Answer(null, code)
            keep(copy, bytes, mime, charset, headers)
            return Answer(WebResourceResponse(mime, charset.ifEmpty { null }, 200, "OK", headers, ByteArrayInputStream(bytes)), 200)
        } catch (_: Exception) {
            return Answer(null, null)
        } finally {
            connection?.disconnect()
        }
    }

    /** Writes a copy through files of its own (two requests for the same
     * address can be fetching at once) and renames them into place, body
     * first; the meta names the body's length (see [serve]). */
    private fun keep(copy: Copy, bytes: ByteArray, mime: String, charset: String, headers: Map<String, String>) {
        val dir = copy.body.parentFile ?: return
        var body: File? = null
        var meta: File? = null
        try {
            dir.mkdirs()
            body = File.createTempFile("body", ".part", dir)
            body.writeBytes(bytes)
            meta = File.createTempFile("meta", ".part", dir)
            meta.writeText(
                JSONObject().put("mime", mime).put("charset", charset).put("length", bytes.size.toLong())
                    .put("headers", JSONObject(headers as Map<*, *>)).toString(),
            )
            if (body.renameTo(copy.body)) meta.renameTo(copy.meta)
        } catch (_: Exception) {
            // Not kept this time; the answer is still good.
        } finally {
            body?.takeIf { it.exists() }?.delete()
            meta?.takeIf { it.exists() }?.delete()
        }
    }

    /** Fetches the built files the page has not asked for yet (views not
     * opened so far), so every view opens with no connection. */
    fun keep(context: Context, serverUrl: String, paths: List<String>) {
        val app = context.applicationContext
        background.execute {
            for (path in paths) {
                if (!path.startsWith("/assets/")) continue
                val url = serverUrl + path
                val copy = copyFor(app, Kind.ASSET, url)
                if (copy.exists()) continue
                fetch(app, url, mapOf("User-Agent" to Net.USER_AGENT), copy, quick = true)
                if (System.currentTimeMillis() < serverDownUntil) return@execute
            }
            prune(dirFor(app, Kind.PAGE), PAGE_LIMIT)
            prune(dirFor(app, Kind.ART), ART_LIMIT)
            // Artwork left in the app-data folder rather than the cache folder.
            File(app.filesDir, ART_DIR).takeIf { it.exists() }?.deleteRecursively()
        }
    }

    /** Drops the copies used longest ago once a folder is over its limit,
     * down to three quarters of it, and any temporary files left by a fetch
     * that did not finish. */
    internal fun prune(dir: File, limit: Long) {
        val dayAgo = System.currentTimeMillis() - 24 * 60 * 60 * 1000L
        dir.listFiles { file -> file.name.endsWith(".part") && file.lastModified() < dayAgo }?.forEach { it.delete() }
        val metas = dir.listFiles { file -> file.name.endsWith(".json") } ?: return
        var total = metas.sumOf { File(it.path.removeSuffix(".json")).length() }
        if (total <= limit) return
        for (meta in metas.sortedBy { it.lastModified() }) {
            if (total <= limit * 3 / 4) break
            val body = File(meta.path.removeSuffix(".json"))
            total -= body.length()
            body.delete()
            meta.delete()
        }
    }
}
