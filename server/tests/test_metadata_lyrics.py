from __future__ import annotations

import wave
from pathlib import Path

import pytest
from mutagen.id3 import USLT
from mutagen.wave import WAVE

from app.metadata_providers import lyrics


def _make_wav_with_lyrics(path: Path, text: str) -> None:
    with wave.open(str(path), "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\x00\x00" * 8000)
    audio = WAVE(str(path))
    audio.add_tags()
    audio.tags.add(USLT(encoding=3, lang="eng", desc="", text=text))
    audio.save()


def test_get_embedded_lyrics_reads_id3_uslt(tmp_path):
    path = tmp_path / "song.wav"
    _make_wav_with_lyrics(path, "Line one\nLine two")
    assert lyrics.get_embedded_lyrics(path) == "Line one\nLine two"


def test_get_embedded_lyrics_returns_none_when_untagged(tmp_path):
    path = tmp_path / "untagged.wav"
    with wave.open(str(path), "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\x00\x00" * 8000)
    assert lyrics.get_embedded_lyrics(path) is None


def test_get_embedded_lyrics_returns_none_for_unreadable_file(tmp_path):
    path = tmp_path / "garbage.wav"
    path.write_bytes(b"not a real wav file")
    assert lyrics.get_embedded_lyrics(path) is None


@pytest.mark.network
def test_fetch_lrclib_lyrics_real_api_finds_bohemian_rhapsody():
    result = lyrics.fetch_lrclib_lyrics(
        "Queen", "Bohemian Rhapsody", album="A Night At The Opera", duration_seconds=355
    )
    assert result is not None
    assert result["plain"] is not None
    assert "real life" in result["plain"].lower()


@pytest.mark.network
def test_fetch_lrclib_lyrics_synced_format_when_available():
    result = lyrics.fetch_lrclib_lyrics(
        "Queen", "Bohemian Rhapsody", album="A Night At The Opera", duration_seconds=355
    )
    assert result is not None
    if result["synced"]:
        assert result["synced"].startswith("[")  # LRC timestamp format


def test_fetch_lrclib_lyrics_has_nothing_for_empty_input():
    assert lyrics.fetch_lrclib_lyrics("", "") == {}


def test_fetch_lrclib_lyrics_returns_none_on_network_failure(monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("no network")

    monkeypatch.setattr("app.metadata_providers.lyrics.httpx.get", boom)
    assert lyrics.fetch_lrclib_lyrics("Queen", "Bohemian Rhapsody") is None


def test_normalize_lyrics_moves_lrc_plain_text_to_synced():
    from app.metadata_providers.lyrics import normalize_lyrics

    raw = "[ar:Someone]\n[ti:Song]\n[00:15.69] Why won't they talk to me\n[01:02:50]Second [00:30]line"
    plain, synced = normalize_lyrics(raw, None)
    assert synced == raw
    assert plain == "Why won't they talk to me\nSecond line"


def test_normalize_lyrics_leaves_plain_text_alone():
    from app.metadata_providers.lyrics import normalize_lyrics

    assert normalize_lyrics("Just words", None) == ("Just words", None)
    assert normalize_lyrics(None, None) == (None, None)


def test_normalize_lyrics_keeps_both_and_strips_timestamps_from_plain():
    from app.metadata_providers.lyrics import normalize_lyrics

    assert normalize_lyrics("Plain", "[00:01.00] Plain") == ("Plain", "[00:01.00] Plain")
    # A "plain" with stamps next to a real synced copy: plain is rebuilt.
    plain, synced = normalize_lyrics("[00:01.00] Plain", "[00:01.00] Plain")
    assert plain == "Plain" and synced == "[00:01.00] Plain"


def test_normalize_lyrics_untimed_synced_becomes_plain():
    from app.metadata_providers.lyrics import normalize_lyrics

    assert normalize_lyrics(None, "no stamps here") == ("no stamps here", None)
    assert normalize_lyrics("keep", "no stamps") == ("keep", None)


def test_normalize_lyrics_timestamps_only_gives_no_plain():
    from app.metadata_providers.lyrics import normalize_lyrics

    plain, synced = normalize_lyrics("[00:01.00]\n[00:02.00]", None)
    assert plain is None and synced == "[00:01.00]\n[00:02.00]"
