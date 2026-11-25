from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from app import transcode
from app.config import settings
from app.models import LibraryRoot, MediaItem, MediaKind
from app.transcode import (
    ensure_transcoded,
    estimate_source_bitrate_kbps,
)


@pytest.fixture(autouse=True)
def _isolated_transcode_dir(tmp_path, monkeypatch):
    # Same reasoning as _isolated_art_dir in test_art.py: transcode_dir is
    # derived from settings.database_path, so isolate it per test.
    monkeypatch.setattr(settings, "database_path", tmp_path / "isolated.db")
    monkeypatch.setattr(transcode, "_failures", transcode.OrderedDict())


def _make_item(
    db_session, tmp_path, filename: str, duration_seconds: float
) -> tuple[MediaItem, Path]:
    root = LibraryRoot(path=str(tmp_path), display_name="Test")
    db_session.add(root)
    db_session.commit()
    item = MediaItem(
        library_root_id=root.id,
        kind=MediaKind.track,
        relative_path=filename,
        title="Track",
        duration_seconds=duration_seconds,
    )
    db_session.add(item)
    db_session.commit()
    db_session.refresh(item)
    return item, tmp_path / filename


def _make_video_item(db_session, tmp_path, filename: str) -> tuple[MediaItem, Path]:
    root = LibraryRoot(path=str(tmp_path), display_name="Test")
    db_session.add(root)
    db_session.commit()
    item = MediaItem(
        library_root_id=root.id,
        kind=MediaKind.video,
        relative_path=filename,
        title="Video",
    )
    db_session.add(item)
    db_session.commit()
    db_session.refresh(item)
    return item, tmp_path / filename


def test_estimate_source_bitrate_kbps_basic(tmp_path):
    path = tmp_path / "f.bin"
    path.write_bytes(b"x" * 128_000)  # 1,024,000 bits over 10s = 102.4 kbps
    assert estimate_source_bitrate_kbps(path, 10.0) == 102.4


def test_estimate_source_bitrate_kbps_no_duration(tmp_path):
    path = tmp_path / "f.bin"
    path.write_bytes(b"x")
    assert estimate_source_bitrate_kbps(path, None) is None
    assert estimate_source_bitrate_kbps(path, 0) is None


def test_ensure_transcoded_skips_when_source_already_below_cap(db_session, tmp_path):
    item, path = _make_item(db_session, tmp_path, "a.mp3", 10.0)
    path.write_bytes(b"x" * 16_000)  # ~12.8kbps - well under any cap

    with patch("app.transcode.subprocess.run") as mock_run:
        result = ensure_transcoded(item, path, 128)

    assert result is None
    mock_run.assert_not_called()


def test_ensure_transcoded_rejects_disallowed_bitrate(db_session, tmp_path):
    item, path = _make_item(db_session, tmp_path, "a.flac", 10.0)
    path.write_bytes(b"x" * 2_000_000)

    with patch("app.transcode.subprocess.run") as mock_run:
        result = ensure_transcoded(item, path, 999)

    assert result is None
    mock_run.assert_not_called()


def test_ensure_transcoded_runs_ffmpeg_and_caches_result(db_session, tmp_path):
    item, path = _make_item(db_session, tmp_path, "a.flac", 10.0)
    path.write_bytes(b"x" * 2_000_000)  # far above any cap

    def fake_run(cmd, **kwargs):
        Path(cmd[-1]).write_bytes(b"transcoded")

    with patch("app.transcode.subprocess.run", side_effect=fake_run) as mock_run:
        result = ensure_transcoded(item, path, 128)

    assert result is not None
    assert result.read_bytes() == b"transcoded"
    assert result == settings.transcode_dir / f"{item.id}_128k.mp3"
    mock_run.assert_called_once()

    # Second call reuses the cached file instead of invoking ffmpeg again.
    with patch("app.transcode.subprocess.run") as mock_run2:
        result2 = ensure_transcoded(item, path, 128)
    assert result2 == result
    mock_run2.assert_not_called()


def test_ensure_transcoded_returns_none_on_ffmpeg_failure(db_session, tmp_path):
    item, path = _make_item(db_session, tmp_path, "a.flac", 10.0)
    path.write_bytes(b"x" * 2_000_000)

    with patch(
        "app.transcode.subprocess.run",
        side_effect=subprocess.CalledProcessError(1, "ffmpeg"),
    ):
        result = ensure_transcoded(item, path, 128)

    assert result is None


def test_a_failed_transcode_is_remembered_per_source_version(db_session, tmp_path):
    """A Range request after a timed-out transcode does not wait the full
    timeout again."""
    import os

    item, path = _make_item(db_session, tmp_path, "a.flac", 10.0)
    path.write_bytes(b"x" * 2_000_000)
    with patch(
        "app.transcode.subprocess.run", side_effect=subprocess.TimeoutExpired("ffmpeg", 120)
    ) as run:
        assert ensure_transcoded(item, path, 128) is None
        assert ensure_transcoded(item, path, 128) is None
        assert run.call_count == 1
        assert ensure_transcoded(item, path, 192) is None  # another bitrate is tried
        assert run.call_count == 2
        stat = path.stat()
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10**9))  # the file changed
        assert ensure_transcoded(item, path, 128) is None
        assert run.call_count == 3


def test_failure_memory_expires_and_stays_bounded(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(transcode.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(transcode, "MAX_REMEMBERED_FAILURES", 2)
    for key in ((1, 128, 1), (2, 128, 1), (3, 128, 1)):
        transcode._remember_failure(key)
    assert list(transcode._failures) == [(2, 128, 1), (3, 128, 1)]
    assert transcode._failed_recently((3, 128, 1))
    clock[0] += transcode.FAILURE_MEMORY_SECONDS + 1
    assert not transcode._failed_recently((3, 128, 1))
    assert (3, 128, 1) not in transcode._failures


def test_transcode_timeout_scales_with_duration(db_session, tmp_path):
    assert transcode.transcode_timeout(None) == 120
    assert transcode.transcode_timeout(600) == 300
    assert transcode.transcode_timeout(10_000) == transcode.MAX_TIMEOUT_SECONDS
    item, path = _make_item(db_session, tmp_path, "mix.flac", 1200.0)
    with open(path, "wb") as handle:
        handle.truncate(22_000_000)  # sparse: ~147 kbps over 20 minutes, above the 128k cap
    with patch("app.transcode.subprocess.run", side_effect=lambda cmd, **kw: Path(cmd[-1]).write_bytes(b"t")) as run:
        ensure_transcoded(item, path, 128)
    assert run.call_args.kwargs["timeout"] == 600
