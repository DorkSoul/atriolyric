from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time

import pytest

from app import renditions
from app.config import settings
from app.models import LibraryRoot, MediaItem, MediaKind, User
from app.security import hash_password
from tests.conftest import login_as

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg not installed",
)


@pytest.fixture(autouse=True)
def _isolated_renditions(tmp_path, monkeypatch):
    """Renditions go to this test's own folder, in software, and no worker,
    failure or probe result outlives the test."""
    monkeypatch.setattr(
        type(settings), "rendition_dir", property(lambda _self: tmp_path / "renditions")
    )
    monkeypatch.setattr(settings, "hwaccel", "none")
    renditions._probe_height.cache_clear()
    yield
    renditions.shutdown_renditions()
    with renditions._lock:
        renditions._failures.clear()
    renditions._probe_height.cache_clear()


def _video(db_session, library_dir, filename: str = "clip.mkv", **fields) -> MediaItem:
    root = db_session.query(LibraryRoot).first()
    if root is None:
        root = LibraryRoot(path=str(library_dir), display_name="Lib")
        db_session.add(root)
        db_session.commit()
        db_session.refresh(root)
    path = library_dir / filename
    if not path.exists():
        path.write_bytes(b"source-bytes")
    values = {"kind": MediaKind.video, "title": "Clip", "artist": "Band", "duration_seconds": 10.0}
    item = MediaItem(library_root_id=root.id, relative_path=filename, **{**values, **fields})
    db_session.add(item)
    db_session.commit()
    db_session.refresh(item)
    return item


class FakeEncoder:
    """Stands in for the ffmpeg run: records what is being encoded and
    holds each job until the test lets it go."""

    def __init__(self, monkeypatch, succeed: bool = True, hold: bool = False):
        self.succeed = succeed
        self.calls: list[tuple[int, int]] = []
        self.running = 0
        self.most_at_once = 0
        self.release = threading.Event()
        if not hold:
            self.release.set()
        monkeypatch.setattr(renditions, "_transcode", self)

    def __call__(self, job, tmp, stop) -> bool:
        self.calls.append((job.item_id, job.quality))
        self.running += 1
        self.most_at_once = max(self.most_at_once, self.running)
        try:
            while not self.release.is_set() and not stop.is_set():
                time.sleep(0.01)
            if stop.is_set() or not self.succeed:
                return False
            tmp.write_bytes(b"x" * 100)
            return True
        finally:
            self.running -= 1


def _wait(predicate, seconds: float = 10.0) -> None:
    deadline = time.monotonic() + seconds
    while not predicate():
        assert time.monotonic() < deadline, "timed out waiting"
        time.sleep(0.01)


def _status(client, item_id: int, quality: int = 720) -> str:
    resp = client.get(f"/api/download/video/{item_id}/renditions/{quality}")
    assert resp.status_code == 200, resp.text
    return resp.json()["status"]


@pytest.fixture()
def height(monkeypatch):
    """Sets what the source's height is said to be (no ffprobe)."""
    box = {"value": 1080}
    monkeypatch.setattr(renditions, "source_height", lambda _path: box["value"])
    return box


# --- levels -------------------------------------------------------------------


def test_only_levels_below_the_source_are_offered():
    assert renditions.offered_levels(2160) == [1080, 720, 480]
    assert renditions.offered_levels(1080) == [720, 480]
    assert renditions.offered_levels(720) == [480]
    assert renditions.offered_levels(480) == []
    assert renditions.offered_levels(None) == [1080, 720, 480]
    assert renditions.parse_quality("720") == 720
    assert renditions.parse_quality("721") is None
    assert renditions.parse_quality("720p") is None


def test_listing_never_upscales(client, db_session, library_dir, height):
    item = _video(db_session, library_dir)
    height["value"] = 720
    body = client.get(f"/api/download/video/{item.id}/renditions").json()
    assert body == {
        "source_height": 720,
        "source_size": len(b"source-bytes"),
        "renditions": [
            {
                "quality": "480",
                "status": "none",
                "progress": None,
                "size": None,
                "url": f"/api/download/video/{item.id}/renditions/480/file",
            }
        ],
    }
    for method, suffix in (("post", ""), ("get", ""), ("get", "/file")):
        url = f"/api/download/video/{item.id}/renditions/720{suffix}"
        assert getattr(client, method)(url).status_code == 404
    assert client.get(f"/api/download/video/{item.id}/renditions/abc").status_code == 404

    height["value"] = None
    body = client.get(f"/api/download/video/{item.id}/renditions").json()
    assert body["source_height"] is None
    assert [r["quality"] for r in body["renditions"]] == ["1080", "720", "480"]


def test_access_rules(client, db_session, library_dir, session_factory, height):
    item = _video(db_session, library_dir)
    track = _video(db_session, library_dir, "song.wav", kind=MediaKind.track)
    gone = _video(db_session, library_dir, "gone.mkv")
    (library_dir / "gone.mkv").unlink()

    def all_four(who, item_id: int) -> list[int]:
        base = f"/api/download/video/{item_id}/renditions"
        return [
            who.get(base).status_code,
            who.post(f"{base}/720").status_code,
            who.get(f"{base}/720").status_code,
            who.get(f"{base}/720/file").status_code,
        ]

    assert all_four(client, item.id) == [200, 200, 200, 409]
    assert all_four(client, 9999) == [404] * 4
    assert all_four(client, gone.id) == [404] * 4
    assert all_four(client, track.id) == [404] * 4
    assert client.get(f"/api/download/video/{track.id}/renditions").json()["detail"] == "Not a video"
    assert client.get("/api/download/video/abc/renditions").status_code == 422

    db_session.query(LibraryRoot).update({LibraryRoot.is_shared: False})
    db_session.add(User(username="plain", password_hash=hash_password("plainpass")))
    db_session.commit()
    plain = login_as(session_factory, client, "plain", "plainpass")
    assert all_four(plain, item.id) == [404] * 4

    client.cookies.clear()
    assert client.get(f"/api/download/video/{item.id}/renditions").status_code == 401


# --- the job's life -----------------------------------------------------------


def test_states_from_none_to_ready_and_the_file(client, db_session, library_dir, height, monkeypatch):
    item = _video(db_session, library_dir, artist="Ba/nd", title="Clip: one")
    encoder = FakeEncoder(monkeypatch, hold=True)
    base = f"/api/download/video/{item.id}/renditions/720"

    assert _status(client, item.id) == "none"
    assert encoder.calls == []  # a status request starts nothing
    assert client.get(f"{base}/file").status_code == 409

    started = client.post(base).json()
    assert started["status"] in {"queued", "preparing"}
    assert started["url"] == f"{base}/file"
    _wait(lambda: _status(client, item.id) == "preparing")
    preparing = client.get(base).json()
    assert preparing["progress"] == 0.0 and preparing["size"] is None
    assert client.get(f"{base}/file").status_code == 409
    # A second POST joins the running job.
    assert client.post(base).json()["status"] == "preparing"

    encoder.release.set()
    _wait(lambda: _status(client, item.id) == "ready")
    assert encoder.calls == [(item.id, 720)]
    ready = client.post(base).json()
    assert ready == {"quality": "720", "status": "ready", "progress": None, "size": 100, "url": f"{base}/file"}
    assert encoder.calls == [(item.id, 720)]  # ready: nothing is encoded again
    listed = client.get(f"/api/download/video/{item.id}/renditions").json()["renditions"]
    assert [(r["quality"], r["status"]) for r in listed] == [("720", "ready"), ("480", "none")]

    resp = client.get(f"{base}/file")
    assert resp.status_code == 200
    assert resp.content == b"x" * 100
    assert resp.headers["content-type"] == "video/mp4"
    assert 'filename="Ba_nd - Clip_ one (720p).mp4"' in resp.headers["content-disposition"]
    part = client.get(f"{base}/file", headers={"Range": "bytes=10-19"})
    assert part.status_code == 206
    assert part.headers["content-range"] == "bytes 10-19/100"
    assert part.content == b"x" * 10
    # Nothing is left behind but the finished file.
    assert [p.name for p in settings.rendition_dir.iterdir()] == [f"{item.id}_720.mp4"]


def test_serving_marks_the_file_recently_used(client, db_session, library_dir, height, monkeypatch):
    item = _video(db_session, library_dir)
    os.utime(library_dir / "clip.mkv", (1000, 1000))
    FakeEncoder(monkeypatch)
    client.post(f"/api/download/video/{item.id}/renditions/720")
    _wait(lambda: _status(client, item.id) == "ready")
    path = renditions.rendition_path(item.id, 720)
    os.utime(path, (2000, 2000))
    assert client.get(f"/api/download/video/{item.id}/renditions/720/file").status_code == 200
    stat = path.stat()
    assert stat.st_atime > time.time() - 60
    assert stat.st_mtime == 2000  # the staleness clock is left alone


def test_a_failure_is_remembered_and_a_post_retries(client, db_session, library_dir, height, monkeypatch):
    item = _video(db_session, library_dir)
    encoder = FakeEncoder(monkeypatch, succeed=False)
    base = f"/api/download/video/{item.id}/renditions/480"
    client.post(base)
    _wait(lambda: _status(client, item.id, 480) == "failed")
    assert client.get(f"{base}/file").status_code == 409
    assert _status(client, item.id, 720) == "none"  # per level
    assert not list(settings.rendition_dir.iterdir())

    encoder.succeed = True
    assert client.post(base).json()["status"] in {"queued", "preparing", "ready"}
    _wait(lambda: _status(client, item.id, 480) == "ready")
    assert encoder.calls == [(item.id, 480), (item.id, 480)]


def test_failure_memory_expires_and_stays_bounded(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(renditions.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(renditions, "MAX_REMEMBERED_FAILURES", 2)
    with renditions._lock:
        for item_id in (1, 2, 3):
            renditions._remember_failure((item_id, 720, 0))
        assert list(renditions._failures) == [(2, 720, 0), (3, 720, 0)]
        assert renditions._failed_recently((3, 720, 0))
        clock[0] += renditions.FAILURE_MEMORY_SECONDS + 1
        assert not renditions._failed_recently((3, 720, 0))
        assert (3, 720, 0) not in renditions._failures


def test_a_crashing_job_fails_and_the_worker_carries_on(client, db_session, library_dir, height, monkeypatch):
    first = _video(db_session, library_dir, "a.mkv")
    second = _video(db_session, library_dir, "b.mkv")
    real = FakeEncoder(monkeypatch)

    def crash_on_first(job, tmp, stop) -> bool:
        if job.item_id == first.id:
            raise RuntimeError("boom")
        return real(job, tmp, stop)

    monkeypatch.setattr(renditions, "_transcode", crash_on_first)
    client.post(f"/api/download/video/{first.id}/renditions/720")
    client.post(f"/api/download/video/{second.id}/renditions/720")
    _wait(lambda: _status(client, second.id) == "ready")
    assert _status(client, first.id) == "failed"


def test_a_stale_rendition_is_rebuilt(client, db_session, library_dir, height, monkeypatch):
    item = _video(db_session, library_dir)
    source = library_dir / "clip.mkv"
    os.utime(source, (1000, 1000))
    encoder = FakeEncoder(monkeypatch)
    base = f"/api/download/video/{item.id}/renditions/720"
    client.post(base)
    _wait(lambda: _status(client, item.id) == "ready")

    # The source changes after the rendition was made.
    path = renditions.rendition_path(item.id, 720)
    os.utime(path, (2000, 2000))
    os.utime(source, (3000, 3000))
    assert _status(client, item.id) == "none"
    assert client.get(f"{base}/file").status_code == 409
    encoder.release.clear()
    client.post(base)
    _wait(lambda: _status(client, item.id) == "preparing")
    assert client.get(f"{base}/file").status_code == 409  # the old file is not served meanwhile
    encoder.release.set()
    _wait(lambda: _status(client, item.id) == "ready")
    assert encoder.calls == [(item.id, 720), (item.id, 720)]
    assert path.stat().st_mtime > 3000


def test_a_source_replaced_during_the_encode_discards_the_result(client, db_session, library_dir, height, monkeypatch):
    item = _video(db_session, library_dir)
    encoder = FakeEncoder(monkeypatch, hold=True)
    client.post(f"/api/download/video/{item.id}/renditions/720")
    _wait(lambda: _status(client, item.id) == "preparing")
    os.utime(library_dir / "clip.mkv", (5000, 5000))
    encoder.release.set()
    _wait(lambda: _status(client, item.id) == "none")
    assert not list(settings.rendition_dir.iterdir())


def test_jobs_run_one_at_a_time_in_order(client, db_session, library_dir, height, monkeypatch):
    items = [_video(db_session, library_dir, f"{n}.mkv") for n in range(3)]
    encoder = FakeEncoder(monkeypatch, hold=True)
    for item in items:
        client.post(f"/api/download/video/{item.id}/renditions/720")
    client.post(f"/api/download/video/{items[0].id}/renditions/480")
    _wait(lambda: _status(client, items[0].id) == "preparing")
    assert [_status(client, item.id) for item in items[1:]] == ["queued", "queued"]
    assert _status(client, items[0].id, 480) == "queued"
    queued = client.get(f"/api/download/video/{items[1].id}/renditions/720").json()
    assert queued["progress"] is None

    encoder.release.set()
    _wait(lambda: _status(client, items[0].id, 480) == "ready")
    assert encoder.calls == [(items[0].id, 720), (items[1].id, 720), (items[2].id, 720), (items[0].id, 480)]
    assert encoder.most_at_once == 1


def test_shutdown_stops_the_worker_and_drops_the_queue(client, db_session, library_dir, height, monkeypatch):
    items = [_video(db_session, library_dir, f"{n}.mkv") for n in range(2)]
    encoder = FakeEncoder(monkeypatch, hold=True)
    for item in items:
        client.post(f"/api/download/video/{item.id}/renditions/720")
    _wait(lambda: _status(client, items[0].id) == "preparing")
    worker = renditions._worker_thread
    assert worker is not None

    renditions.shutdown_renditions()
    assert not worker.is_alive()
    assert [_status(client, item.id) for item in items] == ["none", "none"]  # not "failed"
    assert not list(settings.rendition_dir.iterdir())

    # A later request starts a fresh worker.
    encoder.release.set()
    client.post(f"/api/download/video/{items[1].id}/renditions/720")
    _wait(lambda: _status(client, items[1].id) == "ready")


# --- the cache budget ---------------------------------------------------------


def test_least_recently_used_renditions_are_evicted_first():
    directory = settings.rendition_dir
    directory.mkdir()
    for name, used in (("1_720.mp4", 1000), ("2_720.mp4", 3000), ("3_480.mp4", 2000)):
        (directory / name).write_bytes(b"x" * 100)
        os.utime(directory / name, (used, 1000))
    writing = directory / ".4_720.mp4.abc.tmp"
    writing.write_bytes(b"x" * 1000)
    os.utime(writing, (1, 1))

    renditions.enforce_cache_budget(max_bytes=250)
    assert sorted(p.name for p in directory.iterdir()) == [".4_720.mp4.abc.tmp", "2_720.mp4", "3_480.mp4"]
    # The one just finished stays even when it is the oldest.
    renditions.enforce_cache_budget(max_bytes=100, keep=directory / "3_480.mp4")
    assert sorted(p.name for p in directory.iterdir()) == [".4_720.mp4.abc.tmp", "3_480.mp4"]
    renditions.enforce_cache_budget(max_bytes=0)
    assert [p.name for p in directory.iterdir()] == [".4_720.mp4.abc.tmp"]


def test_the_budget_is_enforced_when_a_rendition_finishes(client, db_session, library_dir, height, monkeypatch):
    monkeypatch.setattr(settings, "rendition_cache_max_bytes", 150)
    old, new = (_video(db_session, library_dir, f"{n}.mkv") for n in ("old", "new"))
    for name in ("old.mkv", "new.mkv"):
        os.utime(library_dir / name, (1000, 1000))
    FakeEncoder(monkeypatch)
    client.post(f"/api/download/video/{old.id}/renditions/720")
    _wait(lambda: _status(client, old.id) == "ready")
    os.utime(renditions.rendition_path(old.id, 720), (2000, 2000))
    client.post(f"/api/download/video/{new.id}/renditions/720")
    _wait(lambda: _status(client, new.id) == "ready")
    _wait(lambda: _status(client, old.id) == "none")
    assert [p.name for p in settings.rendition_dir.iterdir()] == [f"{new.id}_720.mp4"]


# --- the ffmpeg command -------------------------------------------------------


def test_command_is_one_mp4_scaled_and_capped(tmp_path):
    command = renditions.build_command(tmp_path / "in.mkv", tmp_path / "out.tmp", 720, use_hw=False)
    joined = " ".join(command)
    assert "-map 0:v:0 -map 0:a:0? -sn -dn" in joined
    assert "-vf scale=-2:trunc(min(ih\\,720)/2)*2" in joined
    assert "-c:v libx264 -preset veryfast -crf 23 -maxrate 3500k -bufsize 7000k -pix_fmt yuv420p" in joined
    assert "-c:a aac -b:a 192k -ac 2 -movflags +faststart -f mp4 -progress pipe:1" in joined
    assert command[-1] == str(tmp_path / "out.tmp")
    assert "-protocol_whitelist" in command

    hardware = " ".join(renditions.build_command(tmp_path / "in.mkv", tmp_path / "out.tmp", 1080, use_hw=True))
    assert "-init_hw_device vaapi=va:" in hardware
    assert "-vf scale=-2:trunc(min(ih\\,1080)/2)*2,format=nv12,hwupload" in hardware
    assert "-c:v h264_vaapi -b:v 4800k -maxrate 6000k" in hardware


def test_timeout_grows_with_the_video():
    assert renditions.encode_timeout(None) == renditions.MIN_TIMEOUT_SECONDS
    assert renditions.encode_timeout(1000) == 3000
    assert renditions.encode_timeout(1e6) == renditions.MAX_TIMEOUT_SECONDS


def test_a_failed_hardware_encode_is_retried_in_software(tmp_path, monkeypatch):
    runs = []

    def fake_run(job, command, stop) -> bool:
        runs.append("h264_vaapi" in command)
        return not runs[-1]

    monkeypatch.setattr(renditions, "vaapi_usable", lambda: True)
    monkeypatch.setattr(renditions, "_run_ffmpeg", fake_run)
    job = renditions._Job(1, 720, tmp_path / "in.mkv", 0, 10.0)
    assert renditions._transcode(job, tmp_path / "out.tmp", threading.Event())
    assert runs == [True, False]


def test_unknown_height_when_the_probe_cannot_answer(tmp_path, monkeypatch):
    source = tmp_path / "in.mkv"
    source.write_bytes(b"not a video")
    assert renditions.source_height(tmp_path / "missing.mkv") is None

    def timed_out(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("ffprobe", 30)

    monkeypatch.setattr(renditions.subprocess, "run", timed_out)
    assert renditions.source_height(source) is None
    monkeypatch.setattr(
        renditions.subprocess, "run",
        lambda *_a, **_k: subprocess.CompletedProcess([], 0, stdout=b'{"streams": [{"height": 544}]}'),
    )
    assert renditions.source_height(source) == 544  # the timeout was not remembered


# --- with the real ffmpeg -----------------------------------------------------


def _clip(path, size: str = "192x540", seconds: int = 2) -> None:
    subprocess.run(
        [
            "ffmpeg", "-y", "-f", "lavfi", "-i", f"testsrc=size={size}:rate=10:duration={seconds}",
            "-f", "lavfi", "-i", f"sine=duration={seconds}",
            "-f", "lavfi", "-i", f"sine=frequency=880:duration={seconds}",
            "-map", "0:v", "-map", "1:a", "-map", "2:a",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p10le",
            "-c:a", "aac", "-ac", "1", "-shortest", str(path),
        ],
        check=True,
        capture_output=True,
    )


def _probe(path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams", "-show_format", str(path)],
        check=True,
        capture_output=True,
    )
    return json.loads(out.stdout)


@needs_ffmpeg
def test_real_encode_makes_a_480p_h264_aac_mp4(client, db_session, library_dir):
    _clip(library_dir / "real.mkv")
    item = _video(db_session, library_dir, "real.mkv", duration_seconds=2.0)
    listing = client.get(f"/api/download/video/{item.id}/renditions").json()
    assert listing["source_height"] == 540
    assert [r["quality"] for r in listing["renditions"]] == ["480"]

    base = f"/api/download/video/{item.id}/renditions/480"
    assert client.post(base).json()["status"] in {"queued", "preparing", "ready"}
    _wait(lambda: _status(client, item.id, 480) in {"ready", "failed"}, seconds=120)
    ready = client.get(base).json()
    assert ready["status"] == "ready"

    resp = client.get(f"{base}/file")
    assert resp.status_code == 200
    assert len(resp.content) == ready["size"]
    out = renditions.rendition_path(item.id, 480)
    probed = _probe(out)
    assert "mp4" in probed["format"]["format_name"]
    assert [s["codec_type"] for s in probed["streams"]] == ["video", "audio"]
    video, audio = probed["streams"]
    assert (video["codec_name"], video["pix_fmt"]) == ("h264", "yuv420p")
    assert (video["width"], video["height"]) == (170, 480)
    assert (audio["codec_name"], audio["channels"]) == ("aac", 2)
    assert abs(float(probed["format"]["duration"]) - 2.0) < 0.5
    # faststart: the index (moov) comes before the media data (mdat).
    head = out.read_bytes()
    assert 0 < head.find(b"moov") < head.find(b"mdat")
    assert [p.name for p in settings.rendition_dir.iterdir()] == [out.name]


@needs_ffmpeg
def test_real_encode_reports_progress_and_stops_at_shutdown(db_session, library_dir, monkeypatch):
    # Longer than the encoder's start-up delay: with Debian's ffmpeg 7.1.5 the
    # first progress figure comes about 4 s into an encode read at playback
    # speed, so a 4 s clip would only ever report its end.
    _clip(library_dir / "long.mkv", size="64x540", seconds=12)
    item = _video(db_session, library_dir, "long.mkv", duration_seconds=12.0)
    source = library_dir / "long.mkv"
    real = renditions.build_command

    def slowed(src, out, quality, use_hw):
        command = real(src, out, quality, use_hw)
        at = command.index("-i")
        return [*command[:at], "-re", *command[at:]]  # read at playback speed

    monkeypatch.setattr(renditions, "build_command", slowed)
    renditions.request(item, 480, source)
    _wait(lambda: (renditions.status(item.id, 480, source)["progress"] or 0) > 0, seconds=60)
    state = renditions.status(item.id, 480, source)
    assert state["status"] == "preparing" and 0 < state["progress"] < 1
    proc = renditions._current_proc
    assert proc is not None

    renditions.shutdown_renditions()
    assert proc.poll() is not None
    assert renditions.status(item.id, 480, source)["status"] == "none"
    assert not list(settings.rendition_dir.iterdir())


@needs_ffmpeg
def test_real_encode_of_a_broken_file_fails(db_session, library_dir):
    item = _video(db_session, library_dir, "broken.mkv")
    source = library_dir / "broken.mkv"
    assert renditions.source_height(source) is None
    renditions.request(item, 720, source)
    _wait(lambda: renditions.status(item.id, 720, source)["status"] == "failed", seconds=60)
    assert not list(settings.rendition_dir.iterdir())
