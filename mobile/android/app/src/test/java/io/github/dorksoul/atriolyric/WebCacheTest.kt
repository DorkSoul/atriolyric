package io.github.dorksoul.atriolyric

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File

class WebCacheTest {
    @get:Rule
    val folder = TemporaryFolder()

    @Test
    fun pathsAreSortedIntoWhatTheCopyKeeps() {
        assertEquals(WebCache.Kind.PAGE, WebCache.kindOf("/"))
        assertEquals(WebCache.Kind.PAGE, WebCache.kindOf("/asset-list.json"))
        assertEquals(WebCache.Kind.ASSET, WebCache.kindOf("/assets/index-abc123.js"))
        assertEquals(WebCache.Kind.ART, WebCache.kindOf("/api/stream/42/art"))
        assertEquals(WebCache.Kind.ART, WebCache.kindOf("/api/stream/42/art/full"))
        assertEquals(WebCache.Kind.ART, WebCache.kindOf("/api/stream/42/thumbnail"))
        assertEquals(WebCache.Kind.ART, WebCache.kindOf("/api/art/album/7/thumb"))
        // The API, the socket and the app's own addresses are never kept.
        assertNull(WebCache.kindOf("/api/library/tracks"))
        assertNull(WebCache.kindOf("/api/stream/42"))
        assertNull(WebCache.kindOf("/ws/playback"))
        assertNull(WebCache.kindOf("/__atrio/download-art/42"))
    }

    private fun copy(dir: File, name: String, size: Int, age: Long): Pair<File, File> {
        val body = File(dir, name).apply { writeBytes(ByteArray(size)) }
        val meta = File(dir, "$name.json").apply {
            writeText("{}")
            setLastModified(System.currentTimeMillis() - age)
        }
        return body to meta
    }

    @Test
    fun pruneDropsTheLeastRecentlyUsedDownToThreeQuarters() {
        val dir = folder.newFolder("webapp")
        val oldest = copy(dir, "a", 400, 3_000_000)
        val middle = copy(dir, "b", 400, 2_000_000)
        val newest = copy(dir, "c", 300, 1_000_000)
        WebCache.prune(dir, 1000)
        assertFalse(oldest.first.exists())
        assertFalse(oldest.second.exists())
        assertTrue(middle.first.exists())
        assertTrue(newest.first.exists())
    }

    @Test
    fun pruneLeavesAFolderUnderItsLimitAloneButClearsOldTemporaryFiles() {
        val dir = folder.newFolder("webart")
        val kept = copy(dir, "a", 100, 5_000_000)
        val stale = File(dir, "body123.part").apply {
            writeBytes(ByteArray(10))
            setLastModified(System.currentTimeMillis() - 2 * 24 * 3600 * 1000L)
        }
        val fresh = File(dir, "body456.part").apply { writeBytes(ByteArray(10)) }
        WebCache.prune(dir, 1000)
        assertTrue(kept.first.exists())
        assertFalse(stale.exists())
        assertTrue(fresh.exists())
    }
}
