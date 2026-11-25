"""HLS transcoder internals, driven with fake ffmpeg processes and a fake clock
so no real encoder (or real waiting) is involved."""

from __future__ import annotations

import io
import signal
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app import hls
from app.config import settings

_REAL_ENSURE = hls._ensure_janitor


class FakeProc:
    def __init__(self, returncode=None, stderr: bytes | None = b"", signal_error=None, wait_error=None):
        self.returncode = returncode
        self.stderr = io.BytesIO(stderr) if stderr is not None else None
        self.signals: list[int] = []
        self._signal_error = signal_error
        self._wait_error = wait_error

    def poll(self):
        return self.returncode

    def send_signal(self, sig):
        if self._signal_error:
            raise self._signal_error
        self.signals.append(sig)
        if sig == signal.SIGKILL:
            self.returncode = -9

    def wait(self, timeout=None):
        if self._wait_error:
            raise self._wait_error


class Stop(Exception):
    pass


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_path", tmp_path / "data" / "app.db")
    monkeypatch.setattr(settings, "hwaccel", "none")
    monkeypatch.setattr(hls, "_sessions", {})
    monkeypatch.setattr(hls, "_vaapi_ok", None)
    monkeypatch.setattr(hls, "_ensure_janitor", lambda: None)


def _session(tmp_path, n=1, **kwargs) -> hls._Session:
    directory = tmp_path / f"s{n}"
    directory.mkdir(exist_ok=True)
    session = hls._Session(key=(n, 1, 0), directory=directory, **kwargs)
    hls._sessions[session.key] = session
    return session


# --- VAAPI detection ---------------------------------------------------


def test_vaapi_usable_is_off_for_software_modes_and_cached_otherwise(monkeypatch):
    assert hls.vaapi_usable() is False  # hwaccel == "none"

    monkeypatch.setattr(settings, "hwaccel", "auto")
    calls = []
    monkeypatch.setattr(hls, "_test_vaapi", lambda: calls.append(1) or True)
    assert hls.vaapi_usable() is True
    assert hls.vaapi_usable() is True
    assert calls == [1]

    monkeypatch.setattr(hls, "_vaapi_ok", None)
    monkeypatch.setattr(hls, "_test_vaapi", lambda: False)
    assert hls.vaapi_usable() is False


def test_test_vaapi_checks_device_then_runs_a_test_encode(tmp_path, monkeypatch):
    monkeypatch.setattr(hls, "VAAPI_DEVICE", str(tmp_path / "missing"))
    assert hls._test_vaapi() is False

    device = tmp_path / "renderD128"
    device.write_text("")
    monkeypatch.setattr(hls, "VAAPI_DEVICE", str(device))
    monkeypatch.setattr(hls.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0))
    assert hls._test_vaapi() is True

    def fail(*_a, **_k):
        raise subprocess.CalledProcessError(1, "ffmpeg")

    monkeypatch.setattr(hls.subprocess, "run", fail)
    assert hls._test_vaapi() is False


# --- command building --------------------------------------------------


def test_encode_command_variants(tmp_path):
    path = Path("/m/v.mkv")
    hw = hls._encode_command(path, tmp_path, 2, True)
    assert "h264_vaapi" in hw and "-init_hw_device" in hw and hw[hw.index("-ss") + 1] == "12"
    sw = hls._encode_command(path, tmp_path, 0, False)
    assert "libx264" in sw and "-init_hw_device" not in sw
    audio = hls._encode_command(path, tmp_path, 1, False, audio_kbps=96)
    assert "-vn" in audio and "96k" in audio and "libx264" not in audio


# --- _Session process control ------------------------------------------


def test_session_signal_swallows_dead_process_errors(tmp_path):
    session = _session(tmp_path)
    session.proc = FakeProc(signal_error=ProcessLookupError())
    session.signal(signal.SIGSTOP)  # must not raise
    session.pause()
    assert session.stopped is True
    session.resume()
    assert session.stopped is False


def test_session_pause_resume_and_kill_signal_the_process(tmp_path):
    session = _session(tmp_path)
    proc = FakeProc()
    session.proc = proc
    session.pause()
    session.pause()  # already stopped: no second signal
    session.kill()
    assert proc.signals == [signal.SIGSTOP, signal.SIGCONT, signal.SIGKILL]
    assert session.proc is None and session.stopped is False


def test_session_kill_tolerates_a_process_that_will_not_exit(tmp_path):
    session = _session(tmp_path)
    session.proc = FakeProc(wait_error=subprocess.TimeoutExpired("ffmpeg", 5))
    session.kill()
    assert session.proc is None


# --- janitor and cache cap ---------------------------------------------


def _touch(directory: Path, *names: str, size: int = 10) -> None:
    for name in names:
        (directory / name).write_bytes(b"x" * size)


def test_janitor_drops_idles_pauses_ahead_encoders_and_prunes_old_segments(tmp_path, monkeypatch):
    now = time.monotonic()
    idle = _session(tmp_path, 1)
    idle.last_request = now - hls.IDLE_SECONDS - 5
    idle.proc = FakeProc()

    sleepy = _session(tmp_path, 2)
    sleepy.last_request = now - hls.ENCODER_IDLE_SECONDS - 5
    sleepy.proc = FakeProc()

    ahead = _session(tmp_path, 3)
    ahead.last_request = time.monotonic() + 1000
    ahead.proc = FakeProc()
    ahead.start_index = 0
    ahead.last_index = 200
    _touch(ahead.directory, *[f"{i}.ts" for i in range(0, 3)])  # old: pruned
    _touch(ahead.directory, "notes.txt", "x.ts")

    close = _session(tmp_path, 4)
    close.last_request = time.monotonic() + 1000
    close.proc = FakeProc()
    close.stopped = True

    # A session whose directory vanished: the prune pass copes.
    gone = _session(tmp_path, 5)
    gone.last_request = time.monotonic() + 1000
    gone.directory.rmdir()

    # Segments just ahead of the playhead -> pause.
    ahead2 = _session(tmp_path, 6)
    ahead2.last_request = time.monotonic() + 1000
    ahead2.proc = FakeProc()
    _touch(ahead2.directory, *[f"{i}.ts" for i in range(hls.AHEAD_SEGMENTS + 3)])

    ticks = []

    def fake_wait(_seconds):
        ticks.append(1)
        if len(ticks) > 10:
            raise Stop
        return False

    with pytest.raises(Stop):
        hls._janitor(SimpleNamespace(wait=fake_wait))

    assert idle.key not in hls._sessions and not idle.directory.exists()
    assert sleepy.proc is None  # encoder stopped, session kept
    assert ahead2.stopped is True
    assert close.stopped is False  # resumed
    assert not (ahead.directory / "0.ts").exists()
    assert (ahead.directory / "notes.txt").exists() and (ahead.directory / "x.ts").exists()


def test_size_cap_tolerates_vanishing_directories_and_files(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "hls_cache_max_bytes", 25)
    missing = _session(tmp_path, 1)
    missing.directory.rmdir()
    live = _session(tmp_path, 2)
    _touch(live.directory, "0.ts", "1.ts", "2.ts", "3.ts", "other.txt")

    real_unlink = Path.unlink

    def flaky_unlink(self, *a, **k):
        if self.name == "0.ts":
            raise OSError("busy")
        return real_unlink(self, *a, **k)

    monkeypatch.setattr(Path, "unlink", flaky_unlink)
    hls._enforce_size_cap([missing, live])
    assert (live.directory / "0.ts").exists()
    assert not (live.directory / "1.ts").exists()
    assert (live.directory / "3.ts").exists()  # back under the cap: stop deleting


def test_ensure_janitor_starts_one_thread(monkeypatch):
    started = []
    monkeypatch.setattr(hls, "_janitor_started", False)
    monkeypatch.setattr(hls, "spawn", lambda name, target, *a: started.append(name))
    _REAL_ENSURE()
    _REAL_ENSURE()
    assert started == ["hls-janitor"]


# --- sessions ----------------------------------------------------------


def test_get_session_reuses_and_evicts_the_longest_idle(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "hls_max_sessions", 1)
    evictions = []
    monkeypatch.setattr(hls, "spawn", lambda name, target, *a: evictions.append((target, a)))
    first = hls._get_session(1, 1)
    assert hls._get_session(1, 1) is first
    first.last_request -= hls.ACTIVE_SESSION_SECONDS + 1  # idle: may be taken over
    second = hls._get_session(2, 1, audio_kbps=96)
    assert second.directory.name == "2_1_audio96" and second.use_hw is False
    for target, args in evictions:  # the eviction thread, once the lock is free
        target(*args)
    assert first.key not in hls._sessions and not first.directory.exists()
    assert first.dead is True


def test_start_records_probed_codecs_and_launches_ffmpeg(tmp_path, monkeypatch):
    session = _session(tmp_path)
    launched = []
    monkeypatch.setattr(hls.subprocess, "Popen", lambda cmd, **kw: launched.append(cmd) or FakeProc())
    monkeypatch.setattr(hls, "probe_av_codecs", lambda _p: None)
    hls._start(session, Path("/m/v.mkv"), 3)
    assert session.codecs == "unknown" and session.start_index == 3

    session.codecs = None
    monkeypatch.setattr(hls, "probe_av_codecs", lambda _p: ("hevc", "yuv420p10le", None))
    hls._start(session, Path("/m/v.mkv"), 0)
    assert session.codecs == "hevc/yuv420p10le/None"
    assert len(launched) == 2


# --- get_segment --------------------------------------------------------


class Clock:
    """Fake time module: every sleep advances the clock and may run a hook."""

    def __init__(self, step=0.1, hook=None):
        self.now = 1000.0
        self.step = step
        self.hook = hook

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += self.step if self.step is not None else seconds
        if self.hook:
            self.hook()


def _install_clock(monkeypatch, **kwargs) -> Clock:
    clock = Clock(**kwargs)
    monkeypatch.setattr(hls, "time", SimpleNamespace(sleep=clock.sleep, monotonic=clock.monotonic))
    return clock


def test_get_segment_logs_slow_segments(tmp_path, monkeypatch, caplog):
    clock = _install_clock(monkeypatch)
    session = hls._get_session(1, 1)
    (session.directory / "0.ts").write_bytes(b"x")

    original = clock.monotonic
    calls = []

    def ticking():
        calls.append(1)
        return original() + 10 * len(calls)

    monkeypatch.setattr(hls.time, "monotonic", ticking)
    with caplog.at_level("INFO", logger="app.hls"):
        assert hls.get_segment(1, 1, Path("/m/v.mkv"), 0, 30.0) == session.directory / "0.ts"
    assert "HLS segment slow" in caplog.text


def test_get_segment_rejects_out_of_range_indexes():
    with pytest.raises(HTTPException) as exc:
        hls.get_segment(1, 1, Path("/m/v.mkv"), 99, 12.0)
    assert exc.value.status_code == 404


def test_get_segment_times_out_when_nothing_is_produced(tmp_path, monkeypatch):
    _install_clock(monkeypatch, step=50)
    starts = []
    monkeypatch.setattr(hls, "_start", lambda session, path, index: starts.append(index))
    with pytest.raises(HTTPException) as exc:
        hls.get_segment(1, 1, Path("/m/v.mkv"), 0, 30.0)
    assert exc.value.status_code == 504
    assert starts


def test_get_segment_resumes_a_paused_encoder_until_the_segment_lands(tmp_path, monkeypatch):
    session = hls._get_session(1, 1)
    proc = FakeProc()
    session.proc = proc
    session.stopped = True
    target = session.directory / "1.ts"
    _install_clock(monkeypatch, hook=lambda: target.write_bytes(b"x"))
    assert hls.get_segment(1, 1, Path("/m/v.mkv"), 1, 30.0) == target
    assert proc.signals == [signal.SIGCONT]


def test_get_segment_falls_back_to_software_when_vaapi_dies(tmp_path, monkeypatch, caplog):
    session = hls._get_session(1, 1)
    session.use_hw = True
    session.proc = FakeProc(returncode=1)
    (session.directory / hls.STDERR_LOG).write_bytes(b"x" * 10000 + b"vaapi exploded")
    target = session.directory / "0.ts"
    starts = []

    def fake_start(sess, path, index):
        starts.append(sess.use_hw)
        target.write_bytes(b"x")

    monkeypatch.setattr(hls, "_start", fake_start)
    _install_clock(monkeypatch)
    with caplog.at_level("WARNING", logger="app.hls"):
        assert hls.get_segment(1, 1, Path("/m/v.mkv"), 0, 30.0) == target
    assert starts == [False]
    assert "falling back to software" in caplog.text
    assert "vaapi exploded" in caplog.text  # the tail of the log


def test_get_segment_reports_a_failed_software_transcode(tmp_path, monkeypatch):
    session = hls._get_session(1, 1)
    session.use_hw = False
    session.proc = FakeProc(returncode=1, stderr=None)
    _install_clock(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        hls.get_segment(1, 1, Path("/m/v.mkv"), 0, 30.0)
    assert exc.value.status_code == 500
    assert session.proc is None


def test_get_segment_survives_a_missing_encoder_log(tmp_path, monkeypatch):
    session = hls._get_session(1, 1)
    session.use_hw = False
    session.proc = FakeProc(returncode=1)
    assert not (session.directory / hls.STDERR_LOG).exists()
    _install_clock(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        hls.get_segment(1, 1, Path("/m/v.mkv"), 0, 30.0)
    assert exc.value.status_code == 500


def test_get_segment_gives_way_to_a_newer_seek(tmp_path, monkeypatch, caplog):
    def fake_start(session, path, index):
        # Another request (a seek elsewhere) takes over the session.
        session.latest_token = 10**9
        session.latest_index = index + 5

    monkeypatch.setattr(hls, "_start", fake_start)
    _install_clock(monkeypatch)
    with caplog.at_level("INFO", logger="app.hls"), pytest.raises(HTTPException) as exc:
        hls.get_segment(1, 1, Path("/m/v.mkv"), 0, 60.0)
    assert exc.value.status_code == 409
    assert "superseded" in caplog.text


def test_explicit_vaapi_that_is_unusable_logs_an_error(monkeypatch, caplog):
    monkeypatch.setattr(settings, "hwaccel", "vaapi")
    monkeypatch.setattr(hls, "_test_vaapi", lambda: False)
    with caplog.at_level("ERROR", logger="app.hls"):
        assert hls.vaapi_usable() is False
    assert "ATRIOLYRIC_HWACCEL=vaapi" in caplog.text


# --- stderr to a file, the cap, eviction races ----------------------------


def test_encoder_stderr_goes_to_a_file_so_a_chatty_ffmpeg_cannot_block(tmp_path, monkeypatch):
    """A real child that writes 200 KB to stderr before producing its segment.
    Through an unread pipe it would block for good after 64 KB (and every
    segment request 504); through the session's log file it finishes."""
    import sys

    script = (
        "import sys, pathlib; sys.stderr.write('decode error\\n' * 16000); sys.stderr.flush();"
        "out = pathlib.Path(sys.argv[1]); (out / '0.tmp').write_bytes(b'seg'); (out / '0.tmp').rename(out / '0.ts')"
    )
    monkeypatch.setattr(
        hls, "_encode_command", lambda _f, out_dir, *_a: [sys.executable, "-c", script, str(out_dir)]
    )
    monkeypatch.setattr(hls, "probe_av_codecs", lambda _p: None)
    monkeypatch.setattr(hls, "SEGMENT_WAIT_SECONDS", 20)
    path = hls.get_segment(1, 1, Path("/m/v.mkv"), 0, 30.0)
    session = hls._sessions[(1, 1, 0)]
    session.proc.wait(10)
    log = session.directory / hls.STDERR_LOG
    assert path.read_bytes() == b"seg"
    assert log.stat().st_size >= 200_000
    assert session.stderr_tail().endswith("decode error\n")
    assert len(session.stderr_tail()) <= hls.STDERR_TAIL_BYTES
    hls._drop(session)
    assert not log.exists()  # deleted with the session


def test_each_start_truncates_the_encoder_log(tmp_path, monkeypatch):
    session = _session(tmp_path)
    (session.directory / hls.STDERR_LOG).write_bytes(b"old run")
    monkeypatch.setattr(hls.subprocess, "Popen", lambda cmd, **kw: FakeProc())
    monkeypatch.setattr(hls, "probe_av_codecs", lambda _p: None)
    hls._start(session, Path("/m/v.mkv"), 0)
    assert (session.directory / hls.STDERR_LOG).read_bytes() == b""


def test_janitor_empties_an_oversized_encoder_log(tmp_path, monkeypatch):
    session = _session(tmp_path)
    session.last_request = time.monotonic()
    log = session.directory / hls.STDERR_LOG
    log.write_bytes(b"x" * (hls.STDERR_LOG_MAX_BYTES + 1))
    hls._janitor_pass(1)
    assert log.stat().st_size == 0


def test_cap_returns_503_instead_of_evicting_an_active_viewer(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "hls_max_sessions", 2)
    monkeypatch.setattr(hls, "spawn", lambda name, target, *a: pytest.fail("evicted an active session"))
    first = hls._get_session(1, 1)
    hls._get_session(2, 2)
    first.last_request -= 10  # requested 10 s ago: still active
    with pytest.raises(HTTPException) as exc:
        hls._get_session(3, 3)
    assert exc.value.status_code == 503
    assert exc.value.headers == {"Retry-After": str(hls.ACTIVE_SESSION_SECONDS - 10)}
    assert len(hls._sessions) == 2 and first.dead is False


def test_a_fifth_concurrent_viewer_gets_503_through_get_segment(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "hls_max_sessions", 4)
    for viewer in range(4):
        hls._get_session(viewer, viewer)
    with pytest.raises(HTTPException) as exc:
        hls.get_segment(9, 9, Path("/m/v.mkv"), 0, 30.0)
    assert exc.value.status_code == 503 and "Retry-After" in (exc.value.headers or {})


def test_vaapi_self_test_does_not_hold_the_global_session_lock(tmp_path, monkeypatch):
    held = []
    monkeypatch.setattr(hls, "vaapi_usable", lambda: held.append(hls._sessions_lock.locked()) or False)
    hls._get_session(1, 1)
    assert held == [False]


def test_start_on_a_dead_session_launches_nothing(tmp_path, monkeypatch):
    session = _session(tmp_path)
    session.dead = True
    monkeypatch.setattr(hls.subprocess, "Popen", lambda *a, **k: pytest.fail("orphan ffmpeg"))
    hls._start(session, Path("/m/v.mkv"), 0)
    assert session.proc is None


def test_eviction_waits_for_an_in_flight_start_and_kills_its_encoder(tmp_path):
    """_retire takes the session lock: a start already holding it finishes,
    then its encoder is killed; nothing runs on in a deleted directory."""
    import threading

    session = _session(tmp_path)
    proc = FakeProc()
    session.lock.acquire()  # an in-flight _start
    del hls._sessions[session.key]
    retired = threading.Thread(target=hls._retire, args=(session,))
    retired.start()
    time.sleep(0.05)
    assert retired.is_alive() and session.dead is False  # waiting for the lock
    session.proc = proc  # the start launched its encoder
    session.lock.release()
    retired.join(5)
    assert session.dead is True and signal.SIGKILL in proc.signals
    assert not session.directory.exists()


def test_retire_keeps_a_directory_a_returning_viewer_already_reuses(tmp_path):
    old = _session(tmp_path)
    del hls._sessions[old.key]
    new = hls._Session(key=old.key, directory=old.directory)
    hls._sessions[new.key] = new
    hls._retire(old)
    assert old.directory.exists()


def test_get_segment_looks_up_again_when_its_session_died_meanwhile(tmp_path, monkeypatch):
    dead = hls._Session(key=(1, 1, 0), directory=tmp_path / "dead", dead=True)
    fresh = _session(tmp_path, 1)
    (fresh.directory / "0.ts").write_bytes(b"x")
    lookups = iter([dead, fresh])
    monkeypatch.setattr(hls, "_get_session", lambda *a: next(lookups))
    assert hls.get_segment(1, 1, Path("/m/v.mkv"), 0, 30.0) == fresh.directory / "0.ts"


def test_kill_still_kills_when_proc_is_cleared_concurrently(tmp_path):
    session = _session(tmp_path)

    class Racing(FakeProc):
        def poll(self):
            session.proc = None  # another thread cleared it meanwhile
            return self.returncode

    proc = Racing()
    session.proc = proc
    session.kill()
    assert proc.signals == [signal.SIGKILL]


def test_janitor_survives_a_failing_pass(tmp_path, monkeypatch, caplog):
    passes = []

    def flaky(tick):
        passes.append(tick)
        if tick == 1:
            raise RuntimeError("boom")

    def fake_wait(_seconds):
        if len(passes) >= 3:
            raise Stop
        return False

    monkeypatch.setattr(hls, "_janitor_pass", flaky)
    with caplog.at_level("ERROR", logger="app.hls"), pytest.raises(Stop):
        hls._janitor(SimpleNamespace(wait=fake_wait))
    assert passes == [1, 2, 3]
    assert "HLS janitor pass failed" in caplog.text


def test_an_empty_segment_is_no_such_segment(tmp_path, monkeypatch):
    session = hls._get_session(1, 1)
    (session.directory / "4.ts").write_bytes(b"")
    with pytest.raises(HTTPException) as exc:
        hls.get_segment(1, 1, Path("/m/v.mkv"), 4, 30.0)
    assert exc.value.status_code == 404


def test_shutdown_stops_the_janitor_thread(monkeypatch):
    """Otherwise the lifespan's join_all would wait its full timeout on it."""
    monkeypatch.setattr(hls, "_janitor_started", False)
    monkeypatch.setattr(hls, "_ensure_janitor", _REAL_ENSURE)
    threads = []
    monkeypatch.setattr(hls, "spawn", lambda name, target, *a: threads.append(
        __import__("threading").Thread(target=target, args=a, daemon=True)) or threads[-1].start())
    hls._ensure_janitor()
    hls.shutdown_sessions()
    threads[0].join(3)
    assert not threads[0].is_alive() and hls._janitor_started is False
