package io.github.dorksoul.atriolyric

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class NetTest {
    @Test
    fun aWebPageIsNeverMedia() {
        assertFalse(Net.isMediaType("text/html"))
        assertFalse(Net.isMediaType("Text/HTML; charset=utf-8"))
        assertTrue(Net.isMediaType("audio/mpeg"))
        assertTrue(Net.isMediaType("video/mp4"))
        assertTrue(Net.isMediaType("application/octet-stream"))
        // No type at all is left to the player to judge.
        assertTrue(Net.isMediaType(null))
    }

    @Test
    fun artworkMustBeAnImage() {
        assertTrue(Net.isImageType("image/jpeg"))
        assertTrue(Net.isImageType(" IMAGE/webp"))
        assertFalse(Net.isImageType("text/html; charset=utf-8"))
        assertFalse(Net.isImageType(null))
    }

    @Test
    fun sameOriginComparesSchemeHostAndPort() {
        assertTrue(Net.sameOrigin("https://music.example.org/api/download/app/android", "https://music.example.org"))
        assertTrue(Net.sameOrigin("https://Music.Example.org:443/x", "https://music.example.org"))
        assertTrue(Net.sameOrigin("http://10.0.0.2/x", "http://10.0.0.2:80"))
        assertFalse(Net.sameOrigin("http://music.example.org/x", "https://music.example.org"))
        assertFalse(Net.sameOrigin("https://evil.example.org/x.apk", "https://music.example.org"))
        assertFalse(Net.sameOrigin("https://music.example.org:8443/x", "https://music.example.org"))
        assertFalse(Net.sameOrigin("https://music.example.org.evil.net/x", "https://music.example.org"))
        assertFalse(Net.sameOrigin("https://music.example.org/x", null))
        assertFalse(Net.sameOrigin("not a url", "https://music.example.org"))
    }
}
