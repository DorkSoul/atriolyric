from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select, update

from app.library_cache import bump_library_version
from app.models import (
    IgnoredLibraryFolder,
    LibraryRoot,
    ListenEvent,
    MediaItem,
    PlayHistory,
    User,
    WantListItem,
    utcnow,
)
from app.scanner import scan_library_root
from app.security import hash_password
from tests.conftest import login_as, make_audio_fixture


def _seed_library(db_session, library_dir) -> LibraryRoot:
    make_audio_fixture(
        library_dir / "a.wav",
        title="One",
        artist="Band",
        album="LP",
        genre="Rock",
        date="1995",
        seconds=2,
    )
    make_audio_fixture(
        library_dir / "b.wav",
        title="Two",
        artist="Band",
        album="LP",
        genre="rock",
        date="1999-04",
        seconds=2,
    )
    make_audio_fixture(
        library_dir / "c.wav",
        title="Three",
        artist="Other",
        album="EP",
        genre="Jazz",
        date="2012",
    )
    make_audio_fixture(
        library_dir / "d.wav", title="Four", artist="Other", album=None, genre=None
    )
    root = LibraryRoot(path=str(library_dir), display_name="Test Library")
    db_session.add(root)
    db_session.commit()
    scan_library_root(db_session, root)
    return root


def _ids(db_session) -> dict[str, int]:
    return {
        title: item_id
        for item_id, title in db_session.execute(select(MediaItem.id, MediaItem.title))
    }


def test_stats_totals_genres_decades_and_recently_added(
    client, db_session, library_dir
):
    _seed_library(db_session, library_dir)
    response = client.get("/api/library/stats")
    assert response.status_code == 200
    stats = response.json()

    totals = stats["totals"]
    assert (totals["songs"], totals["albums"], totals["artists"], totals["videos"]) == (
        4,
        2,
        2,
        0,
    )
    assert totals["total_bytes"] == sum(p.stat().st_size for p in library_dir.iterdir())
    assert totals["total_seconds"] == 6.0
    assert totals["last_scanned_at"] is None  # scanned directly, not through a task

    # "rock" and "Rock" are one genre.
    assert stats["by_genre"]["distinct"] == 2
    assert [(g["name"].lower(), g["songs"]) for g in stats["by_genre"]["top"]] == [
        ("rock", 2),
        ("jazz", 1),
    ]
    assert stats["contents"]["genre_count"] == 2
    assert stats["by_decade"] == {
        "decades": [{"decade": "1990s", "songs": 2}, {"decade": "2010s", "songs": 1}],
        "unknown": 1,
    }

    recent = stats["recently_added"]
    assert {(a["artist"], a["album"]) for a in recent} == {
        ("Band", "LP"),
        ("Other", "EP"),
    }
    lp = next(a for a in recent if a["album"] == "LP")
    assert (
        lp["track_count"] == 2
        and lp["year"] == 1999
        and lp["format_label"] == "WAV 16/8"
    )
    assert lp["art_key"] and lp["added_at"]
    # The album's first-indexed track. Which file that is depends on the order
    # the disk lists them in (name order here, not on every filesystem).
    ids = _ids(db_session)
    assert lp["representative_media_item_id"] == min(ids["One"], ids["Two"])

    contents = stats["contents"]
    assert contents["formats"] == [
        {
            "format": "wav",
            "label": "WAV",
            "lossless": True,
            "songs": 4,
            "bytes": totals["total_bytes"],
        }
    ]
    assert (
        contents["with_lyrics"],
        contents["loudness_measured"],
        contents["liked_songs"],
    ) == (0, 0, 0)

    quality = stats["quality"]
    assert quality["lossless_songs"] == 4 and quality["lossless_share"] == 1.0
    assert quality["hi_res_songs"] == 0 and quality["average_mp3_kbps"] is None
    assert quality["below_192_kbps_songs"] == 0
    assert quality["longest_song_seconds"] == 2.0
    assert quality["average_album_seconds"] == 2.5  # LP 4 s, EP 1 s
    assert (quality["quality_scanned"], quality["quality_total"]) == (4, 4)

    growth = stats["growth"]
    assert (
        growth["albums_added_this_month"] == 2 and growth["albums_added_this_year"] == 2
    )
    assert growth["largest_album"]["album"] == "LP"
    assert growth["video_bytes"] == 0
    assert "storage" not in growth
    storage_rows = client.get("/api/library/stats/storage").json()
    assert len(storage_rows) == 1
    storage = storage_rows[0]
    assert storage["roots"] == ["Test Library"]
    assert storage["free_bytes"] > 0 and storage["total_bytes"] >= storage["free_bytes"]

    nav = stats["nav_counts"]
    assert (nav["songs"], nav["albums"], nav["artists"], nav["videos"]) == (4, 2, 2, 0)
    assert nav["playlists"] == len(client.get("/api/playlists").json())
    assert nav["radio_stations"] == len(client.get("/api/radio/stations").json())
    assert nav["want_list"] == 0


def test_quality_counts_say_how_much_is_scanned(client, db_session, library_dir):
    _seed_library(db_session, library_dir)
    ids = _ids(db_session)
    db_session.execute(
        update(MediaItem)
        .where(MediaItem.id == ids["One"])
        .values(codec="mp3", bitrate_kbps=128, bit_depth=None, sample_rate_hz=44100)
    )
    db_session.execute(
        update(MediaItem)
        .where(MediaItem.id == ids["Two"])
        .values(codec="flac", bitrate_kbps=3000, bit_depth=24, sample_rate_hz=96000)
    )
    db_session.execute(
        update(MediaItem).where(MediaItem.id == ids["Four"]).values(codec=None)
    )
    db_session.commit()
    bump_library_version()

    stats = client.get("/api/library/stats").json()
    quality = stats["quality"]
    assert (quality["quality_scanned"], quality["quality_total"]) == (3, 4)
    assert quality["lossless_songs"] == 2
    assert quality["lossless_share"] == 2 / 3
    assert quality["hi_res_songs"] == 1
    assert quality["average_mp3_kbps"] == 128
    assert quality["below_192_kbps_songs"] == 1
    formats = {f["format"]: f for f in stats["contents"]["formats"]}
    # The unscanned row counts under its extension.
    assert {key: f["songs"] for key, f in formats.items()} == {
        "wav": 2,
        "mp3": 1,
        "flac": 1,
    }
    assert formats["flac"]["label"] == "FLAC" and formats["mp3"]["lossless"] is False
    lp = next(a for a in stats["recently_added"] if a["album"] == "LP")
    assert lp["format_label"] in ("MP3 128", "FLAC 24/96")


def test_ignored_folders_stay_out_of_the_stats_but_not_the_tab_counts(
    client, db_session, library_dir
):
    (library_dir / "skip").mkdir()
    make_audio_fixture(
        library_dir / "skip" / "x.wav",
        title="Skipped",
        artist="Band",
        album="Skip",
        genre="Pop",
    )
    root = _seed_library(db_session, library_dir)
    db_session.add(IgnoredLibraryFolder(library_root_id=root.id, relative_path="skip"))
    db_session.commit()
    bump_library_version()

    stats = client.get("/api/library/stats").json()
    assert stats["totals"]["songs"] == 4
    assert "pop" not in [g["name"].lower() for g in stats["by_genre"]["top"]]
    assert stats["nav_counts"]["songs"] == 5 and stats["nav_counts"]["albums"] == 3


def test_stats_respect_a_private_root(client, session_factory, db_session, tmp_path):
    shared_dir, private_dir = tmp_path / "shared", tmp_path / "private"
    shared_dir.mkdir()
    private_dir.mkdir()
    make_audio_fixture(
        shared_dir / "s.wav", title="Shared", artist="A", album="X", genre="Rock"
    )
    make_audio_fixture(
        private_dir / "p.wav", title="Private", artist="B", album="Y", genre="Jazz"
    )
    make_audio_fixture(
        private_dir / "q.wav", title="Private 2", artist="B", album="Y", genre="Jazz"
    )
    for path, name, shared in (
        (shared_dir, "Shared", True),
        (private_dir, "Private", False),
    ):
        root = LibraryRoot(path=str(path), display_name=name, is_shared=shared)
        db_session.add(root)
        db_session.commit()
        scan_library_root(db_session, root)
    plain_user = User(username="plain", password_hash=hash_password("plainpass"))
    db_session.add(plain_user)
    db_session.commit()
    plain = login_as(session_factory, client, "plain", "plainpass")

    stats = plain.get("/api/library/stats").json()
    assert stats["totals"]["songs"] == 1 and stats["totals"]["albums"] == 1
    assert [g["name"] for g in stats["by_genre"]["top"]] == ["Rock"]
    assert [a["album"] for a in stats["recently_added"]] == ["X"]
    assert [s["roots"] for s in plain.get("/api/library/stats/storage").json()] == [
        ["Shared"]
    ]
    assert stats["nav_counts"]["songs"] == 1

    private_id = db_session.scalars(
        select(LibraryRoot.id).where(LibraryRoot.display_name == "Private")
    ).one()
    assert (
        client.post(f"/api/admin/roots/{private_id}/access/{plain_user.id}").status_code
        == 200
    )
    assert plain.get("/api/library/stats").json()["totals"]["songs"] == 3


def test_listening_numbers_are_per_user(
    client, session_factory, db_session, library_dir
):
    _seed_library(db_session, library_dir)
    ids = _ids(db_session)
    me = db_session.scalars(select(User).where(User.username == "dork_soul")).one()
    other = User(username="plain", password_hash=hash_password("plainpass"))
    db_session.add(other)
    db_session.commit()
    now = utcnow()
    for offset_days, title in ((0, "One"), (1, "One"), (2, "Three"), (20, "One")):
        played = now - timedelta(days=offset_days)
        if played.year != now.year:
            continue
        db_session.add(
            PlayHistory(
                user_id=me.id,
                media_item_id=ids[title],
                played_at=played,
                started_at=played,
            )
        )
        db_session.add(
            ListenEvent(
                user_id=me.id,
                media_item_id=ids[title],
                started_at=played,
                ended_at=played,
                listened_seconds=100.0,
                end_reason="finished",
            )
        )
    db_session.add(WantListItem(user_id=me.id, kind="album", artist="X", title="Y"))
    db_session.add(
        WantListItem(user_id=me.id, kind="album", artist="X", title="Z", excluded=True)
    )
    db_session.commit()

    mine = client.get("/api/library/stats").json()
    listening = mine["listening"]
    plays_this_year = db_session.execute(
        select(PlayHistory.id).where(PlayHistory.user_id == me.id)
    ).all()
    assert listening["plays_this_week"] == 3
    assert listening["plays_this_year"] == len(plays_this_year)
    assert listening["listening_seconds_this_year"] == 100.0 * len(plays_this_year)
    assert listening["listening_capped"] is False
    assert listening["most_played_artist"]["name"] == "Band"
    assert listening["most_played_album"] == {
        "artist": "Band",
        "album": "LP",
        "plays": listening["most_played_artist"]["plays"],
    }
    assert listening["never_played_songs"] == 2
    assert mine["nav_counts"]["want_list"] == 1

    theirs = (
        login_as(session_factory, client, "plain", "plainpass")
        .get("/api/library/stats")
        .json()
    )
    assert theirs["listening"] == {
        "plays_this_week": 0,
        "plays_this_year": 0,
        "listening_seconds_this_year": 0.0,
        "listening_capped": False,
        "most_played_artist": None,
        "most_played_album": None,
        "never_played_songs": 4,
    }
    assert theirs["nav_counts"]["want_list"] == 0


def test_listening_is_capped_when_plays_predate_the_listen_log(
    client, db_session, library_dir
):
    _seed_library(db_session, library_dir)
    me = db_session.scalars(select(User).where(User.username == "dork_soul")).one()
    now = utcnow()
    db_session.add(
        PlayHistory(user_id=me.id, media_item_id=_ids(db_session)["One"], played_at=now)
    )
    db_session.commit()
    assert (
        client.get("/api/library/stats").json()["listening"]["listening_capped"] is True
    )


def test_liked_songs_and_the_shared_cache(client, db_session, library_dir):
    _seed_library(db_session, library_dir)
    ids = _ids(db_session)
    assert client.get("/api/library/stats").json()["contents"]["liked_songs"] == 0
    assert (
        client.put(
            f"/api/media/{ids['One']}/preference", json={"state": "liked"}
        ).status_code
        == 200
    )
    # Liked songs are per user and never come from the cache.
    assert client.get("/api/library/stats").json()["contents"]["liked_songs"] == 1


def test_storage_never_raises_for_an_unreadable_root(client, db_session, tmp_path):
    root = LibraryRoot(path=str(tmp_path / "unmounted"), display_name="Gone")
    db_session.add(root)
    db_session.commit()
    stats = client.get("/api/library/stats").json()
    assert client.get("/api/library/stats/storage").json() == [
        {"roots": ["Gone"], "free_bytes": None, "total_bytes": None}
    ]
    assert stats["totals"]["songs"] == 0
    assert stats["quality"]["lossless_share"] is None


def test_stats_require_login(raw_client):
    assert raw_client.get("/api/library/stats").status_code == 401


def test_storage_gives_up_on_a_folder_that_does_not_answer(
    client, db_session, tmp_path, monkeypatch
):
    import threading

    from app.routers.library import stats as stats_module

    release = threading.Event()

    def hung(path):
        release.wait(5)
        raise OSError("hung mount")

    monkeypatch.setattr(stats_module, "_disk_usage", hung)
    monkeypatch.setattr(stats_module, "STORAGE_CHECK_SECONDS", 0.2)
    db_session.add(LibraryRoot(path=str(tmp_path), display_name="Slow"))
    db_session.commit()
    try:
        assert client.get("/api/library/stats/storage").json() == [
            {"roots": ["Slow"], "free_bytes": None, "total_bytes": None}
        ]
        # The main stats never touch the filesystem.
        assert "storage" not in client.get("/api/library/stats").json()["growth"]
    finally:
        release.set()
