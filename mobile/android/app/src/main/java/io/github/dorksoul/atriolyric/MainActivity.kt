package io.github.dorksoul.atriolyric

import android.content.Context
import android.content.res.Configuration
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.ColorFilter
import android.graphics.Paint
import android.graphics.PixelFormat
import android.graphics.drawable.Drawable
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.SystemClock
import android.view.View
import android.webkit.CookieManager
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebResourceResponse
import android.webkit.WebView
import androidx.activity.OnBackPressedCallback
import androidx.core.view.WindowCompat
import com.getcapacitor.BridgeActivity
import com.getcapacitor.BridgeWebViewClient
import com.getcapacitor.CapConfig

/**
 * Which page the app shows. With a server set it is the web app, loaded from
 * that server so it runs same-origin with its API (and still has the bridge:
 * Capacitor only gives the bridge to the app's own address, which is why the
 * activity is created with the server as that address). The app keeps its own
 * copy of the page (WebCache), so it opens the same with no connection. The
 * bundled shell page is only for choosing the server, and for a server that
 * has never been reached yet.
 */
object Shell {
    private const val PREFS = "atrio_shell"
    private const val KEY_SERVER = "server_url"

    /** Whether the next (re)creation loads the server's page (when one is
     * set). In memory only: a cold start always begins on the server's page. */
    @Volatile
    var remote = true

    /** Why the shell is showing after the server's page failed, if it did. */
    @Volatile
    var reason: String? = null

    fun serverUrl(context: Context): String? =
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE).getString(KEY_SERVER, null)

    fun setServerUrl(context: Context, url: String) {
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit().putString(KEY_SERVER, url).apply()
    }

    /** "music.example.org" -> "https://music.example.org"; null when it is not an address. */
    fun normalise(input: String): String? {
        var text = input.trim().trimEnd('/')
        if (text.isEmpty()) return null
        if (!text.startsWith("http://") && !text.startsWith("https://")) text = "https://$text"
        val uri = Uri.parse(text)
        if (uri.host.isNullOrEmpty()) return null
        return text
    }
}

/**
 * The colours around the page: behind the status bar (the page's background)
 * and behind the navigation bar (the page's bottom bar), and whether they are
 * light, which decides the colour of the system's icons. The page reports
 * them (plugin `setSystemBars`); they are remembered so the next start is
 * right before the page has loaded.
 */
object SystemBarColours {
    private const val PREFS = "atrio_bars"
    const val DEFAULT_TOP = "#0d1017"
    const val DEFAULT_BOTTOM = "#141925"

    class Colours(val top: Int, val bottom: Int, val light: Boolean)

    fun parse(text: String?): Int? = try {
        if (text != null && Regex("#[0-9a-fA-F]{6}").matches(text)) Color.parseColor(text) else null
    } catch (_: IllegalArgumentException) {
        null
    }

    fun load(context: Context): Colours {
        val prefs = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
        return Colours(
            prefs.getInt("top", Color.parseColor(DEFAULT_TOP)),
            prefs.getInt("bottom", Color.parseColor(DEFAULT_BOTTOM)),
            prefs.getBoolean("light", false),
        )
    }

    fun save(context: Context, colours: Colours) {
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit()
            .putInt("top", colours.top).putInt("bottom", colours.bottom).putBoolean("light", colours.light).apply()
    }
}

/** The window's background: one colour for its top half, one for its bottom
 * half. Only the strips behind the system bars are ever seen. */
private class TwoToneDrawable(private val top: Int, private val bottom: Int) : Drawable() {
    private val paint = Paint()

    override fun draw(canvas: Canvas) {
        val area = bounds
        val middle = area.top + area.height() / 2f
        paint.color = top
        canvas.drawRect(area.left.toFloat(), area.top.toFloat(), area.right.toFloat(), middle, paint)
        paint.color = bottom
        canvas.drawRect(area.left.toFloat(), middle, area.right.toFloat(), area.bottom.toFloat(), paint)
    }

    override fun setAlpha(alpha: Int) {}

    override fun setColorFilter(colorFilter: ColorFilter?) {}

    @Deprecated("Deprecated in Java")
    override fun getOpacity(): Int = PixelFormat.OPAQUE
}

class MainActivity : BridgeActivity() {
    @Volatile
    private var showingRemote = false

    /** Whether this activity shows the server's page (not the bundled one).
     * Read by the plugin from its own thread. */
    val showingServerPage: Boolean
        get() = showingRemote
    private var inForeground = false
    private val updatePageVisibility = Runnable { applyPageVisibility() }
    private val onMusicIntent: () -> Unit = {
        // At once when the music starts; a moment later when it stops, so a
        // hand-over from one slot to the other never counts as stopping.
        schedulePageVisibility(if (PlayerHub.pageNeeded()) 0L else 3_000L)
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        registerPlugin(AtrioNativePlugin::class.java)
        val server = Shell.serverUrl(this)
        showingRemote = Shell.remote && server != null
        if (showingRemote) {
            config = CapConfig.Builder(this)
                .setServerUrl(server)
                .setAllowNavigation(arrayOf(Uri.parse(server).host ?: ""))
                .setBackgroundColor("#0d1017")
                .setAppendedUserAgentString(Net.USER_AGENT)
                .create()
        }
        // The restored state belongs to whichever page was showing before.
        super.onCreate(null)

        if (showingRemote) {
            val serverHost = Uri.parse(server).host
            bridge.setWebViewClient(object : BridgeWebViewClient(bridge) {
                // A sign-in gateway in front of the server sends the page to its
                // own address to log in and back again. Those hops stay in the
                // app: a redirect, or any step taken while on the gateway. A
                // link the user follows from the app to another site still
                // opens in the browser (Capacitor's default).
                override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
                    val target = request.url
                    if (target.scheme != "http" && target.scheme != "https") return super.shouldOverrideUrlLoading(view, request)
                    if (target.host == serverHost) return false
                    val onGateway = view.url?.let { Uri.parse(it).host } != serverHost
                    if (request.isForMainFrame && (request.isRedirect || onGateway)) return false
                    return super.shouldOverrideUrlLoading(view, request)
                }

                // The page, its files and its artwork come through the app's
                // own copy, which is what opens with no connection.
                // A download's saved artwork has an address of its own on the
                // server's origin (Downloads.artResponse), answered here.
                override fun shouldInterceptRequest(view: WebView, request: WebResourceRequest): WebResourceResponse? =
                    Downloads.artResponse(applicationContext, server!!, request.url)
                        ?: WebCache.intercept(applicationContext, server, request)
                        ?: super.shouldInterceptRequest(view, request)

                // Only reached when there is no copy of the page yet (a server
                // that was never loaded): the shell says so.
                override fun onReceivedError(view: WebView, request: WebResourceRequest, error: WebResourceError) {
                    super.onReceivedError(view, request, error)
                    if (request.isForMainFrame) show(remote = false, reason = "unreachable")
                }

                override fun onReceivedHttpError(view: WebView, request: WebResourceRequest, errorResponse: WebResourceResponse) {
                    super.onReceivedHttpError(view, request, errorResponse)
                    if (request.isForMainFrame && errorResponse.statusCode >= 500) show(remote = false, reason = "unreachable")
                }
            })
        }

        PlayerHub.onMusicIntent = onMusicIntent

        // After Capacitor's own set-up of the bars, which is posted.
        window.decorView.post { applySystemBars() }

        // The web app closes its own layers with Back; leaving the first page
        // puts the app in the background instead of ending it (and the music).
        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() {
                val webView = bridge?.webView
                if (webView != null && webView.canGoBack()) webView.goBack() else moveTaskToBack(true)
            }
        })
    }

    /** Switches between the server's page and the bundled shell. */
    fun show(remote: Boolean, reason: String?) {
        if (isFinishing || isDestroyed) return
        if (remote == showingRemote && remote == Shell.remote) return
        Shell.remote = remote
        Shell.reason = reason
        recreate()
    }

    /** Paints the areas behind the status and navigation bars in the page's
     * colours and picks dark or light system icons to suit. */
    fun applySystemBars() {
        if (isFinishing || isDestroyed) return
        val colours = SystemBarColours.load(this)
        window.decorView.background = TwoToneDrawable(colours.top, colours.bottom)
        // Before Android 15 the bars have colours of their own.
        if (Build.VERSION.SDK_INT < 35) {
            @Suppress("DEPRECATION")
            window.statusBarColor = colours.top
            @Suppress("DEPRECATION")
            window.navigationBarColor = colours.bottom
        }
        if (Build.VERSION.SDK_INT >= 29) window.isNavigationBarContrastEnforced = false
        val controller = WindowCompat.getInsetsController(window, window.decorView)
        controller.isAppearanceLightStatusBars = colours.light
        controller.isAppearanceLightNavigationBars = colours.light
    }

    override fun onConfigurationChanged(newConfig: Configuration) {
        // Capacitor resets the bars to the system's light or dark here.
        super.onConfigurationChanged(newConfig)
        applySystemBars()
    }

    override fun onStart() {
        super.onStart()
        inForeground = true
        PlayerHub.main.removeCallbacks(updatePageVisibility)
        // Downloads Android would not let start from the background go now.
        Downloads.onForeground(this)
    }

    override fun onStop() {
        super.onStop()
        inForeground = false
        // After the window has told the page it is hidden.
        schedulePageVisibility(1_000L)
    }

    override fun onDestroy() {
        PlayerHub.main.removeCallbacks(updatePageVisibility)
        if (PlayerHub.onMusicIntent === onMusicIntent) PlayerHub.onMusicIntent = null
        super.onDestroy()
    }

    private fun schedulePageVisibility(delayMs: Long) {
        if (inForeground) return
        PlayerHub.main.removeCallbacks(updatePageVisibility)
        PlayerHub.main.postDelayed(updatePageVisibility, delayMs)
    }

    /**
     * In the background, while the music plays, the page is kept "visible".
     * The music plays natively, so to Chromium the hidden page is silent, and
     * it freezes a hidden silent page a minute after it was hidden (`freeze`
     * 60 s after `visibilitychange`). A frozen page never hears that a track
     * ended, and the queue would stop there until the app was opened again.
     * Told the window is visible, the page is
     * not frozen and changes tracks as it does in the foreground. A Next or
     * Previous from a headset wakes it the same way for a few seconds, so it
     * can carry the command out with the music paused. When the music stops
     * (or it is radio, or a video's sound) the page gets the real state of the
     * window back: hidden.
     */
    private fun applyPageVisibility() {
        if (inForeground || isFinishing || isDestroyed) return
        val webView = bridge?.webView ?: return
        val needed = PlayerHub.pageNeeded()
        webView.dispatchWindowVisibilityChanged(if (needed) View.VISIBLE else webView.windowVisibility)
        // Woken only for a command: look again once its time is up.
        if (needed && !PlayerHub.musicWantsToPlay()) {
            schedulePageVisibility(maxOf(0L, PlayerHub.pageNeededUntil - SystemClock.elapsedRealtime()) + 500L)
        }
    }

    override fun onPause() {
        super.onPause()
        // The session cookie must survive the process being killed.
        CookieManager.getInstance().flush()
    }
}
