package io.github.dorksoul.atriolyric

import android.app.Application

/**
 * Sets up what every part of the app shares before any of them runs: the
 * cookie handler for native requests, and the download manager. The manager
 * is made here, on the main thread, because Media3 delivers its callbacks on
 * the thread that made it; made first by a plugin call it would live on
 * Capacitor's worker thread, apart from the download service and the players.
 */
class AtrioApp : Application() {
    override fun onCreate() {
        super.onCreate()
        Net.install()
        Downloads.manager(this)
    }
}
