"""Feature extraction (app.audio_features): the ffmpeg command, the pipe
reader's bounds and failure paths, and the numpy analysis itself."""

from __future__ import annotations

import io
import shutil
import subprocess
import threading
from pathlib import Path

import numpy as np
import pytest

from app import audio_features as af
from app.video_meta import INPUT_PROTOCOLS

SR = af.SAMPLE_RATE
needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")


def click_track(bpm: float, seconds: float, *, gain: float = 0.5, seed: int = 0) -> np.ndarray:
    """Decaying noise bursts on every beat over a quiet hum."""
    rng = np.random.default_rng(seed)
    t = np.arange(int(seconds * SR)) / SR
    phase = np.mod(t, 60.0 / bpm)
    signal = rng.standard_normal(len(t)) * np.exp(-40 * phase) + 0.05 * np.sin(2 * np.pi * 110 * t)
    return (gain * signal / np.abs(signal).max()).astype(np.float32)


def lowpassed_noise(cutoff_hz: float, seconds: float = 30.0, seed: int = 1) -> np.ndarray:
    rng = np.random.default_rng(seed)
    spectrum = np.fft.rfft(rng.standard_normal(int(seconds * SR)))
    spectrum[np.fft.rfftfreq(int(seconds * SR), 1 / SR) > cutoff_hz] = 0
    noise = np.fft.irfft(spectrum)
    return (0.3 * noise / np.abs(noise).max()).astype(np.float32)


# --- the ffmpeg command ---


def test_window_starts():
    assert af.window_starts(None) is None
    assert af.window_starts(af.MIN_WINDOWED_SECONDS - 1) is None
    starts = af.window_starts(240.0)
    assert starts == [33.0, 105.0, 177.0]  # centred at 20/50/80 %
    # The last window never runs past the end.
    short = af.window_starts(af.MIN_WINDOWED_SECONDS)
    assert short is not None and short[-1] + af.WINDOW_SECONDS <= af.MIN_WINDOWED_SECONDS


def test_argv_windows_each_input_is_protocol_limited_and_seeks_before_reading(tmp_path):
    argv, channels = af.ffmpeg_argv(tmp_path / "a.flac", 240.0)
    assert channels == 3
    assert "-nostdin" in argv and argv[argv.index("ffmpeg") - 3 : argv.index("ffmpeg")] in (
        ["nice", "-n", "10"],
        [],
    )
    inputs = [i for i, arg in enumerate(argv) if arg == "-i"]
    assert len(inputs) == 3
    for i in inputs:
        # -protocol_whitelist, -ss, -t precede every -i.
        assert argv[i - 6 : i - 4] == list(INPUT_PROTOCOLS)
        assert argv[i - 4] == "-ss" and argv[i - 2] == "-t" and argv[i - 1] == "30"
        assert argv[i + 1] == str(tmp_path / "a.flac")
    graph = argv[argv.index("-filter_complex") + 1]
    assert "amerge=inputs=3" in graph and graph.count("aresample=22050") == 3
    assert argv[-5:] == ["-c:a", "pcm_f32le", "-f", "f32le", "pipe:1"]


def test_argv_short_or_unknown_length_reads_from_the_start(tmp_path):
    for duration in (None, 60.0):
        argv, channels = af.ffmpeg_argv(tmp_path / "a.mp3", duration)
        assert channels == 1
        assert argv.count("-i") == 1 and "-ss" not in argv
        assert argv[argv.index("-t") + 1] == "90"


# --- the pipe reader, with a fake ffmpeg ---


class FakeProc:
    def __init__(self, data: bytes = b"", returncode: int = 0, stream=None):
        self.stdout = stream if stream is not None else io.BufferedReader(io.BytesIO(data))
        self._returncode = returncode
        self.killed = False

    def kill(self):
        self.killed = True
        if hasattr(self.stdout, "unblock"):
            self.stdout.unblock()

    def poll(self):
        return None if not self.killed else -9

    def wait(self):
        return -9 if self.killed else self._returncode


def pcm(*channels: np.ndarray) -> bytes:
    return np.stack(channels, axis=1).astype("<f4").tobytes()


def test_decode_deinterleaves_the_three_windows(monkeypatch, tmp_path):
    windows = [np.full(SR, value, np.float32) for value in (0.1, 0.2, 0.3)]
    monkeypatch.setattr(af.subprocess, "Popen", lambda *a, **k: FakeProc(pcm(*windows)))
    out = af.decode_excerpt(tmp_path / "a.flac", 240.0)
    assert [w[0] for w in out] == pytest.approx([0.1, 0.2, 0.3])
    assert all(len(w) == SR for w in out)


def test_decode_failures_raise_analysis_error(monkeypatch, tmp_path):
    monkeypatch.setattr(af.subprocess, "Popen", lambda *a, **k: FakeProc(b"", returncode=1))
    with pytest.raises(af.AnalysisError, match="ffmpeg exited 1"):
        af.decode_excerpt(tmp_path / "a.flac", None)

    def missing(*a, **k):
        raise FileNotFoundError("ffmpeg")

    monkeypatch.setattr(af.subprocess, "Popen", missing)
    with pytest.raises(af.AnalysisError, match="not runnable"):
        af.decode_excerpt(tmp_path / "a.flac", None)


class EndlessStream:
    """stdout of an ffmpeg that never stops writing."""

    def __init__(self):
        self.read_bytes = 0

    def readinto(self, view):
        view[:] = b"\0" * len(view)
        self.read_bytes += len(view)
        return len(view)

    def close(self):
        pass


def test_decode_reads_a_bounded_amount_and_kills_ffmpeg(monkeypatch, tmp_path):
    stream = EndlessStream()
    proc = FakeProc(stream=stream)
    monkeypatch.setattr(af.subprocess, "Popen", lambda *a, **k: proc)
    out = af.decode_excerpt(tmp_path / "a.flac", None)
    assert proc.killed
    assert len(out[0]) == af.MAX_DECODE_SECONDS * SR
    assert stream.read_bytes < (af.MAX_DECODE_SECONDS + 5) * SR * 4


def test_decode_stops_when_asked(monkeypatch, tmp_path):
    stop = threading.Event()

    class StopAfterFirstRead(EndlessStream):
        def readinto(self, view):
            stop.set()
            return super().readinto(view)

    proc = FakeProc(stream=StopAfterFirstRead())
    monkeypatch.setattr(af.subprocess, "Popen", lambda *a, **k: proc)
    with pytest.raises(af.AnalysisError, match="stopped"):
        af.decode_excerpt(tmp_path / "a.flac", None, stop)
    assert proc.killed


def test_decode_times_out_a_hung_ffmpeg(monkeypatch, tmp_path):
    class Hung:
        def __init__(self):
            self._released = threading.Event()

        def readinto(self, view):
            self._released.wait(5)
            return 0

        def unblock(self):
            self._released.set()

        def close(self):
            pass

    proc = FakeProc(stream=Hung())
    monkeypatch.setattr(af.subprocess, "Popen", lambda *a, **k: proc)
    monkeypatch.setattr(af, "transcode_timeout", lambda seconds: 0.05)
    with pytest.raises(af.AnalysisError, match="timed out"):
        af.decode_excerpt(tmp_path / "a.flac", None)
    assert proc.killed


def test_extract_features_never_raises(monkeypatch, tmp_path):
    def boom(*a, **k):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(af, "decode_excerpt", boom)
    assert af.extract_features(tmp_path / "a.flac") is None
    monkeypatch.setattr(af, "decode_excerpt", lambda *a: [np.zeros(SR * 20, np.float32)])
    assert af.extract_features(tmp_path / "a.flac") is None  # silent
    with pytest.raises(af.AnalysisError, match="too short or silent"):
        af.analyse_file(tmp_path / "a.flac")
    monkeypatch.setattr(af, "decode_excerpt", lambda *a: [np.zeros(0, np.float32)])
    with pytest.raises(af.AnalysisError, match="no audio"):
        af.analyse_file(tmp_path / "a.flac")


def test_a_tagged_length_longer_than_the_audio_reads_from_the_start(monkeypatch, tmp_path):
    calls = []

    def decode(path, duration, stop=None):
        calls.append(duration)
        if duration is not None:  # the late windows seeked past the end
            return [click_track(120, 30), np.zeros(10, np.float32), np.zeros(10, np.float32)]
        return [click_track(120, 60)]

    monkeypatch.setattr(af, "decode_excerpt", decode)
    assert af.analyse_file(tmp_path / "a.flac", 600.0).analysed_seconds == 60.0
    assert calls == [600.0, None]


# --- the analysis ---


@pytest.mark.parametrize("bpm", [90, 120, 128, 150])
def test_tempo_of_a_click_track(bpm):
    features = af.compute_features([click_track(bpm, 30, seed=s) for s in range(3)])
    assert features is not None
    # Right within 3 %, or a whole octave off.
    ratio = features.tempo_bpm / bpm
    assert min(abs(ratio * m - 1) for m in (0.5, 1, 2)) < 0.03, features.tempo_bpm
    assert features["tempo_strength"] > 0.5


def test_louder_and_brighter_order_correctly():
    noise = lowpassed_noise(4000)
    quiet = af.compute_features([noise * 0.1])
    loud = af.compute_features([noise])
    assert quiet is not None and loud is not None
    assert loud.loudness_db - quiet.loudness_db == pytest.approx(20, abs=0.5)
    centroids = [af.compute_features([lowpassed_noise(c)]).brightness_hz for c in (500, 2000, 8000)]  # type: ignore[union-attr]
    assert centroids == sorted(centroids)
    assert centroids[0] < 500 < centroids[2]


def test_a_major_chord_finds_its_key_and_mode():
    t = np.arange(30 * SR) / SR
    # G major triad: G, B, D with a little octave doubling.
    chord = sum(np.sin(2 * np.pi * f * t) for f in (196.0, 246.94, 293.66, 392.0))
    features = af.compute_features([(0.2 * chord).astype(np.float32)])
    assert features is not None
    assert (features.key, features.mode) == (7, 1)
    # Key-relative: the tonic is bin 0.
    assert features["chroma_rel_0"] == max(features.values[17:29])


def test_analysis_is_deterministic_and_round_trips_as_a_blob():
    windows = [click_track(110, 30, seed=s) for s in range(3)]
    first, second = af.compute_features(windows), af.compute_features(windows)
    assert first is not None and second is not None
    assert first.to_blob() == second.to_blob()
    assert len(first.to_blob()) == af.FEATURE_COUNT * 4 == 180
    restored = af.FeatureVector.values_from_blob(first.to_blob())
    assert restored is not None and np.array_equal(restored, first.values)
    assert af.FeatureVector.values_from_blob(b"\0" * 12) is None
    assert len(af.FEATURE_NAMES) == af.FEATURE_COUNT == len(set(af.FEATURE_NAMES))


def test_too_short_or_silent_audio_has_no_features():
    assert af.compute_features([click_track(120, 5)]) is None  # under MIN_AUDIBLE_SECONDS
    assert af.compute_features([np.zeros(30 * SR, np.float32)]) is None
    assert af.compute_features([np.zeros(100, np.float32)]) is None
    assert af.compute_features([]) is None


# --- with the real ffmpeg ---


def _generate(path: Path, expression: str, seconds: float, *extra: str) -> None:
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-f", "lavfi", "-i", f"aevalsrc='{expression}':s=44100:d={seconds}",
            "-ac", "2", *extra, str(path),
        ],
        check=True,
        timeout=60,
    )


@needs_ffmpeg
def test_real_ffmpeg_windows_tempo_and_formats(tmp_path):
    beat = "0.6*sin(2*PI*55*t)*exp(-25*mod(t,0.5))+0.1*(random(0)-0.5)*exp(-60*mod(t+0.25,0.5))+0.05*sin(2*PI*220*t)"
    flac, mp3 = tmp_path / "beat.flac", tmp_path / "beat.mp3"
    _generate(flac, beat, 130, "-sample_fmt", "s16")
    subprocess.run(["ffmpeg", "-loglevel", "error", "-nostdin", "-y", "-i", str(flac), "-b:a", "128k", str(mp3)], check=True, timeout=60)
    a = af.extract_features(flac, 130.0)  # three windows
    b = af.extract_features(flac, 130.0)
    c = af.extract_features(mp3, 130.0)
    whole = af.extract_features(flac, None)  # from the start
    assert a is not None and b is not None and c is not None and whole is not None
    assert a.to_blob() == b.to_blob()  # deterministic
    assert a.analysed_seconds == 90.0
    assert a.tempo_bpm == pytest.approx(120, rel=0.03)
    assert c.tempo_bpm == pytest.approx(120, rel=0.03)
    assert abs(a.loudness_db - c.loudness_db) < 1.0
    assert abs(a.brightness_hz - c.brightness_hz) / a.brightness_hz < 0.05


@needs_ffmpeg
def test_real_ffmpeg_failures_are_none(tmp_path):
    broken = tmp_path / "broken.flac"
    broken.write_bytes(b"fLaC" + b"\0" * 1000)
    assert af.extract_features(broken, 200.0) is None
    with pytest.raises(af.AnalysisError):
        af.analyse_file(tmp_path / "missing.flac", None)
    short = tmp_path / "short.wav"
    _generate(short, "0.5*sin(2*PI*440*t)", 3)
    with pytest.raises(af.AnalysisError, match="too short"):
        af.analyse_file(short, 3.0)
