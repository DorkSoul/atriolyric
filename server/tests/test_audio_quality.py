from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select, update

from app.audio_quality import (
    format_label,
    is_lossless,
    quality_from_audio,
    read_audio_quality,
)
from app.models import LibraryRoot, MediaItem
from app.scanner import scan_library_root
from tests.conftest import make_audio_fixture


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (("flac", 16, 44100, 900, ".flac"), "FLAC 16/44"),
        (("flac", 24, 96000, 2800, ".flac"), "FLAC 24/96"),
        (("alac", 24, 192000, None, ".m4a"), "ALAC 24/192"),
        (("flac", None, None, None, ".flac"), "FLAC"),
        (("mp3", None, 44100, 320, ".mp3"), "MP3 320"),
        (("opus", None, None, 128, ".opus"), "Opus 128"),
        (("aac", None, 44100, None, ".m4a"), "AAC"),
        (("wav", 16, 8000, 128, ".wav"), "WAV 16/8"),
        (("dsd", 1, 2822400, 5645, ".dsf"), "DSD"),
        (("somecodec", None, None, 96, ".xyz"), "SOMECODEC 96"),
        ((None, None, None, None, ".flac"), "FLAC"),
        ((None, None, None, None, "ogg"), "OGG"),
        ((None, None, None, None, None), None),
        ((None, None, None, None, ""), None),
    ],
)
def test_format_label(args, expected):
    assert format_label(*args) == expected


def test_is_lossless_by_codec_or_extension():
    assert is_lossless("flac") and is_lossless("alac") and not is_lossless("mp3")
    assert is_lossless(None, ".FLAC") and not is_lossless(None, ".m4a")


def _fake(class_name: str, **info) -> object:
    return type(class_name, (), {"info": SimpleNamespace(**info)})()


def test_quality_from_audio_maps_mutagen_types(tmp_path):
    path = tmp_path / "x.flac"
    flac = quality_from_audio(_fake("FLAC", sample_rate=96000, bits_per_sample=24, bitrate=2_800_000), path)
    assert flac == {"codec": "flac", "bitrate_kbps": 2800, "sample_rate_hz": 96000, "bit_depth": 24}
    # Lossy formats never get a bit depth, whatever the header says.
    mp3 = quality_from_audio(_fake("EasyMP3", sample_rate=44100, bits_per_sample=16, bitrate=320_000), path)
    assert mp3 == {"codec": "mp3", "bitrate_kbps": 320, "sample_rate_hz": 44100, "bit_depth": None}
    aac = quality_from_audio(_fake("EasyMP4", codec="mp4a.40.2", sample_rate=44100, bitrate=256_000), path)
    assert aac["codec"] == "aac"
    alac = quality_from_audio(_fake("MP4", codec="alac", sample_rate=44100, bits_per_sample=16, bitrate=0), path)
    assert alac["codec"] == "alac" and alac["bit_depth"] == 16


def test_quality_without_a_bitrate_comes_from_size_and_length(tmp_path):
    path = tmp_path / "x.opus"
    path.write_bytes(b"\0" * 16_000)  # 128 kbit over one second
    opus = quality_from_audio(_fake("OggOpus", length=1.0), path)
    assert opus == {"codec": "opus", "bitrate_kbps": 128, "sample_rate_hz": None, "bit_depth": None}


def test_an_unreadable_file_still_gets_its_extension_as_codec(tmp_path):
    path = tmp_path / "broken.mp3"
    path.write_bytes(b"not audio at all")
    assert read_audio_quality(path)["codec"] == "mp3"
    assert quality_from_audio(None, Path("noext"))["codec"] is None


def _scan(db_session, library_dir: Path, mode: str = "normal") -> tuple[LibraryRoot, dict]:
    root = db_session.scalars(select(LibraryRoot).where(LibraryRoot.path == str(library_dir))).first()
    if root is None:
        root = LibraryRoot(path=str(library_dir), display_name="Test Library")
        db_session.add(root)
        db_session.commit()
    return root, scan_library_root(db_session, root, mode=mode)


def _quality(db_session) -> dict[str, tuple]:
    db_session.expire_all()
    return {
        row.title: (row.codec, row.bitrate_kbps, row.sample_rate_hz, row.bit_depth)
        for row in db_session.scalars(select(MediaItem))
    }


def test_a_scan_fills_the_quality_columns(client, db_session, library_dir):
    make_audio_fixture(library_dir / "a.wav", title="Alpha", artist="A", album="LP", genre="Rock")
    _scan(db_session, library_dir)
    # The fixture is 8 kHz, 16-bit mono PCM: 128 kbit/s.
    assert _quality(db_session) == {"Alpha": ("wav", 128, 8000, 16)}
    track = client.get("/api/library/tracks").json()[0]
    assert track["format_label"] == "WAV 16/8"


def test_a_normal_scan_backfills_rows_from_before_the_columns(db_session, library_dir):
    for index in range(3):
        make_audio_fixture(library_dir / f"t{index}.wav", title=f"T{index}", artist="A", album="LP", genre=None)
    _scan(db_session, library_dir)
    db_session.execute(
        update(MediaItem).values(codec=None, bitrate_kbps=None, sample_rate_hz=None, bit_depth=None)
    )
    db_session.commit()

    _root, result = _scan(db_session, library_dir)
    # Not a change to the file: nothing counts as updated.
    assert result["updated"] == 0 and result["added"] == 0
    assert set(_quality(db_session).values()) == {("wav", 128, 8000, 16)}


def test_rescan_metadata_refreshes_the_quality_columns(db_session, library_dir):
    make_audio_fixture(library_dir / "a.wav", title="Alpha", artist="A", album="LP", genre=None)
    _scan(db_session, library_dir)
    db_session.execute(update(MediaItem).values(codec="mp3", bitrate_kbps=96, bit_depth=None))
    db_session.commit()

    _scan(db_session, library_dir)  # a normal scan leaves a filled row alone
    assert _quality(db_session)["Alpha"][0] == "mp3"
    _scan(db_session, library_dir, mode="metadata")
    assert _quality(db_session)["Alpha"] == ("wav", 128, 8000, 16)


def test_format_label_falls_back_to_the_extension_before_a_scan(client, db_session, library_dir):
    make_audio_fixture(library_dir / "a.wav", title="Alpha", artist="A", album="LP", genre=None)
    _scan(db_session, library_dir)
    db_session.execute(update(MediaItem).values(codec=None))
    db_session.commit()
    from app.library_cache import bump_library_version

    bump_library_version()
    assert client.get("/api/library/tracks").json()[0]["format_label"] == "WAV"
