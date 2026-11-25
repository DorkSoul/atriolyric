from __future__ import annotations

import mutagen

from app.tag_writer import write_tags
from tests.conftest import make_audio_fixture, unwritable_audio


def test_write_tags_updates_all_fields(tmp_path):
    path = tmp_path / "song.wav"
    make_audio_fixture(path, title="Old Title", artist="Old Artist", album="Old Album", genre="Old Genre")

    ok = write_tags(path, title="New Title", artist="New Artist", album="New Album", genre="New Genre", track_number=7)
    assert ok is True

    audio = mutagen.File(path, easy=True)
    assert str(audio.tags.get("TIT2")) == "New Title"
    assert str(audio.tags.get("TPE1")) == "New Artist"
    assert str(audio.tags.get("TALB")) == "New Album"
    assert str(audio.tags.get("TCON")) == "New Genre"
    assert str(audio.tags.get("TRCK")) == "7"


def test_write_tags_only_touches_specified_fields(tmp_path):
    path = tmp_path / "song.wav"
    make_audio_fixture(path, title="Keep Title", artist="Keep Artist", album="Old Album", genre="Old Genre")

    write_tags(path, album="New Album")

    audio = mutagen.File(path, easy=True)
    assert str(audio.tags.get("TIT2")) == "Keep Title"
    assert str(audio.tags.get("TPE1")) == "Keep Artist"
    assert str(audio.tags.get("TALB")) == "New Album"


def test_write_tags_on_untagged_file_adds_tags(tmp_path):
    path = tmp_path / "song.wav"
    make_audio_fixture(path, title="X", artist=None, album=None, genre=None)

    ok = write_tags(path, artist="Fresh Artist")
    assert ok is True
    audio = mutagen.File(path, easy=True)
    assert str(audio.tags.get("TPE1")) == "Fresh Artist"


def test_write_tags_returns_false_for_readonly_file(tmp_path):
    # A read-only library mount should fail cleanly, not crash.
    path = tmp_path / "song.wav"
    make_audio_fixture(path, title="Title", artist="Artist", album="Album", genre="Genre")
    with unwritable_audio(path):
        assert write_tags(path, title="Should Not Land") is False

    audio = mutagen.File(path, easy=True)
    assert str(audio.tags.get("TIT2")) == "Title"


def test_write_tags_returns_false_for_missing_file(tmp_path):
    assert write_tags(tmp_path / "nope.wav", title="X") is False


def test_write_tags_noop_when_nothing_requested(tmp_path):
    path = tmp_path / "song.wav"
    make_audio_fixture(path, title="Title", artist="Artist", album="Album", genre="Genre")
    assert write_tags(path) is True
