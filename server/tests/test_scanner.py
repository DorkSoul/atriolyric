from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from mutagen.id3 import TCON
from mutagen.wave import WAVE

from app.models import Album, LibraryRoot, MediaItem, TrackArtist
from app.scanner import (
    SCAN_COMMIT_BATCH_SIZE,
    _first_musicbrainz_id,
    _parse_release_date,
    read_audio_tags,
    scan_library_root,
)
from tests.conftest import make_audio_fixture


def test_scan_skips_symlinked_file_outside_root(db_session, library_dir, tmp_path):
    outside = tmp_path / "outside.wav"
    make_audio_fixture(outside, title="Secret", artist="A", album="X", genre="Rock")
    (library_dir / "linked.wav").symlink_to(outside)
    root = LibraryRoot(path=str(library_dir), display_name="Library")
    db_session.add(root)
    db_session.commit()

    result = scan_library_root(db_session, root)

    assert result["added"] == 0
    assert db_session.query(MediaItem).count() == 0


def _make_root(db_session, library_dir) -> LibraryRoot:
    root = LibraryRoot(path=str(library_dir), display_name="Test Library")
    db_session.add(root)
    db_session.commit()
    db_session.refresh(root)
    return root


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("2005", "2005-01-01"),
        ("2005-03", "2005-03-01"),
        ("2005-03-01", "2005-03-01"),
        ("2005-03-01T00:00:00Z", "2005-03-01"),
        (None, None),
        ("", None),
        ("unknown", None),
    ],
)
def test_parse_release_date(raw, expected):
    assert _parse_release_date(raw) == expected


def test_scan_reads_full_release_date_from_date_tag(db_session, library_dir):
    make_audio_fixture(
        library_dir / "track1.wav",
        title="Song One",
        artist="Artist A",
        album="Album X",
        genre="Rock",
        date="1999-05-20",
    )
    root = _make_root(db_session, library_dir)

    scan_library_root(db_session, root)

    item = db_session.query(MediaItem).one()
    assert item.release_date == "1999-05-20"


def test_scan_pads_a_year_only_date_tag_to_the_1st_of_january(db_session, library_dir):
    make_audio_fixture(
        library_dir / "track1.wav",
        title="Song One",
        artist="Artist A",
        album="Album X",
        genre="Rock",
        date=1999,
    )
    root = _make_root(db_session, library_dir)

    scan_library_root(db_session, root)

    item = db_session.query(MediaItem).one()
    assert item.release_date == "1999-01-01"


def test_scan_raises_on_nonexistent_root_path(db_session):
    root = LibraryRoot(path="/tmp/definitely-not-a-real-path-xyz", display_name="Ghost")
    db_session.add(root)
    db_session.commit()
    db_session.refresh(root)

    with pytest.raises(FileNotFoundError):
        scan_library_root(db_session, root)


def test_scan_reads_tags_into_media_items(db_session, library_dir):
    make_audio_fixture(
        library_dir / "track1.wav",
        title="Song One",
        artist="Artist A",
        album="Album X",
        genre="Rock",
        track_number=1,
    )
    root = _make_root(db_session, library_dir)

    result = scan_library_root(db_session, root)
    assert result == {"added": 1, "updated": 0, "removed": 0, "failed": 0, "art_redone": 0, "missing_held": 0, "unreadable_dirs": [], "stopped": False}

    items = db_session.query(MediaItem).all()
    assert len(items) == 1
    item = items[0]
    assert item.title == "Song One"
    assert item.artist == "Artist A"
    assert item.album == "Album X"
    assert item.genre == "Rock"
    assert item.track_number == 1
    assert item.duration_seconds == 1.0


def test_read_audio_tags_extracts_musicbrainz_ids_from_id3_frames(tmp_path):
    audio = SimpleNamespace(
        tags={
            "TXXX:MusicBrainz Track Id": SimpleNamespace(text=["recording-id"]),
            "TXXX:MusicBrainz Artist Id": SimpleNamespace(text=["artist-id"]),
        },
        info=SimpleNamespace(length=1.0),
    )
    with patch("app.scanner.mutagen.File", return_value=audio):
        tags = read_audio_tags(tmp_path / "track.mp3")

    assert tags["musicbrainz_recording_mbid"] == "recording-id"
    assert tags["musicbrainz_artist_mbid"] == "artist-id"


def test_read_audio_tags_extracts_easy_musicbrainz_ids(tmp_path):
    audio = SimpleNamespace(
        tags={
            "musicbrainz_trackid": ["easy-recording-id"],
            "musicbrainz_artistid": ["easy-artist-id"],
            "isrc": ["gb-abc-1234567"],
        },
        info=SimpleNamespace(length=1.0),
    )
    with patch("app.scanner.mutagen.File", return_value=audio):
        tags = read_audio_tags(tmp_path / "track.flac")

    assert tags["musicbrainz_recording_mbid"] == "easy-recording-id"
    assert tags["musicbrainz_artist_mbid"] == "easy-artist-id"
    assert tags["musicbrainz_isrc"] == "gb-abc-1234567"


def test_read_audio_tags_extracts_album_artist_from_easy_and_id3_tags(tmp_path):
    audio = SimpleNamespace(
        tags={"albumartist": ["Album Credit"]},
        info=SimpleNamespace(length=1.0),
    )
    with patch("app.scanner.mutagen.File", return_value=audio):
        assert read_audio_tags(tmp_path / "track.flac")["album_artist"] == "Album Credit"

    audio.tags = {"TPE2": SimpleNamespace(text=["ID3 Album Credit"])}
    with patch("app.scanner.mutagen.File", return_value=audio):
        assert read_audio_tags(tmp_path / "track.mp3")["album_artist"] == "ID3 Album Credit"


def test_scan_backfills_album_artist_for_existing_unchanged_files(db_session, library_dir):
    make_audio_fixture(
        library_dir / "track.wav", title="Track", artist="Track Credit", album="Album", genre="Rock"
    )
    root = _make_root(db_session, library_dir)
    scan_library_root(db_session, root)
    item = db_session.query(MediaItem).one()
    item.album_artist_scanned_at = None  # Represents a row from before this field existed.
    db_session.commit()

    tags = {
        "kind": "track",
        "title": "Track",
        "artist": "Track Credit",
        "album_artist": "Album Credit",
        "album": "Album",
        "genre": "Rock",
        "genres": ["Rock"],
        "track_number": 1,
        "duration_seconds": 1.0,
        "release_date": None,
        "musicbrainz_recording_mbid": None,
        "musicbrainz_artist_mbid": None,
        "musicbrainz_isrc": None,
    }
    with patch("app.scanner.read_audio_tags", return_value=tags):
        result = scan_library_root(db_session, root)

    db_session.refresh(item)
    assert result["updated"] == 1
    assert item.album_artist == "Album Credit"
    assert item.album_artist_scanned_at is not None


def test_musicbrainz_id_reader_handles_ufid_and_invalid_data():
    ufid = {"UFID:http://musicbrainz.org": SimpleNamespace(data=b"ufid-id")}
    assert _first_musicbrainz_id(ufid, "musicbrainz_trackid", "Track") == "ufid-id"
    assert _first_musicbrainz_id({}, "musicbrainz_trackid", "Track") is None
    invalid = {"UFID:http://musicbrainz.org": SimpleNamespace(data="not-bytes")}
    assert _first_musicbrainz_id(invalid, "musicbrainz_trackid", "Track") is None


def test_rescan_updates_musicbrainz_ids_from_changed_tags(db_session, library_dir):
    import os

    path = library_dir / "track.wav"
    make_audio_fixture(path, title="Track", artist="Artist", album="Album", genre="Rock")
    root = _make_root(db_session, library_dir)
    scan_library_root(db_session, root)
    os.utime(path, (path.stat().st_atime, path.stat().st_mtime + 1))

    tags = {
        "title": "Track",
        "artist": "Artist",
        "album": "Album",
        "genre": "Rock",
        "genres": ["Rock"],
        "track_number": 1,
        "duration_seconds": 1.0,
        "release_date": None,
        "musicbrainz_recording_mbid": "recording-id",
        "musicbrainz_artist_mbid": "artist-id",
        "musicbrainz_isrc": "GBABC1234567",
    }
    with patch("app.scanner.read_audio_tags", return_value=tags):
        scan_library_root(db_session, root)

    item = db_session.query(MediaItem).one()
    assert item.musicbrainz_recording_mbid == "recording-id"
    assert item.musicbrainz_artist_mbid == "artist-id"
    assert item.musicbrainz_isrc == "GBABC1234567"
    assert item.musicbrainz_isrc_scanned_at is not None


def test_isrc_backfill_keeps_completed_metadata_lookup_state(db_session, library_dir):
    path = library_dir / "track.wav"
    make_audio_fixture(path, title="Track", artist="Artist", album="Album", genre="Rock")
    root = _make_root(db_session, library_dir)
    scan_library_root(db_session, root)
    item = db_session.query(MediaItem).one()
    item.musicbrainz_metadata_json = {}
    item.musicbrainz_lookup_status = "complete"
    item.musicbrainz_isrc_scanned_at = None
    db_session.commit()

    scan_library_root(db_session, root)

    db_session.refresh(item)
    assert item.musicbrainz_lookup_status == "complete"
    assert item.musicbrainz_isrc_scanned_at is not None


def test_scan_retains_every_embedded_genre_value(db_session, library_dir):
    path = library_dir / "multi-genre.wav"
    make_audio_fixture(
        path, title="Multi", artist="Artist", album="Album", genre="Rock"
    )
    audio = WAVE(str(path))
    audio.tags.setall("TCON", [TCON(encoding=3, text=["Rock", "Alternative Rock"])])
    audio.save()
    root = _make_root(db_session, library_dir)

    scan_library_root(db_session, root)

    item = db_session.query(MediaItem).one()
    assert item.genre == "Rock"
    assert {genre.name for genre in item.genres} == {"Rock", "Alternative Rock"}


def test_scan_falls_back_to_filename_when_no_title_tag(db_session, library_dir):
    (library_dir / "untagged.wav").write_bytes(b"")
    # Not a valid WAV, but scanner should skip unreadable tags gracefully
    # and still record the file with the filename as title.
    root = _make_root(db_session, library_dir)
    scan_library_root(db_session, root)

    item = db_session.query(MediaItem).one()
    assert item.title == "untagged"


def test_rescan_is_idempotent_for_unchanged_files(db_session, library_dir):
    make_audio_fixture(
        library_dir / "track1.wav",
        title="Song One",
        artist="A",
        album="X",
        genre="Rock",
    )
    root = _make_root(db_session, library_dir)

    first = scan_library_root(db_session, root)
    second = scan_library_root(db_session, root)

    assert first == {"added": 1, "updated": 0, "removed": 0, "failed": 0, "art_redone": 0, "missing_held": 0, "unreadable_dirs": [], "stopped": False}
    assert second == {"added": 0, "updated": 0, "removed": 0, "failed": 0, "art_redone": 0, "missing_held": 0, "unreadable_dirs": [], "stopped": False}
    assert db_session.query(MediaItem).count() == 1


def test_rescan_reports_progress_for_an_unchanged_file(db_session, library_dir):
    make_audio_fixture(
        library_dir / "track1.wav", title="Song One", artist="A", album="X", genre="Rock"
    )
    root = _make_root(db_session, library_dir)
    scan_library_root(db_session, root)
    progress: list[tuple[int, int]] = []

    scan_library_root(db_session, root, lambda processed, total: progress.append((processed, total)))

    assert progress == [(0, 1), (1, 1)]


def test_scan_removes_deleted_files(db_session, library_dir):
    path = library_dir / "track1.wav"
    make_audio_fixture(path, title="Song One", artist="A", album="X", genre="Rock")
    # A second file stays: a scan that finds no files at all removes nothing.
    make_audio_fixture(library_dir / "track2.wav", title="Song Two", artist="A", album="X", genre="Rock")
    root = _make_root(db_session, library_dir)
    scan_library_root(db_session, root)
    assert db_session.query(MediaItem).count() == 2

    path.unlink()
    result = scan_library_root(db_session, root)

    assert result == {"added": 0, "updated": 0, "removed": 1, "failed": 0, "art_redone": 0, "missing_held": 0, "unreadable_dirs": [], "stopped": False}
    assert db_session.query(MediaItem).count() == 1


def test_scan_detects_tag_changes_via_mtime(db_session, library_dir):
    path = library_dir / "track1.wav"
    make_audio_fixture(path, title="Original", artist="A", album="X", genre="Rock")
    root = _make_root(db_session, library_dir)
    scan_library_root(db_session, root)

    import os

    scanned_mtime_ns = path.stat().st_mtime_ns
    make_audio_fixture(path, title="Renamed", artist="A", album="X", genre="Rock")
    # The modification time moves on by a second, whatever the clock or the
    # filesystem's timestamp resolution did meanwhile.
    os.utime(path, ns=(path.stat().st_atime_ns, scanned_mtime_ns + 1_000_000_000))

    result = scan_library_root(db_session, root)
    assert result == {"added": 0, "updated": 1, "removed": 0, "failed": 0, "art_redone": 1, "missing_held": 0, "unreadable_dirs": [], "stopped": False}
    assert db_session.query(MediaItem).one().title == "Renamed"


def test_scan_commits_in_batches_not_just_once_at_the_end(db_session, library_dir):
    # A scan holding one uncommitted transaction open for its entire
    # duration locks out every other request touching the database for
    # just as long - see app.db and SCAN_COMMIT_BATCH_SIZE.
    track_count = SCAN_COMMIT_BATCH_SIZE + 5
    for i in range(track_count):
        make_audio_fixture(
            library_dir / f"t{i}.wav", title=f"T{i}", artist="Band", album="Album", genre="Rock"
        )
    root = _make_root(db_session, library_dir)

    with patch.object(db_session, "commit", wraps=db_session.commit) as commit_spy:
        result = scan_library_root(db_session, root)

    assert result == {"added": track_count, "updated": 0, "removed": 0, "failed": 0, "art_redone": 0, "missing_held": 0, "unreadable_dirs": [], "stopped": False}
    assert commit_spy.call_count > 1


def _rewrite_in_place(path, **tags):
    """Rewrite a fixture's tags, then put the old mtime back (an editor
    that preserves timestamps)."""
    import os

    before = path.stat()
    make_audio_fixture(path, **tags)
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))


def test_scan_records_file_size(db_session, library_dir):
    path = library_dir / "track1.wav"
    make_audio_fixture(path, title="One", artist="A", album="X", genre="Rock")
    scan_library_root(db_session, _make_root(db_session, library_dir))
    assert db_session.query(MediaItem).one().file_size == path.stat().st_size


def test_rescan_detects_size_change_with_same_mtime(db_session, library_dir):
    path = library_dir / "track1.wav"
    make_audio_fixture(path, title="One", artist="A", album="X", genre="Rock")
    root = _make_root(db_session, library_dir)
    scan_library_root(db_session, root)

    _rewrite_in_place(
        path, title="One with a much longer title", artist="A", album="X", genre="Rock"
    )

    result = scan_library_root(db_session, root)
    assert result["updated"] == 1
    assert db_session.query(MediaItem).one().title == "One with a much longer title"


def test_rescan_fills_missing_file_size_without_rereading(db_session, library_dir):
    path = library_dir / "track1.wav"
    make_audio_fixture(path, title="One", artist="A", album="X", genre="Rock")
    root = _make_root(db_session, library_dir)
    scan_library_root(db_session, root)
    item = db_session.query(MediaItem).one()
    item.file_size = None
    db_session.commit()

    assert scan_library_root(db_session, root)["updated"] == 0
    db_session.refresh(item)
    assert item.file_size == path.stat().st_size


def test_metadata_rescan_rereads_tags_but_skips_art_and_loudness(db_session, library_dir):
    path = library_dir / "track1.wav"
    make_audio_fixture(path, title="Old", artist="A", album="X", genre="Rock")
    root = _make_root(db_session, library_dir)
    scan_library_root(db_session, root)
    item = db_session.query(MediaItem).one()
    item.replaygain_track_gain = -7.5  # an ffmpeg measurement
    before = path.stat()
    item.file_size = before.st_size  # unchanged on purpose
    db_session.commit()
    # Same size and mtime: only a metadata rescan can notice this.
    make_audio_fixture(path, title="New", artist="A", album="X", genre="Rock")
    import os

    # Restore the exact nanosecond mtime: rebuilding it from the float
    # (int(st_mtime * 1e9)) can land a nanosecond off, which reads as a
    # changed file.
    os.utime(path, ns=(before.st_mtime_ns, before.st_mtime_ns))

    assert scan_library_root(db_session, root)["updated"] == 0
    with patch("app.scanner.ensure_art_thumb_cached") as thumb, patch(
        "app.scanner.delete_media_cache"
    ) as cache:
        progress: list[tuple[int, int]] = []
        result = scan_library_root(
            db_session, root, lambda done, total: progress.append((done, total)), mode="metadata"
        )
    assert progress[-1] == (1, 1)
    assert result["updated"] == 1
    thumb.assert_not_called()
    cache.assert_not_called()
    db_session.refresh(item)
    assert item.title == "New"
    assert item.replaygain_track_gain == -7.5


def test_metadata_rescan_takes_gain_and_embedded_lyrics_from_tags(db_session, library_dir):
    path = library_dir / "track1.wav"
    make_audio_fixture(path, title="Song", artist="A", album="X", genre="Rock")
    root = _make_root(db_session, library_dir)
    scan_library_root(db_session, root)

    with patch("app.scanner.get_embedded_lyrics", return_value="la la la"), patch(
        "app.scanner.read_audio_tags",
        return_value={
            "title": "Song",
            "replaygain_track_gain": -3.0,
            "replaygain_album_gain": -4.0,
            "kind": "track",
        },
    ):
        scan_library_root(db_session, root, mode="metadata")
    item = db_session.query(MediaItem).one()
    assert (item.replaygain_track_gain, item.replaygain_album_gain) == (-3.0, -4.0)
    assert item.lyrics_plain == "la la la"
    assert item.loudness_scanned_at is not None


def test_full_rescan_rebuilds_everything_for_unchanged_files(db_session, library_dir):
    make_audio_fixture(
        library_dir / "track1.wav", title="Song", artist="A", album="X", genre="Rock"
    )
    root = _make_root(db_session, library_dir)
    scan_library_root(db_session, root)

    with patch("app.scanner.ensure_art_thumb_cached") as thumb, patch(
        "app.scanner.delete_media_cache"
    ) as cache:
        result = scan_library_root(db_session, root, mode="full")
    assert result["updated"] == 1
    thumb.assert_called_once()
    cache.assert_called_once()


def test_scan_rejects_unknown_mode(db_session, library_dir):
    with pytest.raises(ValueError):
        scan_library_root(db_session, _make_root(db_session, library_dir), mode="bogus")


def test_vorbis_files_without_lyrics_dont_break_the_lyrics_lookup(tmp_path):
    from unittest.mock import MagicMock

    from app.metadata_providers.lyrics import get_embedded_lyrics

    class VorbisLike(dict):
        def get(self, key, default=None):
            if not key.isascii():
                raise ValueError
            return super().get(key, default)

    audio = MagicMock()
    audio.tags = VorbisLike()
    with patch("app.metadata_providers.lyrics.mutagen.File", return_value=audio):
        assert get_embedded_lyrics(tmp_path / "x.flac") is None
    audio.tags = VorbisLike(UNSYNCEDLYRICS=["la la"])
    with patch("app.metadata_providers.lyrics.mutagen.File", return_value=audio):
        assert get_embedded_lyrics(tmp_path / "x.flac") == "la la"


def test_one_unreadable_file_is_skipped_not_fatal(db_session, library_dir):
    make_audio_fixture(library_dir / "good.wav", title="Good", artist="A", album="X", genre=None)
    make_audio_fixture(library_dir / "bad.wav", title="Bad", artist="A", album="X", genre=None)
    root = _make_root(db_session, library_dir)
    real = __import__("app.scanner", fromlist=["get_embedded_lyrics"]).get_embedded_lyrics

    def flaky(path):
        if path.name == "bad.wav":
            raise ValueError
        return real(path)

    with patch("app.scanner.get_embedded_lyrics", side_effect=flaky):
        result = scan_library_root(db_session, root)
    assert result["failed"] == 1
    assert {i.title for i in db_session.query(MediaItem)} >= {"Good"}


def test_a_failed_file_that_created_the_artist_and_album_does_not_fail_the_next(
    db_session, library_dir
):
    """The first file of a new artist creates the Artist and Album rows. If
    that file then fails, its rollback removes them, and the next file by
    the same artist must create them again rather than link to the
    rolled-back rows (a foreign-key failure, which would skip it too)."""
    make_audio_fixture(library_dir / "one.wav", title="One", artist="A", album="X", genre=None)
    make_audio_fixture(library_dir / "two.wav", title="Two", artist="A", album="X", genre=None)
    root = _make_root(db_session, library_dir)
    real = __import__("app.scanner", fromlist=["assign_album"]).assign_album
    failed_names: list[str] = []

    def fail_first(db, item, cache=None):
        # Whichever file the walk reaches first fails, after its credits and
        # album were written - so the order on disk does not matter.
        album = real(db, item, cache)
        if not failed_names:
            failed_names.append(item.relative_path)
            db.flush()
            raise ValueError
        return album

    with patch("app.scanner.assign_album", side_effect=fail_first):
        result = scan_library_root(db_session, root)

    assert result["failed"] == 1
    items = db_session.query(MediaItem).all()
    assert len(items) == 1
    good = items[0]
    assert good.relative_path != failed_names[0]
    credits = db_session.query(TrackArtist).filter_by(media_item_id=good.id).all()
    # The album owner falls back to the track artist, so A is credited twice.
    assert sorted((c.artist.name, c.role) for c in credits) == [
        ("A", "album_artist"),
        ("A", "main"),
    ]
    assert good.album_id is not None
    assert db_session.get(Album, good.album_id).title == "X"


# --- Removal safety ---------------------------------------------------------
# An unmounted bind mount is an existing, empty folder, and an unreadable
# folder lists as empty. Neither may delete rows (and with them every user's
# favourites, playlist entries and history) without an explicit confirmation.


def _populate(library_dir, count: int, folder: str = "") -> list:
    """`count` tagged WAVs under `folder`, copied from one generated fixture."""
    import shutil

    target = library_dir / folder if folder else library_dir
    target.mkdir(parents=True, exist_ok=True)
    first = target / "t0.wav"
    make_audio_fixture(first, title="T0", artist="Band", album="Album", genre="Rock")
    paths = [first]
    for i in range(1, count):
        path = target / f"t{i}.wav"
        shutil.copy(first, path)
        paths.append(path)
    return paths


def _block_folder_listing(monkeypatch, blocked_name: str) -> None:
    """Make os.walk report `blocked_name` as unreadable, the way it does for
    a folder it gets EACCES on (chmod is no test when running as root)."""
    import os

    real_walk = os.walk

    def walk(top, *args, onerror=None, **kwargs):
        for dirpath, dirnames, filenames in real_walk(top, *args, onerror=onerror, **kwargs):
            if blocked_name in dirnames:
                dirnames.remove(blocked_name)
                onerror(PermissionError(13, "Permission denied", os.path.join(dirpath, blocked_name)))
            yield dirpath, dirnames, filenames

    monkeypatch.setattr("app.scanner.os.walk", walk)


def test_empty_root_after_a_populated_scan_removes_nothing(db_session, library_dir, caplog):
    from app.models import Favorite, PlayHistory, Playlist, PlaylistItem, User

    paths = _populate(library_dir, 3)
    root = _make_root(db_session, library_dir)
    scan_library_root(db_session, root)
    user = db_session.query(User).one()
    track = db_session.query(MediaItem).first()
    playlist = Playlist(user_id=user.id, name="Mine")
    db_session.add(playlist)
    db_session.flush()
    db_session.add_all([
        Favorite(user_id=user.id, media_item_id=track.id),
        PlaylistItem(playlist_id=playlist.id, media_item_id=track.id, position=0),
        PlayHistory(user_id=user.id, media_item_id=track.id),
    ])
    db_session.commit()

    for path in paths:  # what an unmounted share looks like
        path.unlink()
    with caplog.at_level("WARNING", logger="app.scanner"):
        result = scan_library_root(db_session, root)

    assert result["removed"] == 0
    assert result["missing_held"] == 3
    assert result["unreadable_dirs"] == []
    assert db_session.query(MediaItem).count() == 3
    assert db_session.query(Favorite).count() == 1
    assert db_session.query(PlaylistItem).count() == 1
    assert db_session.query(PlayHistory).count() == 1
    assert "not removing 3 missing track(s)" in caplog.text
    assert str(library_dir) in caplog.text


def test_unreadable_folder_keeps_its_rows_and_holds_other_removals(
    db_session, library_dir, monkeypatch
):
    _populate(library_dir, 2, "Locked")
    other = _populate(library_dir, 10, "Open")
    root = _make_root(db_session, library_dir)
    scan_library_root(db_session, root)
    other[0].unlink()  # a genuine removal elsewhere is held too

    _block_folder_listing(monkeypatch, "Locked")
    result = scan_library_root(db_session, root)

    assert result["unreadable_dirs"] == ["Locked"]
    assert result["missing_held"] == 3
    assert result["removed"] == 0
    assert db_session.query(MediaItem).count() == 12


@pytest.mark.skipif(
    __import__("os").geteuid() == 0, reason="root can read a chmod 000 folder"
)
def test_a_chmod_000_folder_is_reported_not_dropped(db_session, library_dir):
    _populate(library_dir, 2, "Locked")
    _populate(library_dir, 3, "Open")
    root = _make_root(db_session, library_dir)
    scan_library_root(db_session, root)
    locked = library_dir / "Locked"
    locked.chmod(0)
    try:
        result = scan_library_root(db_session, root)
    finally:
        locked.chmod(0o755)

    assert result["unreadable_dirs"] == ["Locked"]
    assert result["missing_held"] == 2
    assert db_session.query(MediaItem).count() == 5


def test_a_file_that_cannot_be_resolved_holds_removals(db_session, library_dir, monkeypatch):
    from pathlib import Path

    paths = _populate(library_dir, 10)
    root = _make_root(db_session, library_dir)
    scan_library_root(db_session, root)
    paths[0].unlink()
    real_resolve = Path.resolve

    def resolve(self, strict=False):
        if self.name == "t1.wav":
            raise OSError("I/O error")
        return real_resolve(self, strict=strict)

    monkeypatch.setattr("app.scanner.Path.resolve", resolve)
    result = scan_library_root(db_session, root)

    # t0 is gone and t1 unreadable: neither row may be removed on this evidence.
    assert result["missing_held"] == 2
    assert result["removed"] == 0
    assert db_session.query(MediaItem).count() == 10


def test_a_small_genuine_removal_still_happens(db_session, library_dir):
    paths = _populate(library_dir, 10)
    root = _make_root(db_session, library_dir)
    scan_library_root(db_session, root)
    paths[0].unlink()

    result = scan_library_root(db_session, root)

    assert result["removed"] == 1
    assert result["missing_held"] == 0
    assert result["unreadable_dirs"] == []
    assert db_session.query(MediaItem).count() == 9


def _add_missing_rows(db_session, root, count: int) -> None:
    """Rows whose files are not on disk (cheaper than writing and deleting them)."""
    from app.models import MediaKind

    db_session.add_all(
        MediaItem(
            library_root_id=root.id, kind=MediaKind.track,
            relative_path=f"gone/{i}.wav", title=f"Gone {i}",
        )
        for i in range(count)
    )
    db_session.commit()


@pytest.mark.parametrize(
    "present,missing,held",
    [
        (79, 21, True),  # 21 of 100: over 20% and over 20 rows
        (84, 21, False),  # 21 of 105: exactly 20%
        (20, 20, False),  # 20 of 40: half, but not more than 20 rows
    ],
)
def test_large_removals_are_held(db_session, library_dir, present, missing, held):
    _populate(library_dir, present)
    root = _make_root(db_session, library_dir)
    scan_library_root(db_session, root)
    _add_missing_rows(db_session, root, missing)

    result = scan_library_root(db_session, root)

    assert result["missing_held"] == (missing if held else 0)
    assert result["removed"] == (0 if held else missing)
    assert db_session.query(MediaItem).count() == present + (missing if held else 0)


def test_confirm_removals_removes_what_was_held(db_session, library_dir):
    paths = _populate(library_dir, 3)
    root = _make_root(db_session, library_dir)
    scan_library_root(db_session, root)
    for path in paths:
        path.unlink()
    assert scan_library_root(db_session, root)["missing_held"] == 3

    result = scan_library_root(db_session, root, mode="confirm_removals")

    assert result["removed"] == 3
    assert result["missing_held"] == 0
    assert db_session.query(MediaItem).count() == 0


def test_confirm_removals_skips_unchanged_files_like_a_normal_scan(db_session, library_dir):
    _populate(library_dir, 2)
    root = _make_root(db_session, library_dir)
    scan_library_root(db_session, root)

    result = scan_library_root(db_session, root, mode="confirm_removals")

    assert (result["added"], result["updated"], result["removed"]) == (0, 0, 0)


def test_confirm_removals_still_holds_when_a_folder_is_unreadable(
    db_session, library_dir, monkeypatch
):
    _populate(library_dir, 2, "Locked")
    _populate(library_dir, 2, "Open")
    root = _make_root(db_session, library_dir)
    scan_library_root(db_session, root)

    _block_folder_listing(monkeypatch, "Locked")
    result = scan_library_root(db_session, root, mode="confirm_removals")

    assert result["missing_held"] == 2
    assert result["removed"] == 0
    assert db_session.query(MediaItem).count() == 4


def test_confirm_removals_still_refuses_a_missing_root(db_session):
    root = LibraryRoot(path="/tmp/definitely-not-a-real-path-xyz", display_name="Ghost")
    db_session.add(root)
    db_session.commit()

    with pytest.raises(FileNotFoundError):
        scan_library_root(db_session, root, mode="confirm_removals")


def test_unreadable_dirs_are_capped(db_session, library_dir, monkeypatch):
    import os

    from app.scanner import UNREADABLE_DIRS_REPORTED

    root = _make_root(db_session, library_dir)

    def walk(top, *args, onerror=None, **kwargs):
        for i in range(UNREADABLE_DIRS_REPORTED + 5):
            onerror(PermissionError(13, "Permission denied", os.path.join(top, f"d{i}")))
        return iter(())

    monkeypatch.setattr("app.scanner.os.walk", walk)
    result = scan_library_root(db_session, root)

    assert len(result["unreadable_dirs"]) == UNREADABLE_DIRS_REPORTED


def test_a_dangling_symlink_is_skipped_not_counted_as_unreadable(db_session, library_dir):
    paths = _populate(library_dir, 10)
    (library_dir / "dangling.wav").symlink_to(library_dir / "nowhere.wav")
    root = _make_root(db_session, library_dir)
    scan_library_root(db_session, root)
    paths[0].unlink()

    result = scan_library_root(db_session, root)

    assert result["removed"] == 1
    assert result["unreadable_dirs"] == []
    assert db_session.query(MediaItem).count() == 9


def test_embedded_lyrics_are_read_before_the_write_transaction_opens(db_session, library_dir):
    """The lyrics come from a second tag read on the library mount; doing it
    while the file's rows are flushed would hold SQLite's writer lock across
    that disk I/O."""
    make_audio_fixture(library_dir / "new.wav", title="New", artist="A", album="X", genre="Rock")
    make_audio_fixture(library_dir / "old.wav", title="Old", artist="A", album="X", genre="Rock")
    root = _make_root(db_session, library_dir)
    with patch("app.scanner.get_embedded_lyrics", return_value=None):
        scan_library_root(db_session, root)
    db_session.query(MediaItem).filter_by(relative_path="old.wav").update({"file_mtime": 0})
    db_session.commit()
    (library_dir / "old.wav").touch()
    make_audio_fixture(library_dir / "added.wav", title="Added", artist="B", album="Y", genre=None)
    write_open: list[bool] = []

    def lyrics(path):
        dbapi = db_session.connection().connection.dbapi_connection
        write_open.append(dbapi.in_transaction)
        return "la la"

    with patch("app.scanner.get_embedded_lyrics", side_effect=lyrics):
        result = scan_library_root(db_session, root)

    assert result["added"] == 1 and result["updated"] == 1
    # One read per changed file (the new one and the touched one), and no
    # write was pending at either.
    assert write_open == [False, False]
    assert {i.lyrics_plain for i in db_session.query(MediaItem).filter(MediaItem.title != "New")} == {"la la"}


def test_a_stopped_scan_keeps_every_row_and_removes_nothing(db_session, library_dir):
    paths = _populate(library_dir, 6)
    root = _make_root(db_session, library_dir)
    scan_library_root(db_session, root)
    for path in paths[:4]:  # most of the library is gone: a full scan would remove it
        path.unlink()
    for name in ("n1.wav", "n2.wav", "n3.wav"):
        make_audio_fixture(library_dir / name, title=name, artist="New", album="New", genre=None)
    reads: list[str] = []

    def lyrics(path):
        reads.append(path.name)
        return None

    with patch("app.scanner.get_embedded_lyrics", side_effect=lyrics):
        result = scan_library_root(
            db_session, root, mode="confirm_removals", should_stop=lambda: len(reads) >= 2
        )

    assert result["stopped"] is True
    assert result["added"] == 2
    # A partial walk is no evidence of deletion: nothing removed, nothing held.
    assert result["removed"] == 0
    assert result["missing_held"] == 0
    assert db_session.query(MediaItem).count() == 6 + 2


def test_a_scan_stopped_during_the_walk_indexes_nothing(db_session, library_dir):
    _populate(library_dir, 2)
    root = _make_root(db_session, library_dir)

    result = scan_library_root(db_session, root, should_stop=lambda: True)

    assert result == {
        "added": 0, "updated": 0, "removed": 0, "failed": 0, "art_redone": 0, "missing_held": 0,
        "unreadable_dirs": [], "stopped": True,
    }
    assert db_session.query(MediaItem).count() == 0
    # Not stopping is the ordinary scan.
    assert scan_library_root(db_session, root, should_stop=lambda: False)["stopped"] is False


# --- Moved and renamed files keep their row ---------------------------------
# A track's row carries every user's favourites, playlist entries and play
# history, so a renamed folder must not become "removed" plus "added".


def _liked(db_session, items) -> None:
    from app.models import Favorite, PlayHistory, User

    user = db_session.query(User).one()
    for item in items:
        db_session.add(Favorite(user_id=user.id, media_item_id=item.id))
        db_session.add(PlayHistory(user_id=user.id, media_item_id=item.id))
    db_session.commit()


def _user_data_ids(db_session) -> tuple[set[int], set[int]]:
    from app.models import Favorite, PlayHistory

    return (
        {row.media_item_id for row in db_session.query(Favorite)},
        {row.media_item_id for row in db_session.query(PlayHistory)},
    )


def test_a_renamed_album_folder_keeps_its_rows_and_every_users_data(db_session, library_dir):
    _populate(library_dir, 12, "Band/Album")
    make_audio_fixture(library_dir / "other.wav", title="Other", artist="B", album="Y", genre="Pop")
    root = _make_root(db_session, library_dir)
    scan_library_root(db_session, root)
    before = {
        item.relative_path: item.id
        for item in db_session.query(MediaItem).filter(MediaItem.relative_path.like("Band/%"))
    }
    assert len(before) == 12
    _liked(db_session, db_session.query(MediaItem).filter(MediaItem.id.in_(before.values())).all())

    (library_dir / "Band" / "Album").rename(library_dir / "Band" / "Album (2020)")
    result = scan_library_root(db_session, root)

    assert (result["added"], result["removed"], result["missing_held"]) == (0, 0, 0)
    db_session.expire_all()
    after = {
        item.relative_path: item.id
        for item in db_session.query(MediaItem).filter(MediaItem.relative_path.like("Band/%"))
    }
    assert after == {
        path.replace("Band/Album/", "Band/Album (2020)/"): item_id for path, item_id in before.items()
    }
    assert db_session.query(MediaItem).count() == 13
    favourites, plays = _user_data_ids(db_session)
    assert favourites == plays == set(before.values())
    # A third scan sees nothing left to do.
    assert scan_library_root(db_session, root)["updated"] == 0


def test_moving_a_whole_library_into_a_subfolder_is_not_held_or_doubled(db_session, library_dir):
    paths = _populate(library_dir, 30)
    root = _make_root(db_session, library_dir)
    scan_library_root(db_session, root)
    ids = {item.id for item in db_session.query(MediaItem)}
    (library_dir / "Music").mkdir()
    for path in paths:
        path.rename(library_dir / "Music" / path.name)

    result = scan_library_root(db_session, root)

    assert (result["added"], result["removed"], result["missing_held"]) == (0, 0, 0)
    db_session.expire_all()
    assert {item.id for item in db_session.query(MediaItem)} == ids
    assert all(item.relative_path.startswith("Music/") for item in db_session.query(MediaItem))


def test_a_file_renamed_and_retagged_at_once_is_still_the_same_track(db_session, library_dir):
    old = library_dir / "01 song.wav"
    make_audio_fixture(old, title="Song", artist="Band", album="Album", genre="Rock")
    make_audio_fixture(library_dir / "keep.wav", title="Keep", artist="Band", album="Album", genre="Rock")
    root = _make_root(db_session, library_dir)
    scan_library_root(db_session, root)
    item_id = db_session.query(MediaItem).filter_by(title="Song").one().id

    old.unlink()
    # A tagger that renames as it saves: new name, new bytes, same song.
    make_audio_fixture(library_dir / "Band - Song.wav", title="Song", artist="Band", album="Album", genre="Indie Rock")
    result = scan_library_root(db_session, root)

    assert (result["added"], result["removed"]) == (0, 0)
    db_session.expire_all()
    item = db_session.get(MediaItem, item_id)
    assert item.relative_path == "Band - Song.wav"
    assert item.genre == "Indie Rock"


def test_a_copy_is_a_new_track_and_a_different_song_is_not_taken_for_a_move(db_session, library_dir):
    import shutil

    first = library_dir / "a.wav"
    make_audio_fixture(first, title="A", artist="Band", album="Album", genre="Rock")
    gone = library_dir / "gone.wav"
    make_audio_fixture(gone, title="Gone", artist="Band", album="Album", genre="Rock")
    root = _make_root(db_session, library_dir)
    scan_library_root(db_session, root)
    gone_id = db_session.query(MediaItem).filter_by(title="Gone").one().id

    shutil.copy2(first, library_dir / "a copy.wav")  # the original is still there
    gone.unlink()
    make_audio_fixture(library_dir / "new.wav", title="Brand New", artist="Other", album="Else", genre="Pop")
    result = scan_library_root(db_session, root)

    assert (result["added"], result["removed"]) == (2, 1)
    db_session.expire_all()
    assert db_session.get(MediaItem, gone_id) is None
    assert db_session.query(MediaItem).filter_by(title="A").count() == 2


def test_a_moved_file_keeps_its_loudness_measurement(db_session, library_dir):
    path = library_dir / "t.wav"
    make_audio_fixture(path, title="T", artist="Band", album="Album", genre="Rock")
    make_audio_fixture(library_dir / "keep.wav", title="Keep", artist="Band", album="Album", genre="Rock")
    root = _make_root(db_session, library_dir)
    scan_library_root(db_session, root)
    item = db_session.query(MediaItem).filter_by(title="T").one()
    item.replaygain_track_gain = -7.5
    item.loudness_scanned_at = item.created_at
    db_session.commit()
    item_id = item.id

    (library_dir / "sub").mkdir()
    path.rename(library_dir / "sub" / "t.wav")
    scan_library_root(db_session, root)

    db_session.expire_all()
    moved = db_session.get(MediaItem, item_id)
    assert moved.relative_path == "sub/t.wav"
    assert moved.replaygain_track_gain == -7.5
    assert moved.loudness_scanned_at is not None
