from __future__ import annotations

import logging
import math
import os
import shutil
import sqlite3
import struct
import subprocess
import threading
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from mutagen._vorbis import VCommentDict
from mutagen.easyid3 import EasyID3
from mutagen.easymp4 import EasyMP4Tags
from mutagen.id3 import ID3, TXXX
from mutagen.mp4 import MP4FreeForm, MP4Tags
from mutagen.wave import WAVE

from app import library_cache, loudness
from app.models import LibraryRoot, MediaItem, MediaKind, utcnow
from app.scanner import (
    _parse_gain,
    _parse_peak,
    _parse_r128,
    _raw_replaygain,
    read_audio_tags,
    read_replaygain,
    read_track_peak,
    scan_library_root,
)
from tests.conftest import make_audio_fixture

FFMPEG_SUMMARY = """
[Parsed_ebur128_0 @ 0x1] Summary:

  Integrated loudness:
    I:         -9.5 LUFS
    Threshold: -19.8 LUFS

  Loudness range:
    LRA:         4.1 LU
"""


def _audio(tags):
    return SimpleNamespace(tags=tags, info=SimpleNamespace(length=1.0))


def _tag_wav(path: Path, **txxx: str) -> None:
    make_audio_fixture(path, title=path.stem, artist="Artist", album="Album", genre=None)
    audio = WAVE(str(path))
    for desc, value in txxx.items():
        audio.tags.add(TXXX(encoding=3, desc=desc, text=value))
    audio.save()


# --- tag parsing ---


def test_parse_gain_and_r128_values():
    assert _parse_gain("-6.54 dB") == -6.54
    assert _parse_gain(b"+2.10 dB") == 2.10
    assert _parse_gain("garbage") is None
    assert _parse_gain(None) is None
    assert _parse_gain("-99 dB") is None  # a broken tag, not a real gain
    # Q7.8 relative to -23 LUFS, so -1280 (-5 dB) is 0 dB against -18 LUFS.
    assert _parse_r128("-1280") == 0.0
    assert _parse_r128("256") == 6.0
    assert _parse_r128("n/a") is None


def test_wav_txxx_replaygain_is_read_and_stored_by_a_scan(db_session, library_dir):
    _tag_wav(
        library_dir / "tagged.wav",
        REPLAYGAIN_TRACK_GAIN="-7.25 dB",
        REPLAYGAIN_ALBUM_GAIN="-6.50 dB",
    )
    make_audio_fixture(library_dir / "plain.wav", title="plain", artist="A", album=None, genre=None)
    tags = read_audio_tags(library_dir / "tagged.wav")
    assert (tags["replaygain_track_gain"], tags["replaygain_album_gain"]) == (-7.25, -6.5)

    root = LibraryRoot(path=str(library_dir), display_name="Lib")
    db_session.add(root)
    db_session.commit()
    scan_library_root(db_session, root)
    tagged = db_session.query(MediaItem).filter_by(relative_path="tagged.wav").one()
    plain = db_session.query(MediaItem).filter_by(relative_path="plain.wav").one()
    assert (tagged.replaygain_track_gain, tagged.replaygain_album_gain) == (-7.25, -6.5)
    assert tagged.loudness_scanned_at is not None
    # Untagged files are left for the ffmpeg backfill.
    assert plain.replaygain_track_gain is None and plain.loudness_scanned_at is None

    # A changed file without tags drops the stale gains for re-measurement.
    make_audio_fixture(library_dir / "tagged.wav", title="tagged", artist="Artist", album="Album", genre=None)
    tagged_path = library_dir / "tagged.wav"
    stat = tagged_path.stat()
    os.utime(tagged_path, (stat.st_atime, stat.st_mtime + 10))
    scan_library_root(db_session, root)
    db_session.refresh(tagged)
    assert tagged.replaygain_track_gain is None and tagged.loudness_scanned_at is None


def test_mp3_rva2_via_easy_keys(tmp_path):
    easy = EasyID3()
    easy["replaygain_track_gain"] = "-3.50 dB"
    easy["replaygain_album_gain"] = "-2.25 dB"
    with patch("app.scanner.mutagen.File", return_value=_audio(easy)):
        assert read_replaygain(tmp_path / "song.mp3") == (-3.5, -2.25)


def test_mp3_txxx_fallback_when_easy_mode_hides_it(tmp_path):
    raw = ID3()
    raw.add(TXXX(encoding=3, desc="REPLAYGAIN_TRACK_GAIN", text="-4.00 dB"))
    raw.add(TXXX(encoding=3, desc="replaygain_album_gain", text=[]))

    def fake_file(_path, easy=False):
        return _audio(EasyID3() if easy else raw)

    with patch("app.scanner.mutagen.File", side_effect=fake_file):
        assert read_replaygain(tmp_path / "song.mp3") == (-4.0, None)


def test_flac_vorbis_comments(tmp_path):
    comments = VCommentDict()
    comments["REPLAYGAIN_TRACK_GAIN"] = ["-8.10 dB"]
    comments["REPLAYGAIN_ALBUM_GAIN"] = ["-7.90 dB"]
    with patch("app.scanner.mutagen.File", return_value=_audio(comments)):
        assert read_replaygain(tmp_path / "song.flac") == (-8.1, -7.9)


def test_opus_r128_gains_get_the_replaygain_offset(tmp_path):
    comments = VCommentDict()
    comments["R128_TRACK_GAIN"] = ["-2560"]  # -10 dB vs -23 LUFS
    comments["R128_ALBUM_GAIN"] = ["0"]
    with patch("app.scanner.mutagen.File", return_value=_audio(comments)):
        assert read_replaygain(tmp_path / "song.opus") == (-5.0, 5.0)


def test_m4a_freeform_itunes_atoms(tmp_path):
    raw = MP4Tags()
    raw["----:com.apple.iTunes:REPLAYGAIN_TRACK_GAIN"] = [MP4FreeForm(b"-6.00 dB")]
    raw["----:com.apple.iTunes:replaygain_album_gain"] = []
    raw["\xa9nam"] = ["Title"]

    def fake_file(_path, easy=False):
        return _audio(EasyMP4Tags() if easy else raw)

    with patch("app.scanner.mutagen.File", side_effect=fake_file):
        assert read_replaygain(tmp_path / "song.m4a") == (-6.0, None)


def test_unreadable_or_untagged_files_have_no_gain(tmp_path):
    with patch("app.scanner.mutagen.File", side_effect=OSError("boom")):
        assert read_replaygain(tmp_path / "x.mp3") == (None, None)
        assert _raw_replaygain(tmp_path / "x.mp3", "track") is None
    with patch("app.scanner.mutagen.File", return_value=None):
        assert read_replaygain(tmp_path / "x.mp3") == (None, None)
        assert _raw_replaygain(tmp_path / "x.mp3", "track") is None
    with patch("app.scanner.mutagen.File", return_value=_audio(None)):
        assert read_replaygain(tmp_path / "x.mp3") == (None, None)


# --- ffmpeg measurement ---


def test_parse_integrated_lufs():
    assert loudness.parse_integrated_lufs(FFMPEG_SUMMARY) == -9.5
    assert loudness.parse_integrated_lufs("no summary here") is None


def test_measure_uses_ffmpeg_output_and_tolerates_failures(tmp_path):
    ok = SimpleNamespace(returncode=0, stderr=FFMPEG_SUMMARY)
    with patch("app.loudness.subprocess.run", return_value=ok) as run:
        assert loudness.measure_integrated_lufs(tmp_path / "a.flac") == -9.5
    assert "ebur128=peak=true:framelog=verbose" in run.call_args.args[0]
    with patch("app.loudness.subprocess.run", return_value=SimpleNamespace(returncode=1, stderr="")):
        assert loudness.measure_integrated_lufs(tmp_path / "a.flac") is None
    with patch("app.loudness.subprocess.run", side_effect=subprocess.TimeoutExpired("ffmpeg", 1)):
        assert loudness.measure_integrated_lufs(tmp_path / "a.flac") is None
        # The pass itself is told, so it can leave the track pending.
        with pytest.raises(loudness.MeasurementTimedOut):
            loudness.measure_loudness(tmp_path / "a.flac")
    with patch("app.loudness.subprocess.run", side_effect=FileNotFoundError("ffmpeg")):
        assert loudness.measure_integrated_lufs(tmp_path / "a.flac") is None


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
def test_real_ffmpeg_measures_a_generated_file(tmp_path):
    path = tmp_path / "tone.wav"
    with wave.open(str(path), "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"".join(
            struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * i / 8000)))
            for i in range(8000 * 2)
        ))
    lufs = loudness.measure_integrated_lufs(path)
    assert lufs is not None and -40 < lufs < 0


def test_gain_formulas():
    assert loudness.track_gain_for_lufs(-9.5) == -8.5
    # Identical tracks: the album is as loud as each of them.
    assert loudness.album_gain_for_track_gains([-5.0, -5.0]) == pytest.approx(-5.0)
    # Energy mean: the loud track dominates, so the album gain sits well
    # below the plain average of -5 dB.
    assert loudness.album_gain_for_track_gains([-10.0, 0.0]) == pytest.approx(-7.4036, abs=1e-3)


# --- backfill ---


def _track(db_session, root, name, **values):
    item = MediaItem(
        library_root_id=root.id,
        kind=values.pop("kind", MediaKind.track),
        relative_path=name,
        title=name,
        **values,
    )
    db_session.add(item)
    db_session.commit()
    return item


def test_backfill_reads_tags_measures_the_rest_and_fills_album_gains(
    db_session, session_factory, library_dir
):
    _tag_wav(library_dir / "tagged.wav", REPLAYGAIN_TRACK_GAIN="-1.00 dB")
    for name in ("loud.wav", "quiet.wav", "broken.wav", "single.wav"):
        make_audio_fixture(library_dir / name, title=name, artist="A", album=None, genre=None)
    root = LibraryRoot(path=str(library_dir), display_name="Lib")
    db_session.add(root)
    db_session.commit()
    # Rows scanned before gains were stored: none of them have been checked.
    tagged = _track(db_session, root, "tagged.wav", artist="A", album="LP")
    loud = _track(db_session, root, "loud.wav", artist="A", album="LP")
    quiet = _track(db_session, root, "quiet.wav", artist="Other", album_artist="A", album="LP")
    broken = _track(db_session, root, "broken.wav", artist="A", album="LP")
    missing = _track(db_session, root, "gone.wav", artist="A", album="LP")
    single = _track(db_session, root, "single.wav", album="Loose")
    video = _track(db_session, root, "clip.mp4", kind=MediaKind.video)
    done = _track(db_session, root, "done.wav", replaygain_track_gain=3.0, loudness_scanned_at=utcnow())

    measured = {"loud.wav": -8.0, "quiet.wav": -28.0, "single.wav": -18.0, "broken.wav": None}
    calls: list[str] = []

    def fake_measure(path: Path):
        calls.append(path.name)
        return measured[path.name]

    progress: list[tuple[int, int, int]] = []
    version = library_cache._version
    changed = loudness.backfill_loudness(session_factory, fake_measure, lambda d, t, s: progress.append((d, t, s)))

    assert sorted(calls) == ["broken.wav", "loud.wav", "quiet.wav", "single.wav"]
    # tagged, loud, quiet and single got a gain; the missing file is not
    # counted as checked.
    assert progress[-1] == (5, 5, 4)
    assert library_cache._version == version + 1
    db_session.expire_all()
    assert tagged.replaygain_track_gain == -1.0
    assert loud.replaygain_track_gain == -10.0
    assert quiet.replaygain_track_gain == 10.0
    # A file ffmpeg could not measure counts as checked, and is never retried.
    assert broken.replaygain_track_gain is None
    assert broken.loudness_scanned_at is not None
    # A file that is not there stays pending.
    assert missing.replaygain_track_gain is None
    assert missing.loudness_scanned_at is None
    assert video.loudness_scanned_at is None
    assert done.replaygain_album_gain is None  # no album
    # LP's album gain comes from its three measured tracks.
    lp_gain = loudness.album_gain_for_track_gains([-1.0, -10.0, 10.0])
    for item in (tagged, loud, quiet):
        assert item.replaygain_album_gain == pytest.approx(lp_gain)
    assert broken.replaygain_album_gain is None
    assert single.replaygain_album_gain == pytest.approx(0.0)
    assert changed == 4 + 4

    # Nothing left to do: no measuring, no cache bump.
    calls.clear()
    assert loudness.backfill_loudness(session_factory, fake_measure) == 0
    assert calls == []
    assert library_cache._version == version + 1


def test_backfill_keeps_album_gain_from_tags_and_skips_rows_already_covered(
    db_session, session_factory, library_dir
):
    _tag_wav(library_dir / "a.wav", REPLAYGAIN_TRACK_GAIN="-2 dB", REPLAYGAIN_ALBUM_GAIN="-3 dB")
    root = LibraryRoot(path=str(library_dir), display_name="Lib")
    db_session.add(root)
    db_session.commit()
    item = _track(db_session, root, "a.wav", artist="A", album="LP")
    other = _track(db_session, root, "b.wav", artist="A", album="LP")

    def covered_meanwhile(_path):
        raise AssertionError("tagged file must not be measured")

    real_get = type(db_session).get

    def get_with_race(self, model, ident, *args, **kwargs):
        row = real_get(self, model, ident, *args, **kwargs)
        if ident == other.id:
            row.loudness_scanned_at = utcnow()  # a scan got there first
        return row

    with patch.object(type(db_session), "get", get_with_race):
        loudness.backfill_loudness(session_factory, covered_meanwhile)
    db_session.expire_all()
    assert (item.replaygain_track_gain, item.replaygain_album_gain) == (-2.0, -3.0)
    assert other.replaygain_track_gain is None


def test_start_loudness_backfill_runs_once_more_when_asked_mid_run(monkeypatch, caplog):
    started = threading.Event()
    release = threading.Event()
    runs: list[int] = []

    def fake_backfill(progress_cb=None):
        progress_cb(1, 1, 1)
        runs.append(1)
        if len(runs) == 1:
            started.set()
            release.wait(5)
            return 3
        raise RuntimeError("second pass fails")

    monkeypatch.setattr(loudness, "backfill_loudness", fake_backfill)
    caplog.set_level(logging.INFO, logger="app.loudness")
    loudness.start_loudness_backfill()
    assert started.wait(5)
    loudness.start_loudness_backfill()  # queued, not a second thread
    loudness.start_loudness_backfill()
    release.set()
    for _ in range(100):
        if not loudness._run_lock.locked():
            break
        threading.Event().wait(0.05)
    assert not loudness._run_lock.locked()
    assert len(runs) == 2
    assert "Loudness backfill complete: 3 values stored" in caplog.text
    assert "Loudness backfill failed" in caplog.text
    assert "Loudness backfill: 1 / 1 tracks checked, 1 values stored" in caplog.text


def test_run_until_idle_picks_up_a_request_made_after_its_last_check(monkeypatch):
    runs: list[int] = []

    def fake_backfill(progress_cb=None):
        runs.append(1)
        return 0

    real_release = loudness._run_lock.release
    requested_late = []

    class LateRequestLock:
        def acquire(self, blocking=True):
            return loudness_lock.acquire(blocking)

        def release(self):
            real_release()
            if not requested_late:
                requested_late.append(1)
                loudness._rerun.set()  # arrives just as the thread lets go

        def locked(self):
            return loudness_lock.locked()

    loudness_lock = loudness._run_lock
    monkeypatch.setattr(loudness, "backfill_loudness", fake_backfill)
    monkeypatch.setattr(loudness, "_run_lock", LateRequestLock())
    loudness._rerun.set()
    loudness_lock.acquire()
    loudness._run_until_idle()
    assert len(runs) == 2
    assert not loudness_lock.locked()


PEAK_SUMMARY = FFMPEG_SUMMARY + """
  True peak:
    Peak:      -6.0 dBFS
"""


def test_parse_true_peak_converts_dbfs_to_linear():
    assert loudness.parse_true_peak(PEAK_SUMMARY) == pytest.approx(0.5012, abs=1e-3)
    assert loudness.parse_true_peak("True peak:\n    Peak:       -inf dBFS") == 0.0
    assert loudness.parse_true_peak(FFMPEG_SUMMARY) is None


def test_measure_loudness_returns_lufs_and_peak(tmp_path):
    ok = SimpleNamespace(returncode=0, stderr=PEAK_SUMMARY)
    with patch("app.loudness.subprocess.run", return_value=ok):
        lufs, peak = loudness.measure_loudness(tmp_path / "a.flac")
    assert lufs == -9.5 and peak == pytest.approx(0.5012, abs=1e-3)
    assert loudness.parse_integrated_lufs(PEAK_SUMMARY) == -9.5


def test_parse_peak_tag_values():
    assert _parse_peak("0.988553") == 0.988553
    assert _parse_peak(b"1.2") == 1.2
    assert _parse_peak("0") is None
    assert _parse_peak("99") is None
    assert _parse_peak("n/a") is None
    assert _parse_peak(None) is None


def test_peak_read_from_tags_and_stored_by_scan(db_session, library_dir):
    _tag_wav(
        library_dir / "tagged.wav",
        REPLAYGAIN_TRACK_GAIN="-7.25 dB",
        REPLAYGAIN_TRACK_PEAK="0.912345",
    )
    assert read_track_peak(library_dir / "tagged.wav") == 0.912345
    assert read_audio_tags(library_dir / "tagged.wav")["replaygain_track_peak"] == 0.912345
    root = LibraryRoot(path=str(library_dir), display_name="Lib")
    db_session.add(root)
    db_session.commit()
    scan_library_root(db_session, root)
    item = db_session.query(MediaItem).one()
    assert item.replaygain_track_peak == 0.912345
    # A metadata-only rescan of an untouched file refreshes the peak too.
    item.replaygain_track_peak = None
    db_session.commit()
    scan_library_root(db_session, root, mode="metadata")
    db_session.refresh(item)
    assert item.replaygain_track_peak == 0.912345


def test_read_track_peak_tolerates_unreadable_files(tmp_path):
    with patch("app.scanner.mutagen.File", side_effect=Exception("boom")):
        assert read_track_peak(tmp_path / "x.mp3") is None
    with patch("app.scanner.mutagen.File", return_value=None):
        assert read_track_peak(tmp_path / "x.mp3") is None


def test_backfill_records_measured_and_tagged_peaks(db_session, session_factory, library_dir):
    _tag_wav(library_dir / "tagged.wav", REPLAYGAIN_TRACK_GAIN="-1.00 dB", REPLAYGAIN_TRACK_PEAK="0.8")
    make_audio_fixture(library_dir / "plain.wav", title="p", artist="A", album=None, genre=None)
    make_audio_fixture(library_dir / "nopeak.wav", title="n", artist="A", album=None, genre=None)
    root = LibraryRoot(path=str(library_dir), display_name="Lib")
    db_session.add(root)
    db_session.commit()
    tagged = _track(db_session, root, "tagged.wav")
    plain = _track(db_session, root, "plain.wav")
    nopeak = _track(db_session, root, "nopeak.wav")

    def measure(path):
        return (-20.0, 0.5) if path.name == "plain.wav" else (-20.0, None)

    loudness.backfill_loudness(session_factory, measure)
    db_session.expire_all()
    assert tagged.replaygain_track_peak == 0.8  # tag wins, not measured
    assert plain.replaygain_track_gain == 2.0 and plain.replaygain_track_peak == 0.5
    assert nopeak.replaygain_track_gain == 2.0 and nopeak.replaygain_track_peak is None


# --- writes by id, one grouped album query, nice -----------------------------


def test_a_row_deleted_mid_pass_does_not_abort_the_batch(db_session, session_factory, library_dir):
    for name in ("a.wav", "b.wav", "c.wav"):
        make_audio_fixture(library_dir / name, title=name, artist="A", album=None, genre=None)
    root = LibraryRoot(path=str(library_dir), display_name="Lib")
    db_session.add(root)
    db_session.commit()
    a = _track(db_session, root, "a.wav", artist="A", album="LP")
    b = _track(db_session, root, "b.wav", artist="A", album="LP")
    c = _track(db_session, root, "c.wav", artist="A", album="LP")
    b_id = b.id

    def measure_then_a_scan_deletes_b(path: Path):
        if path.name == "a.wav":
            other = session_factory()
            other.query(MediaItem).filter(MediaItem.id == b_id).delete()
            other.commit()
            other.close()
        return -18.0

    loudness.backfill_loudness(session_factory, measure_then_a_scan_deletes_b)
    db_session.expire_all()
    assert db_session.get(MediaItem, b_id) is None
    for item in (a, c):
        assert item.replaygain_track_gain == 0.0 and item.loudness_scanned_at is not None
        assert item.replaygain_album_gain == pytest.approx(0.0)


def test_other_writes_are_not_blocked_while_a_track_is_measured(
    db_session, session_factory, library_dir
):
    """Writing each row as it is measured and committing per batch would
    hold SQLite's write lock for minutes at a time, and a mix refresh or
    device heartbeat would fail with "database is locked"."""
    for name in ("a.wav", "b.wav", "c.wav"):
        make_audio_fixture(library_dir / name, title=name, artist="A", album=None, genre=None)
    root = LibraryRoot(path=str(library_dir), display_name="Lib")
    db_session.add(root)
    db_session.commit()
    tracks = [_track(db_session, root, name) for name in ("a.wav", "b.wav", "c.wav")]
    db_path = session_factory.kw["bind"].url.database
    written = []

    def measure_while_something_else_writes(path: Path):
        # A separate connection that gives up at once instead of waiting.
        other = sqlite3.connect(db_path, timeout=0)
        try:
            other.execute("UPDATE library_roots SET display_name = ?", (path.name,))
            other.commit()
            written.append(path.name)
        finally:
            other.close()
        return -18.0

    loudness.backfill_loudness(session_factory, measure_while_something_else_writes)
    db_session.expire_all()
    assert written == ["a.wav", "b.wav", "c.wav"]
    assert all(t.replaygain_track_gain == 0.0 for t in tracks)


def test_album_gains_come_from_one_query(db_session, library_dir):
    from sqlalchemy import event

    root = LibraryRoot(path=str(library_dir), display_name="Lib")
    db_session.add(root)
    db_session.commit()
    for album in range(5):
        for track in range(3):
            _track(
                db_session, root, f"{album}-{track}.wav", artist=None if album == 0 else "A",
                album=f"LP{album}", replaygain_track_gain=float(track), loudness_scanned_at=utcnow(),
            )
    _track(db_session, root, "kept.wav", artist="A", album="LP1", replaygain_track_gain=9.0,
           replaygain_album_gain=-4.0, loudness_scanned_at=utcnow())
    selects: list[str] = []

    def listener(conn, cursor, statement, *_a):
        if statement.lstrip().upper().startswith("SELECT"):
            selects.append(statement)

    engine = db_session.get_bind()
    event.listen(engine, "before_cursor_execute", listener)
    try:
        assert loudness._fill_album_gains(db_session) == 15
    finally:
        event.remove(engine, "before_cursor_execute", listener)
    assert len(selects) == 1
    db_session.expire_all()
    rows = db_session.query(MediaItem).filter(MediaItem.album == "LP1").all()
    expected = loudness.album_gain_for_track_gains([0.0, 1.0, 2.0, 9.0])
    assert sorted(r.replaygain_album_gain for r in rows) == pytest.approx(sorted([expected] * 3 + [-4.0]))
    no_artist = db_session.query(MediaItem).filter(MediaItem.album == "LP0").all()
    assert {round(r.replaygain_album_gain, 6) for r in no_artist} == {
        round(loudness.album_gain_for_track_gains([0.0, 1.0, 2.0]), 6)
    }


def test_measuring_runs_under_nice_when_available(tmp_path, monkeypatch):
    ok = SimpleNamespace(returncode=0, stderr=FFMPEG_SUMMARY)
    monkeypatch.setattr(loudness, "_NICE", ("nice", "-n", "10"))
    with patch("app.loudness.subprocess.run", return_value=ok) as run:
        loudness.measure_loudness(tmp_path / "a.flac")
    assert run.call_args.args[0][:4] == ["nice", "-n", "10", "ffmpeg"]
    monkeypatch.setattr(loudness, "_NICE", ())
    with patch("app.loudness.subprocess.run", return_value=ok) as run:
        loudness.measure_loudness(tmp_path / "a.flac")
    assert run.call_args.args[0][0] == "ffmpeg"


def test_tracks_marked_scanned_without_a_gain_are_queued_again(session_factory):
    """A server whose ffmpeg could not measure marked every track as done;
    the one-time backfill clears that mark, and only for tracks."""
    from app.migrations import retry_unmeasured_loudness
    from tests.conftest import startup_against

    db = session_factory()
    root = LibraryRoot(path="/music", display_name="Music")
    db.add(root)
    db.flush()

    def item(name: str, kind: MediaKind, gain: float | None, scanned: bool) -> MediaItem:
        row = MediaItem(
            library_root_id=root.id,
            relative_path=name,
            title=name,
            kind=kind,
            replaygain_track_gain=gain,
            loudness_scanned_at=utcnow() if scanned else None,
        )
        db.add(row)
        return row

    failed = item("failed.flac", MediaKind.track, None, True)
    measured = item("measured.flac", MediaKind.track, -3.5, True)
    video = item("clip.mkv", MediaKind.video, None, True)
    db.commit()
    ids = failed.id, measured.id, video.id
    db.close()

    with startup_against(session_factory):
        retry_unmeasured_loudness()
        retry_unmeasured_loudness()  # idempotent

    db = session_factory()
    try:
        failed, measured, video = (db.get(MediaItem, i) for i in ids)
        assert failed.loudness_scanned_at is None
        assert measured.loudness_scanned_at is not None
        assert measured.replaygain_track_gain == -3.5
        assert video.loudness_scanned_at is not None
    finally:
        db.close()


# --- an unreachable file is not a failed measurement -------------------------


def _library(db_session, folder: Path, names: list[str], *, files: bool = True) -> list[MediaItem]:
    folder.mkdir(exist_ok=True)
    if files:
        for name in names:
            make_audio_fixture(folder / name, title=name, artist="A", album=None, genre=None)
    root = LibraryRoot(path=str(folder), display_name=folder.name)
    db_session.add(root)
    db_session.commit()
    return [_track(db_session, root, name) for name in names]


def _pending(db_session, items: list[MediaItem]) -> list[bool]:
    db_session.expire_all()
    return [item.loudness_scanned_at is None for item in items]


def test_a_missing_file_and_a_timeout_store_nothing_and_are_tried_again(
    db_session, session_factory, library_dir, caplog
):
    ok, gone, slow, broken, vanishing = _library(
        db_session, library_dir, ["ok.wav", "gone.wav", "slow.wav", "broken.wav", "vanishing.wav"]
    )
    (library_dir / "gone.wav").unlink()
    calls: list[str] = []

    def measure(path: Path):
        calls.append(path.name)
        if path.name == "slow.wav":
            raise loudness.MeasurementTimedOut
        if path.name == "vanishing.wav":
            path.unlink()  # the share drops out while ffmpeg reads
            return None
        return None if path.name == "broken.wav" else -18.0

    progress: list[tuple[int, int, int]] = []
    caplog.set_level(logging.INFO, logger="app.loudness")
    loudness.backfill_loudness(session_factory, measure, lambda d, t, s: progress.append((d, t, s)))
    assert _pending(db_session, [ok, gone, slow, broken, vanishing]) == [False, True, True, False, True]
    assert ok.replaygain_track_gain == 0.0
    # Present but not measurable: recorded as a failure.
    assert broken.replaygain_track_gain is None
    # Progress counts what could be dealt with.
    assert progress[-1] == (2, 2, 1)
    assert "Loudness backfill left 3 tracks pending" in caplog.text

    for name in ("gone.wav", "vanishing.wav"):
        make_audio_fixture(library_dir / name, title=name, artist="A", album=None, genre=None)
    calls.clear()
    loudness.backfill_loudness(session_factory, lambda path: calls.append(path.name) or -18.0)
    assert sorted(calls) == ["gone.wav", "slow.wav", "vanishing.wav"]
    assert _pending(db_session, [gone, slow, vanishing]) == [False, False, False]


def test_an_unmounted_library_keeps_its_backlog_and_is_looked_at_once(
    db_session, session_factory, tmp_path, caplog, monkeypatch
):
    # An unmounted share is an existing, empty folder.
    names = [f"t{i:02}.wav" for i in range(30)]
    away = _library(db_session, tmp_path / "nas", names, files=False)
    here = _library(db_session, tmp_path / "local", ["a.wav", "b.wav"])
    looked: list[str] = []
    real = loudness.media_file_path

    def counting(item, **kwargs):
        looked.append(item.relative_path)
        return real(item, **kwargs)

    monkeypatch.setattr(loudness, "media_file_path", counting)
    progress: list[tuple[int, int, int]] = []
    caplog.set_level(logging.INFO, logger="app.loudness")
    loudness._retry_wanted.clear()
    loudness.backfill_loudness(session_factory, lambda _path: -18.0, lambda d, t, s: progress.append((d, t, s)))
    assert _pending(db_session, away) == [True] * 30
    assert _pending(db_session, here) == [False, False]
    assert looked == ["t00.wav", "a.wav", "b.wav"]
    assert progress == [(2, 2, 2)]
    assert f"Loudness backfill left 30 tracks pending: library folder not reachable: {tmp_path / 'nas'}" in caplog.text
    assert loudness._retry_wanted.is_set()

    # The share is back: the next pass measures all of it.
    for name in names:
        make_audio_fixture(tmp_path / "nas" / name, title=name, artist="A", album=None, genre=None)
    loudness._retry_wanted.clear()
    loudness.backfill_loudness(session_factory, lambda _path: -18.0)
    assert _pending(db_session, away) == [False] * 30
    assert not loudness._retry_wanted.is_set()


def test_a_run_of_missing_files_sets_the_folder_aside_for_the_pass(
    db_session, session_factory, library_dir, monkeypatch
):
    # The folder itself looks fine (a share mounted below it is gone).
    monkeypatch.setattr(loudness, "MISSING_STREAK_PAUSE", 5)
    names = [f"gone{i}.wav" for i in range(8)] + ["there.wav"]
    items = _library(db_session, library_dir, names)
    for name in names[:8]:
        (library_dir / name).unlink()
    measured: list[str] = []
    loudness._retry_wanted.clear()
    loudness.backfill_loudness(session_factory, lambda path: measured.append(path.name) or -18.0)
    assert measured == []
    assert _pending(db_session, items) == [True] * 9
    assert loudness._retry_wanted.is_set()


def test_a_few_deleted_files_do_not_hold_the_rest_back_or_ask_for_a_retry(
    db_session, session_factory, library_dir
):
    items = _library(db_session, library_dir, ["a.wav", "deleted.wav", "b.wav"])
    (library_dir / "deleted.wav").unlink()
    loudness._retry_wanted.clear()
    loudness.backfill_loudness(session_factory, lambda _path: -18.0)
    assert _pending(db_session, items) == [False, True, False]
    # Only a scan changes anything about a deleted file.
    assert not loudness._retry_wanted.is_set()


def test_a_file_that_leads_out_of_its_library_is_still_recorded_as_failed(
    db_session, session_factory, library_dir, tmp_path
):
    outside = tmp_path / "outside.wav"
    make_audio_fixture(outside, title="o", artist="A", album=None, genre=None)
    (item,) = _library(db_session, library_dir, ["link.wav"], files=False)
    (library_dir / "link.wav").symlink_to(outside)
    loudness.backfill_loudness(session_factory, lambda _path: pytest.fail("must not be read"))
    assert _pending(db_session, [item]) == [False]
    assert item.replaygain_track_gain is None


def test_a_pass_that_could_not_reach_a_library_is_run_again_later(monkeypatch, caplog):
    timers: list[SimpleNamespace] = []

    def fake_timer(delay, function):
        timer = SimpleNamespace(
            delay=delay,
            function=function,
            started=False,
            cancelled=False,
            start=lambda: setattr(timer, "started", True),
            cancel=lambda: setattr(timer, "cancelled", True),
        )
        timers.append(timer)
        return timer

    unreachable = [True, True, False]

    def fake_backfill(progress_cb=None):
        if unreachable.pop(0):
            loudness._retry_wanted.set()
        return 0

    monkeypatch.setattr(loudness.threading, "Timer", fake_timer)
    monkeypatch.setattr(loudness, "backfill_loudness", fake_backfill)
    monkeypatch.setattr(loudness, "_run_lock", threading.Lock())
    monkeypatch.setattr(loudness, "_retry_timer", None)
    monkeypatch.setattr(loudness, "_retry_delay", 0.0)
    caplog.set_level(logging.INFO, logger="app.loudness")

    def run_pass():
        loudness._rerun.set()
        loudness._run_lock.acquire()
        loudness._run_until_idle()

    run_pass()
    assert [(t.delay, t.started, t.function) for t in timers] == [
        (loudness.UNREACHABLE_RETRY_SECONDS, True, loudness.start_loudness_backfill)
    ]
    assert "Loudness backfill: trying again in 15 minutes" in caplog.text
    run_pass()  # still unreachable: the earlier timer is replaced, the wait doubles
    assert timers[0].cancelled
    assert timers[1].delay == 2 * loudness.UNREACHABLE_RETRY_SECONDS
    run_pass()  # everything reached: no further retry, and the wait starts over
    assert timers[1].cancelled and len(timers) == 2
    assert loudness._retry_delay == 0.0
