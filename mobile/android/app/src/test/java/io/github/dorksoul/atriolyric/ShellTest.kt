package io.github.dorksoul.atriolyric

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class ShellTest {
    @Test
    fun anAddressWithoutASchemeIsHttps() {
        assertEquals("https://music.example.org", Shell.normalise("music.example.org"))
        assertEquals("https://music.example.org", Shell.normalise("  music.example.org/  "))
        assertEquals("https://192.168.1.20:8030", Shell.normalise("192.168.1.20:8030"))
        assertEquals("https://localhost:8030", Shell.normalise("localhost:8030"))
    }

    @Test
    fun aGivenSchemeIsKept() {
        assertEquals("http://192.168.1.20:8030", Shell.normalise("http://192.168.1.20:8030"))
        assertEquals("https://music.example.org", Shell.normalise("https://music.example.org/"))
        assertEquals("http://nas.local:8030", Shell.normalise("HTTP://nas.local:8030"))
        assertEquals("https://music.example.org/sub", Shell.normalise("Https://music.example.org/sub"))
    }

    @Test
    fun anythingElseIsRefused() {
        assertNull(Shell.normalise(""))
        assertNull(Shell.normalise("   "))
        assertNull(Shell.normalise("ftp://music.example.org"))
        assertNull(Shell.normalise("https://"))
        assertNull(Shell.normalise("not an address"))
    }
}
