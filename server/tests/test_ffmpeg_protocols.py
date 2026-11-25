"""Every ffmpeg/ffprobe run on a library or uploaded file is limited to local
files, stdin and decryption (-protocol_whitelist), and still works."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import art, hls, loudness, transcode, video_meta
from app.art import ArtBytes
from app.config import settings
from tests.conftest import make_audio_fixture

WHITELIST = ["-protocol_whitelist", "file,pipe,crypto"]


@pytest.fixture()
def argvs(monkeypatch):
    seen: list[list[str]] = []
    real = subprocess.run

    def spy(command, *args, **kwargs):
        seen.append(list(command))
        return real(command, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", spy)
    return seen


def _png(path: Path) -> Path:
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=red:s=64x64",
         "-frames:v", "1", str(path)],
        check=True,
    )
    return path


def test_every_invocation_carries_the_whitelist_and_still_works(tmp_path, monkeypatch, argvs):
    monkeypatch.setattr(settings, "database_path", tmp_path / "data" / "app.db")
    wav = tmp_path / "t.wav"
    make_audio_fixture(wav, title="T", artist="A", album="B", genre="Rock")
    png = _png(tmp_path / "a.png")
    del argvs[:]  # drop the setup commands

    assert video_meta.probe_duration_seconds(wav) is not None
    assert video_meta.probe_av_codecs(wav) is not None
    assert video_meta.generate_thumbnail(png, tmp_path / "thumb.jpg", 0) is True
    assert loudness.measure_loudness(wav) is not None
    assert art._resize_art(png, tmp_path / "r1.jpg") is True
    assert art._resize_art(ArtBytes(png.read_bytes(), "image/png"), tmp_path / "r2.jpg") is True
    item = SimpleNamespace(id=1, duration_seconds=20.0)
    monkeypatch.setattr(transcode, "estimate_source_bitrate_kbps", lambda *a, **k: 1000.0)
    assert transcode.ensure_transcoded(item, wav, min(transcode.ALLOWED_BITRATES_KBPS)) is not None  # type: ignore[arg-type]

    assert len(argvs) == 7
    for argv in argvs:
        if argv[:3] == ["nice", "-n", "10"]:  # loudness measuring runs niced
            argv = argv[3:]
        assert argv[0].endswith(("ffmpeg", "ffprobe")), argv
        position = argv.index("-protocol_whitelist")
        assert argv[position : position + 2] == WHITELIST
        if argv[0].endswith("ffmpeg"):
            # An input option only applies to the input that follows it.
            assert position < argv.index("-i")


@pytest.mark.parametrize("audio_kbps", [None, 96])
@pytest.mark.parametrize("use_hw", [False, True])
def test_hls_encode_commands_whitelist_the_source(tmp_path, audio_kbps, use_hw):
    command = hls._encode_command(tmp_path / "v.mkv", tmp_path, 2, use_hw, audio_kbps)
    position = command.index("-protocol_whitelist")
    assert command[position : position + 2] == WHITELIST
    assert position < command.index("-i")
