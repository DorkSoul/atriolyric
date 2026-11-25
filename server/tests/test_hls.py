from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from app import hls
from app.config import settings
from app.models import LibraryRoot, MediaItem, MediaKind
from app.video_meta import is_browser_playable

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg not installed",
)


def _clip(path, pix_fmt: str = "yuv420p10le", audio: str = "aac", seconds: int = 20) -> None:
    subprocess.run(
        [
            "ffmpeg", "-y", "-f", "lavfi", "-i", f"testsrc=size=128x96:rate=24:duration={seconds}",
            "-f", "lavfi", "-i", f"sine=duration={seconds}",
            "-c:v", "libx264", "-pix_fmt", pix_fmt, "-g", "24", "-c:a", audio, "-shortest",
            str(path),
        ],
        check=True,
        capture_output=True,
    )


def _video(db_session, library_dir, filename: str, **clip_kwargs) -> int:
    root = LibraryRoot(path=str(library_dir), display_name="Test Library")
    db_session.add(root)
    db_session.commit()
    db_session.refresh(root)
    _clip(library_dir / filename, **clip_kwargs)
    item = MediaItem(
        library_root_id=root.id,
        kind=MediaKind.video,
        relative_path=filename,
        title="Clip",
        duration_seconds=20.0,
    )
    db_session.add(item)
    db_session.commit()
    db_session.refresh(item)
    return item.id


@pytest.fixture(autouse=True)
def _software_and_clean(monkeypatch):
    monkeypatch.setattr(settings, "hwaccel", "none")
    yield
    hls.shutdown_sessions()


def _probe(path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams", "-show_format", str(path)],
        check=True,
        capture_output=True,
    )
    return json.loads(out.stdout)


def test_playback_mode_depends_on_codecs(client, db_session, library_dir):
    ten_bit = _video(db_session, library_dir, "ten.mkv")
    resp = client.get(f"/api/stream/{ten_bit}/playback")
    assert resp.json() == {"mode": "hls", "url": f"/api/stream/{ten_bit}/hls/index.m3u8"}


def test_playable_h264_stays_direct(client, db_session, library_dir):
    ok = _video(db_session, library_dir, "ok.mp4", pix_fmt="yuv420p")
    assert client.get(f"/api/stream/{ok}/playback").json() == {
        "mode": "direct",
        "url": f"/api/stream/{ok}",
    }
    assert is_browser_playable(library_dir / "ok.mp4")


def test_incompatible_audio_alone_uses_hls(client, db_session, library_dir):
    item = _video(db_session, library_dir, "ac3.mkv", pix_fmt="yuv420p", audio="ac3")
    assert client.get(f"/api/stream/{item}/playback").json()["mode"] == "hls"


def test_playlist_is_a_complete_vod_grid(client, db_session, library_dir):
    item = _video(db_session, library_dir, "ten.mkv")
    resp = client.get(f"/api/stream/{item}/hls/index.m3u8")
    assert resp.status_code == 200
    lines = resp.text.splitlines()
    assert "#EXT-X-PLAYLIST-TYPE:VOD" in lines and lines[-1] == "#EXT-X-ENDLIST"
    assert [line for line in lines if line.endswith(".ts")] == ["0.ts", "1.ts", "2.ts", "3.ts"]


def test_segments_are_browser_safe_and_seek_restarts_on_the_grid(client, db_session, library_dir):
    item = _video(db_session, library_dir, "ten.mkv")

    first = client.get(f"/api/stream/{item}/hls/0.ts")
    assert first.status_code == 200
    seg0 = settings.hls_dir.parent / "seg0.ts"
    seg0.write_bytes(first.content)
    streams = {s["codec_type"]: s for s in _probe(seg0)["streams"]}
    assert (streams["video"]["codec_name"], streams["video"]["pix_fmt"]) == ("h264", "yuv420p")
    assert streams["audio"]["codec_name"] == "aac"

    # A seek far from the encoder restarts it, and the new run's timestamps
    # continue the grid (segment 3 starts at ~18s, not back at 0).
    third = client.get(f"/api/stream/{item}/hls/3.ts")
    assert third.status_code == 200
    seg3 = settings.hls_dir.parent / "seg3.ts"
    seg3.write_bytes(third.content)
    start = float(_probe(seg3)["format"]["start_time"])
    first_start = float(_probe(seg0)["format"]["start_time"])
    assert abs((start - first_start) - 18.0) < 1.0


def test_bad_segment_names_and_indexes_404(client, db_session, library_dir):
    item = _video(db_session, library_dir, "ten.mkv")
    assert client.get(f"/api/stream/{item}/hls/abc.ts").status_code == 404
    assert client.get(f"/api/stream/{item}/hls/99.ts").status_code == 404


def test_tracks_have_no_hls(client, db_session, library_dir):
    from tests.test_stream import _scan_one_track

    media_id, _ = _scan_one_track(db_session, library_dir)
    assert client.get(f"/api/stream/{media_id}/hls/index.m3u8").status_code == 404


def test_cached_segment_is_served_again_without_reencoding(client, db_session, library_dir):
    item = _video(db_session, library_dir, "ten.mkv")
    first = client.get(f"/api/stream/{item}/hls/0.ts")
    assert first.status_code == 200

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(hls, "_start", lambda *a, **k: pytest.fail("re-encoded a cached segment"))
        again = client.get(f"/api/stream/{item}/hls/0.ts")
    assert again.content == first.content


def test_size_cap_evicts_passed_segments_first(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "hls_cache_max_bytes", 250)
    session = hls._Session(key=(1, 1), directory=tmp_path)
    session.last_index = 2
    for index in range(4):
        (tmp_path / f"{index}.ts").write_bytes(b"x" * 100)

    hls._enforce_size_cap([session])

    # Over the cap by 150 bytes: the two segments already behind the player go.
    assert sorted(p.name for p in tmp_path.glob("*.ts")) == ["2.ts", "3.ts"]


def test_startup_clears_leftover_hls_and_legacy_video_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_path", tmp_path / "data" / "app.db")
    legacy_video_cache = settings.database_path.parent / "video_cache"
    for directory in (settings.hls_dir, legacy_video_cache):
        directory.mkdir(parents=True)
        (directory / "leftover").write_bytes(b"x")

    hls.reset_hls_dir()

    assert not settings.hls_dir.exists() and not legacy_video_cache.exists()


def test_audio_only_segments_have_no_video(client, db_session, library_dir, tmp_path):
    vid = _video(db_session, library_dir, "bg.mkv")
    playlist = client.get(f"/api/stream/{vid}/audio/index.m3u8")
    assert playlist.status_code == 200
    assert playlist.text.replace("?max_bitrate=192", "") == client.get(
        f"/api/stream/{vid}/hls/index.m3u8"
    ).text
    resp = client.get(f"/api/stream/{vid}/audio/1.ts")
    assert resp.status_code == 200
    out = tmp_path / "a.ts"
    out.write_bytes(resp.content)
    streams = _probe(out)["streams"]
    assert [s["codec_type"] for s in streams] == ["audio"]
    assert streams[0]["codec_name"] == "aac"


def test_audio_only_follows_the_music_quality_cap(client, db_session, library_dir, tmp_path):
    vid = _video(db_session, library_dir, "q.mkv")
    playlist = client.get(f"/api/stream/{vid}/audio/index.m3u8?max_bitrate=96").text
    assert "0.ts?max_bitrate=96" in playlist
    # An unsupported cap falls back to the default rather than erroring.
    assert "0.ts?max_bitrate=192" in client.get(
        f"/api/stream/{vid}/audio/index.m3u8?max_bitrate=7"
    ).text
    assert client.get(f"/api/stream/{vid}/audio/0.ts?max_bitrate=96").status_code == 200
    assert client.get(f"/api/stream/{vid}/audio/0.ts?max_bitrate=320").status_code == 200
    assert len(hls._sessions) == 2


def test_audio_and_video_sessions_are_separate(client, db_session, library_dir):
    vid = _video(db_session, library_dir, "both.mkv")
    assert client.get(f"/api/stream/{vid}/audio/0.ts").status_code == 200
    assert client.get(f"/api/stream/{vid}/hls/0.ts").status_code == 200
    assert len(hls._sessions) == 2


def test_audio_endpoint_rejects_tracks_and_bad_names(client, db_session, library_dir):
    vid = _video(db_session, library_dir, "x.mkv")
    assert client.get(f"/api/stream/{vid}/audio/evil.ts").status_code == 404
    assert client.get(f"/api/stream/{vid}/audio/999.ts").status_code == 404


def test_stale_request_does_not_fight_a_seek_for_the_encoder(tmp_path, monkeypatch):
    """A request left over from before a seek must give up instead of
    restarting the encoder back to its own position over and over."""
    import threading

    from fastapi import HTTPException

    from tests.conftest import wait_until

    starts: list[int] = []
    monkeypatch.setattr(hls, "_start", lambda session, file_path, index: starts.append(index))
    monkeypatch.setattr(hls, "_ensure_janitor", lambda: None)

    outcome: dict[str, object] = {}

    def stale():
        try:
            hls.get_segment(1, 1, tmp_path / "x.mkv", 1, 600.0)
        except HTTPException as exc:
            outcome["stale"] = exc.status_code

    thread = threading.Thread(target=stale)
    thread.start()
    # The stale request has started the encoder for segment 1 and waits on it.
    wait_until(lambda: 1 in starts, what="the stale request's encoder start")
    session = hls._get_session(1, 1)

    def finish_seek():
        # The segment appears only once the seek has restarted the encoder.
        wait_until(lambda: starts and starts[-1] == 93, what="the seek's encoder start")
        (session.directory / "93.ts").write_bytes(b"x")

    threading.Thread(target=finish_seek).start()
    path = hls.get_segment(1, 1, tmp_path / "x.mkv", 93, 600.0)
    thread.join(5)

    assert path.name == "93.ts"
    assert outcome["stale"] == 409
    assert not thread.is_alive()
    # The stale request stopped asking for restarts once the seek arrived.
    assert starts[-1] == 93


def test_leftover_segments_from_an_earlier_run_are_not_encoder_progress(tmp_path):
    session = hls._Session(key=(1, 1, 0), directory=tmp_path)
    for index in (169, 170, 171):  # left by an earlier run, further on
        (tmp_path / f"{index}.ts").write_bytes(b"x")
    session.start_index = 130
    assert session.newest_finished() == 129
    (tmp_path / "130.ts").write_bytes(b"x")
    (tmp_path / "131.ts").write_bytes(b"x")
    assert session.newest_finished() == 131


def test_seeking_back_behind_a_later_run_restarts_the_encoder_once(tmp_path, monkeypatch):
    import threading
    import time

    monkeypatch.setattr(hls, "_ensure_janitor", lambda: None)
    session = hls._get_session(5, 1)
    for index in (169, 170):
        (session.directory / f"{index}.ts").write_bytes(b"x")
    session.start_index = 169
    starts: list[int] = []

    class _Alive:
        returncode = None

        def poll(self):
            return None

        def send_signal(self, _sig):
            self.returncode = -9

        def wait(self, timeout=None):
            return self.returncode

    def fake_start(sess, file_path, index):
        starts.append(index)
        sess.start_index = index
        sess.proc = _Alive()

    monkeypatch.setattr(hls, "_start", fake_start)
    threading.Timer(0.6, lambda: (session.directory / "130.ts").write_bytes(b"x")).start()
    started = time.monotonic()
    path = hls.get_segment(5, 1, tmp_path / "x.mkv", 130, 1200.0)
    assert path.name == "130.ts"
    assert starts == [130]
    assert time.monotonic() - started < 5


def _item(db_session, library_dir, filename: str, duration: float) -> int:
    root = LibraryRoot(path=str(library_dir), display_name="Test Library")
    db_session.add(root)
    db_session.commit()
    item = MediaItem(
        library_root_id=root.id, kind=MediaKind.video, relative_path=filename,
        title="Clip", duration_seconds=duration,
    )
    db_session.add(item)
    db_session.commit()
    return item.id


def test_a_damaged_video_that_floods_stderr_still_encodes(
    client, db_session, library_dir, monkeypatch
):
    """With stderr on an unread pipe, ffmpeg would block once a damaged file
    had produced 64 KB of decode errors, and every later segment would 504."""
    import random

    monkeypatch.setattr(hls, "SEGMENT_WAIT_SECONDS", 30)
    _clip(library_dir / "clean.mkv", seconds=60)
    data = bytearray((library_dir / "clean.mkv").read_bytes())
    rng = random.Random(1)  # noqa: S311 - reproducible damage, not security
    for offset in range(len(data) // 8, len(data) * 7 // 8, 300):
        data[offset] = rng.randrange(256)
    (library_dir / "bad.mkv").write_bytes(bytes(data))
    item = _item(db_session, library_dir, "bad.mkv", 60.0)

    statuses = [client.get(f"/api/stream/{item}/hls/{i}.ts").status_code for i in range(10)]
    assert set(statuses) <= {200, 404} and statuses[0] == 200
    assert 504 not in statuses
    session = next(iter(hls._sessions.values()))
    logged = (session.directory / hls.STDERR_LOG).stat().st_size
    if logged < 64 * 1024:
        pytest.skip(f"this ffmpeg logged only {logged} bytes, not enough to fill a pipe")


def test_a_segment_past_the_real_end_is_404_not_an_empty_200(client, db_session, library_dir):
    """A stored duration longer than the file: starting past the end, ffmpeg
    exits 0 and leaves a 0-byte segment, which is not served as 200."""
    _clip(library_dir / "short.mkv", seconds=7)
    item = _item(db_session, library_dir, "short.mkv", 40.0)
    assert client.get(f"/api/stream/{item}/hls/0.ts").status_code == 200
    assert client.get(f"/api/stream/{item}/hls/5.ts").status_code == 404
