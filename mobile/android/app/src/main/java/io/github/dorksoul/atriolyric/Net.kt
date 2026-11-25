package io.github.dorksoul.atriolyric

import android.content.Context
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import android.net.Uri
import android.webkit.CookieManager
import androidx.annotation.OptIn
import androidx.media3.common.util.UnstableApi
import androidx.media3.datasource.DataSource
import androidx.media3.datasource.DataSpec
import androidx.media3.datasource.DefaultDataSource
import androidx.media3.datasource.DefaultHttpDataSource
import androidx.media3.datasource.TransferListener
import androidx.media3.datasource.cache.CacheDataSource
import java.io.IOException
import java.net.CookieHandler
import java.net.HttpURLConnection
import java.net.URI
import java.net.URL

/**
 * HTTP for the native side. The server signs a user in with a session cookie
 * that lives in the WebView's cookie store. Every native request reads it
 * from there through [WebViewCookies], installed as the process's cookie
 * handler: the connection asks it for each request it makes, redirects
 * included, with that request's own address. So a cookie only ever goes to
 * the host it belongs to (a redirect to a sign-in gateway or anywhere else
 * does not carry the server's session along), and a new login or a logout
 * applies at once.
 */
@OptIn(UnstableApi::class)
object Net {
    const val USER_AGENT = "AtriolyricApp"
    private const val MAX_REDIRECTS = 5

    /** Installs [WebViewCookies] once, before the first native request. */
    fun install() {
        if (CookieHandler.getDefault() !is WebViewCookies) CookieHandler.setDefault(WebViewCookies())
    }

    fun cookieFor(url: String): String? = try {
        CookieManager.getInstance().getCookie(url)
    } catch (_: Exception) {
        null
    }

    /** Whether a media answer may be kept or played: anything but a web
     * page. A sign-in gateway whose session ran out answers a stream request
     * with its login page, and that must fail rather than be saved as a song. */
    fun isMediaType(contentType: String?): Boolean =
        contentType == null || !contentType.trim().lowercase().startsWith("text/html")

    /** Whether saved artwork may be what came back: an image. */
    fun isImageType(contentType: String?): Boolean =
        contentType != null && contentType.trim().lowercase().startsWith("image/")

    /** Same scheme, host and port: the only redirects the app follows on its
     * own, and the only update address it accepts. */
    fun sameOrigin(a: String?, b: String?): Boolean {
        if (a == null || b == null) return false
        return try {
            val left = URI(a)
            val right = URI(b)
            left.scheme.equals(right.scheme, ignoreCase = true) &&
                left.host != null && left.host.equals(right.host, ignoreCase = true) &&
                portOf(left) == portOf(right)
        } catch (_: Exception) {
            false
        }
    }

    private fun portOf(uri: URI): Int = when {
        uri.port != -1 -> uri.port
        uri.scheme.equals("https", ignoreCase = true) -> 443
        uri.scheme.equals("http", ignoreCase = true) -> 80
        else -> -1
    }

    /** Plain HTTP with the session cookie (through the cookie handler). A
     * capped-bitrate song is transcoded on its first request, so the answer
     * can take a while to start. No https-to-http redirects, no web pages,
     * and the answer must come from the host that was asked. */
    fun http(): DataSource.Factory {
        install()
        val base = DefaultHttpDataSource.Factory()
            .setUserAgent(USER_AGENT)
            .setAllowCrossProtocolRedirects(false)
            .setConnectTimeoutMs(15_000)
            .setReadTimeoutMs(180_000)
            .setContentTypePredicate { isMediaType(it) }
        return DataSource.Factory { SameHostDataSource(base.createDataSource()) }
    }

    /** What the players read through: a downloaded copy if there is one (the
     * download cache, never written to from here), the network otherwise, and
     * plain files for saved artwork. The music players (`music`) also read the
     * songs held ahead of the queue ([StreamCache]) before the network. */
    fun playback(context: Context, music: Boolean = false): DataSource.Factory {
        val cached = CacheDataSource.Factory()
            .setCache(Downloads.cache(context))
            .setUpstreamDataSourceFactory(if (music) StreamCache.reader(context, http()) else http())
            .setCacheWriteDataSinkFactory(null)
            .setFlags(CacheDataSource.FLAG_IGNORE_CACHE_ON_ERROR)
        return DefaultDataSource.Factory(context, cached)
    }

    /** An answer to [fetch]: the status, the body and its type. */
    class Answer(val code: Int, val body: ByteArray, val contentType: String?)

    /** Opens `url`, following redirects only within the same origin (the
     * connection's own following would also go to other hosts). A redirect
     * elsewhere comes back as it is, for the caller to treat as an answer. */
    private fun open(url: String, timeoutMs: Int, followRedirects: Boolean): HttpURLConnection {
        install()
        var target = url
        repeat(MAX_REDIRECTS + 1) {
            val connection = URL(target).openConnection() as HttpURLConnection
            connection.instanceFollowRedirects = false
            connection.connectTimeout = timeoutMs
            connection.readTimeout = timeoutMs
            connection.setRequestProperty("User-Agent", USER_AGENT)
            val code = connection.responseCode
            val location = connection.getHeaderField("Location")
            if (!followRedirects || code !in 300..399 || location == null) return connection
            val next = try {
                URL(URL(target), location).toString()
            } catch (_: Exception) {
                return connection
            }
            if (!sameOrigin(target, next)) return connection
            connection.disconnect()
            target = next
        }
        throw IOException("Too many redirects")
    }

    /** Downloads `url` (with the session cookie) into `target`; the HTTP
     * status on an answer, null when the request itself failed. */
    fun download(url: String, target: java.io.File, timeoutMs: Int): Int? {
        var connection: HttpURLConnection? = null
        return try {
            connection = open(url, timeoutMs, followRedirects = true)
            val code = connection.responseCode
            if (code in 200..299) {
                target.parentFile?.mkdirs()
                connection.inputStream.use { input -> target.outputStream().use { output -> input.copyTo(output) } }
            }
            code
        } catch (_: Exception) {
            target.delete()
            null
        } finally {
            connection?.disconnect()
        }
    }

    /** A small GET with the session cookie; null on any failure. */
    fun fetch(url: String, timeoutMs: Int, maxBytes: Int = 4 * 1024 * 1024, followRedirects: Boolean = true): Answer? {
        var connection: HttpURLConnection? = null
        return try {
            connection = open(url, timeoutMs, followRedirects)
            val code = connection.responseCode
            val stream = if (code in 200..299) connection.inputStream else connection.errorStream
            val body = stream?.use { input ->
                val out = java.io.ByteArrayOutputStream()
                val buffer = ByteArray(16 * 1024)
                while (true) {
                    val read = input.read(buffer)
                    if (read < 0) break
                    out.write(buffer, 0, read)
                    if (out.size() > maxBytes) return null
                }
                out.toByteArray()
            } ?: ByteArray(0)
            Answer(code, body, connection.contentType)
        } catch (_: Exception) {
            null
        } finally {
            connection?.disconnect()
        }
    }

    /** Whether there is a network, and whether it is one the user pays for
     * by the megabyte (mobile data, a metered hotspot). */
    fun networkInfo(context: Context): Pair<Boolean, Boolean> = try {
        val manager = context.getSystemService(Context.CONNECTIVITY_SERVICE) as ConnectivityManager
        val network = manager.activeNetwork
        val capabilities = network?.let { manager.getNetworkCapabilities(it) }
        val connected = capabilities?.hasCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET) == true
        connected to (connected && manager.isActiveNetworkMetered)
    } catch (_: Exception) {
        true to false
    }

    fun uriHost(uri: Uri?): String? = uri?.host?.lowercase()
}

/**
 * The WebView's cookie store as the process's cookie handler, so plain
 * connections (Media3's, ours) send and keep the same cookies as the page:
 * per request, by that request's address.
 */
class WebViewCookies : CookieHandler() {
    override fun get(uri: URI, requestHeaders: MutableMap<String, MutableList<String>>): MutableMap<String, MutableList<String>> {
        val cookie = try {
            CookieManager.getInstance().getCookie(uri.toString())
        } catch (_: Exception) {
            null
        }
        return if (cookie.isNullOrEmpty()) mutableMapOf() else mutableMapOf("Cookie" to mutableListOf(cookie))
    }

    override fun put(uri: URI, responseHeaders: MutableMap<String, MutableList<String>>) {
        val manager = try {
            CookieManager.getInstance()
        } catch (_: Exception) {
            return
        }
        for ((name, values) in responseHeaders) {
            // The status line comes with a null name; `equals` here is null-safe.
            if (!(name.equals("Set-Cookie", ignoreCase = true) || name.equals("Set-Cookie2", ignoreCase = true))) continue
            for (value in values) manager.setCookie(uri.toString(), value)
        }
    }
}

/**
 * A data source that refuses an answer from another host than the one asked
 * (the connection follows same-protocol redirects on its own). A song is
 * never fetched from wherever a redirect points, such as a sign-in page.
 */
@OptIn(UnstableApi::class)
private class SameHostDataSource(private val upstream: DataSource) : DataSource {
    override fun addTransferListener(transferListener: TransferListener) = upstream.addTransferListener(transferListener)

    override fun open(dataSpec: DataSpec): Long {
        val length = upstream.open(dataSpec)
        val asked = Net.uriHost(dataSpec.uri)
        val answered = Net.uriHost(upstream.uri)
        if (answered != null && asked != null && answered != asked) {
            upstream.close()
            throw IOException("Redirected to another host ($answered)")
        }
        return length
    }

    override fun read(buffer: ByteArray, offset: Int, length: Int): Int = upstream.read(buffer, offset, length)

    override fun getUri(): Uri? = upstream.uri

    override fun getResponseHeaders(): Map<String, List<String>> = upstream.responseHeaders

    override fun close() = upstream.close()
}
