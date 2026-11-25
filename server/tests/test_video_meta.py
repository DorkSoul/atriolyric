from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from app.video_meta import generate_thumbnail, probe_duration_seconds


def _completed(stdout: bytes = b"") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr=b"")


def test_probe_duration_parses_ffprobe_json(tmp_path):
    fake_output = b'{"format": {"duration": "123.456"}}'
    with patch("app.video_meta.subprocess.run", return_value=_completed(fake_output)):
        assert probe_duration_seconds(tmp_path / "clip.mp4") == 123.456


def test_probe_duration_returns_none_when_ffprobe_missing(tmp_path):
    with patch("app.video_meta.subprocess.run", side_effect=FileNotFoundError):
        assert probe_duration_seconds(tmp_path / "clip.mp4") is None


def test_probe_duration_returns_none_on_nonzero_exit(tmp_path):
    with patch(
        "app.video_meta.subprocess.run",
        side_effect=subprocess.CalledProcessError(1, ["ffprobe"]),
    ):
        assert probe_duration_seconds(tmp_path / "clip.mp4") is None


def test_probe_duration_returns_none_on_malformed_json(tmp_path):
    with patch("app.video_meta.subprocess.run", return_value=_completed(b"not json")):
        assert probe_duration_seconds(tmp_path / "clip.mp4") is None


def test_generate_thumbnail_success(tmp_path):
    src = tmp_path / "clip.mp4"
    out = tmp_path / "thumbs" / "1.jpg"

    def fake_run(argv, **kwargs):
        written = Path(argv[-1])  # ffmpeg writes a temp file beside `out`
        assert written.parent == out.parent and written != out
        written.write_bytes(b"fake-jpeg-bytes")
        return _completed()

    with patch("app.video_meta.subprocess.run", side_effect=fake_run):
        assert generate_thumbnail(src, out, at_seconds=2.0) is True
    assert out.read_bytes() == b"fake-jpeg-bytes"
    assert [p.name for p in out.parent.iterdir()] == ["1.jpg"]


def test_generate_thumbnail_timeout_leaves_no_partial_jpeg(tmp_path):
    """A timeout mid-write leaves no partial JPEG at the final path to be
    served as the thumbnail."""
    out = tmp_path / "thumbs" / "1.jpg"

    def slow_run(argv, **kwargs):
        Path(argv[-1]).write_bytes(b"\xff\xd8partial")
        raise subprocess.TimeoutExpired(argv, 30)

    with patch("app.video_meta.subprocess.run", side_effect=slow_run):
        assert generate_thumbnail(tmp_path / "clip.mp4", out) is False
    assert list(out.parent.iterdir()) == []


def test_generate_thumbnail_without_output_is_a_failure(tmp_path):
    out = tmp_path / "thumbs" / "1.jpg"
    with patch("app.video_meta.subprocess.run", return_value=_completed()):
        assert generate_thumbnail(tmp_path / "clip.mp4", out) is False
    assert not out.exists()


def test_generate_thumbnail_missing_ffmpeg_returns_false(tmp_path):
    out = tmp_path / "thumbs" / "1.jpg"
    with patch("app.video_meta.subprocess.run", side_effect=FileNotFoundError):
        assert generate_thumbnail(tmp_path / "clip.mp4", out) is False
    assert not out.exists()


def test_probe_av_codecs_reports_streams_and_none_when_the_probe_fails(tmp_path):
    from app.video_meta import probe_av_codecs

    payload = b'{"streams": [{"codec_type": "video", "codec_name": "h264", "pix_fmt": "yuv420p"}, {"codec_type": "audio", "codec_name": "aac"}]}'
    with patch("app.video_meta.subprocess.run", return_value=_completed(payload)):
        assert probe_av_codecs(tmp_path / "a.mp4") == ("h264", "yuv420p", "aac")
    with patch("app.video_meta.subprocess.run", return_value=_completed(b'{"streams": []}')):
        assert probe_av_codecs(tmp_path / "a.mp4") == (None, None, None)
    with patch("app.video_meta.subprocess.run", side_effect=FileNotFoundError):
        assert probe_av_codecs(tmp_path / "a.mp4") is None


def test_is_browser_playable_edge_cases(tmp_path, monkeypatch):
    from app import video_meta

    monkeypatch.setattr(video_meta, "_playable_cache", {})
    # A file that vanished counts as playable (the stream route 404s later).
    assert video_meta.is_browser_playable(tmp_path / "missing.mp4") is True

    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"x")
    # An unprobeable file is left alone.
    monkeypatch.setattr(video_meta, "probe_av_codecs", lambda _p: None)
    assert video_meta.is_browser_playable(clip) is True
    monkeypatch.setattr(video_meta, "probe_av_codecs", lambda _p: pytest.fail("cached"))
    assert video_meta.is_browser_playable(clip) is True  # answered from the cache

    # The cache is flushed rather than growing without bound.
    monkeypatch.setattr(video_meta, "_playable_cache", {("k", i, i): True for i in range(4100)})
    monkeypatch.setattr(video_meta, "probe_av_codecs", lambda _p: ("hevc", "yuv420p", "aac"))
    other = tmp_path / "other.mp4"
    other.write_bytes(b"yy")
    assert video_meta.is_browser_playable(other) is False
    assert len(video_meta._playable_cache) == 1
