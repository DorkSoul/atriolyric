from __future__ import annotations

import asyncio
import threading
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import art, cleanup, playback
from app.cleanup import delete_media_records, delete_user_records, enforce_cache_budget
from app.config import settings
from app.deps import _user_for_token
from app.library_query import filter_tracks_query
from app.models import (
    Base,
    CollectionPreference,
    DiscoveryState,
    Favorite,
    LibraryRoot,
    MediaItem,
    RadioStation,
    RadioStationFavorite,
    Session,
    TrackDislike,
    User,
    utcnow,
)
from app.schemas import PlaySourceIn
from app.security import session_token_digest
from app.ws_manager import ConnectionManager
from tests.conftest import startup_against


def test_stored_session_digest_is_not_a_bearer_cookie(db_session):
    raw = "audit-session-token"
    digest = session_token_digest(raw)
    db_session.add(
        Session(
            token=digest,
            user_id=1,
            expires_at=utcnow() + timedelta(hours=1),
        )
    )
    db_session.commit()

    assert _user_for_token(db_session, raw) is not None
    assert _user_for_token(db_session, digest) is None


def test_bootstrap_does_not_recreate_renamed_admin(tmp_path):
    from app import main

    engine = create_engine(f"sqlite:///{tmp_path / 'bootstrap.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine)
    with factory() as db:
        db.add(User(username="renamed", password_hash="unused", is_admin=True))
        db.commit()

    with startup_against(factory):
        main.seed_default_user()

    with factory() as db:
        assert db.query(User).count() == 1
        assert db.query(User).one().username == "renamed"


def test_user_cleanup_removes_discovery_and_all_radio_favorites(db_session):
    owner = User(username="owner", password_hash="unused")
    other = User(username="other", password_hash="unused")
    db_session.add_all([owner, other])
    db_session.commit()
    owner_id = owner.id
    station = RadioStation(
        name="Private", stream_url="https://example.invalid", owner_user_id=owner_id
    )
    db_session.add_all([station, DiscoveryState(user_id=owner_id)])
    db_session.commit()
    db_session.add_all(
        [
            RadioStationFavorite(user_id=owner_id, station_id=station.id),
            RadioStationFavorite(user_id=other.id, station_id=station.id),
        ]
    )
    db_session.commit()

    delete_user_records(db_session, owner_id)
    db_session.commit()

    assert db_session.get(DiscoveryState, owner_id) is None
    assert db_session.query(RadioStationFavorite).count() == 0


def test_user_and_media_cleanup_remove_like_and_dislike_rows(db_session):
    root = LibraryRoot(path="/cleanup", display_name="Cleanup")
    owner = User(username="owner", password_hash="unused")
    other = User(username="other", password_hash="unused")
    db_session.add_all([root, owner, other])
    db_session.commit()
    kept, deleted = (
        MediaItem(library_root_id=root.id, relative_path=f"{name}.mp3", title=name)
        for name in ("kept", "deleted")
    )
    db_session.add_all([kept, deleted])
    db_session.commit()
    db_session.add_all(
        [
            Favorite(user_id=other.id, media_item_id=deleted.id),
            TrackDislike(user_id=other.id, media_item_id=deleted.id),
            TrackDislike(user_id=other.id, media_item_id=kept.id),
            TrackDislike(user_id=owner.id, media_item_id=kept.id),
            CollectionPreference(
                user_id=owner.id, kind="artist", artist="A", state="disliked"
            ),
            CollectionPreference(
                user_id=other.id, kind="album", artist="A", album="X", state="liked"
            ),
        ]
    )
    db_session.commit()
    owner_id, other_id, kept_id = owner.id, other.id, kept.id

    delete_media_records(db_session, [deleted.id])
    delete_user_records(db_session, owner_id)
    db_session.commit()

    assert db_session.query(Favorite).count() == 0
    assert [(r.user_id, r.media_item_id) for r in db_session.query(TrackDislike)] == [
        (other_id, kept_id)
    ]
    # Album/artist entries store names, not media ids: a media delete keeps
    # them, a user delete removes only that user's.
    assert [r.user_id for r in db_session.query(CollectionPreference)] == [other_id]


def test_media_cleanup_removes_all_derived_files(db_session, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_path", tmp_path / "data" / "app.db")
    root = LibraryRoot(path=str(tmp_path), display_name="Test")
    db_session.add(root)
    db_session.commit()
    item = MediaItem(library_root_id=root.id, relative_path="old.mp3", title="Old")
    db_session.add(item)
    db_session.commit()

    paths = [
        settings.thumbnails_dir / f"{item.id}.jpg",
        settings.art_dir / f"{item.id}.png",
        settings.art_thumb_dir / f"{item.id}.jpg",
        settings.transcode_dir / f"{item.id}_128k.mp3",
    ]
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"old")

    delete_media_records(db_session, [item.id])
    db_session.commit()

    assert all(not path.exists() for path in paths)


def test_cache_budget_evicts_oldest_playback_files(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_path", tmp_path / "data" / "app.db")
    old = settings.transcode_dir / "old.mp3"
    new = settings.transcode_dir / "new.mp3"
    new.parent.mkdir(parents=True)
    old.write_bytes(b"1234")
    new.write_bytes(b"5678")
    old.touch()
    new.touch()
    old_mtime = old.stat().st_mtime - 10
    import os

    os.utime(old, (old_mtime, old_mtime))

    enforce_cache_budget(4)

    assert not old.exists()
    assert new.exists()


def test_cache_budget_never_evicts_thumbnails_or_art(tmp_path, monkeypatch):
    """Thumbnails and downloaded art live as long as their item - evicting
    them would only send the startup backfill regenerating them from the
    library disk after every restart."""
    monkeypatch.setattr(settings, "database_path", tmp_path / "data" / "app.db")
    kept = [
        settings.thumbnails_dir / "1.jpg",
        settings.art_dir / "2.jpg",
        settings.art_thumb_dir / "3.jpg",
    ]
    for path in kept:
        path.parent.mkdir(parents=True)
        path.write_bytes(b"image")

    enforce_cache_budget(0)

    assert all(path.exists() for path in kept)


def test_cache_budget_tolerates_unreadable_and_unremovable_files(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_path", tmp_path / "data" / "app.db")
    unreadable = settings.transcode_dir / "unreadable.mp3"
    protected = settings.transcode_dir / "protected.mp3"
    unreadable.parent.mkdir(parents=True)
    unreadable.write_bytes(b"old")
    protected.write_bytes(b"new")
    real_stat = Path.stat
    real_unlink = Path.unlink

    def guarded_stat(path, *args, **kwargs):
        if path == unreadable:
            raise OSError("unreadable")
        return real_stat(path, *args, **kwargs)

    def guarded_unlink(path, *args, **kwargs):
        if path == protected:
            raise OSError("protected")
        return real_unlink(path, *args, **kwargs)

    with (
        patch.object(Path, "stat", guarded_stat),
        patch.object(Path, "unlink", guarded_unlink),
    ):
        cleanup.enforce_cache_budget(0)


def test_remote_artwork_validation_and_download_bounds(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_path", tmp_path / "data" / "app.db")
    for url in ("http://example.com/art.jpg", "https://user:pass@example.com/a"):
        with pytest.raises(ValueError):
            art._validate_remote_art_url(url)
    with patch("app.art.socket.getaddrinfo", side_effect=OSError("dns")):
        with pytest.raises(ValueError, match="resolved"):
            art._validate_remote_art_url("https://example.invalid/a")
    with patch(
        "app.art.socket.getaddrinfo",
        return_value=[(None, None, None, None, ("1.1.1.1", 443))],
    ):
        art._validate_remote_art_url("https://coverartarchive.org/a")
        with pytest.raises(ValueError, match="allowed provider"):
            art._validate_remote_art_url("https://example.com/a")
    with patch(
        "app.art.socket.getaddrinfo",
        return_value=[(None, None, None, None, ("127.0.0.1", 443))],
    ):
        with pytest.raises(ValueError, match="private"):
            art._validate_remote_art_url("https://localhost.invalid/a")

    with pytest.raises(ValueError):
        art.cache_art_bytes(1, b"not-art")

    item = SimpleNamespace(id=123, cover_art_url=None)
    with (
        patch("app.art.find_art_path", return_value=None),
        patch(
            "app.art.extract_embedded_art",
            return_value=None,  # oversized pictures are dropped by extract
        ),
    ):
        assert art.load_full_art(item, tmp_path / "song.mp3") is None

    class FakeResponse:
        def __init__(self, status=200, headers=None, chunks=()):
            self.status_code = status
            self.headers = headers or {}
            self.chunks = chunks

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def raise_for_status(self):
            return None

        def iter_bytes(self, chunk_size):
            assert chunk_size == 64 * 1024
            yield from self.chunks

    class FakeClient:
        def __init__(self, responses):
            self.responses = iter(responses)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def stream(self, method, url):
            assert method == "GET"
            assert url.startswith("https://example.com/")
            return next(self.responses)

    def download_with(responses):
        with (
            patch("app.art._validate_remote_art_url"),
            patch("app.art.httpx.Client", return_value=FakeClient(responses)),
        ):
            return art._download_remote_art("https://example.com/a")

    assert download_with([FakeResponse(302)]) is None
    redirects = [
        FakeResponse(302, {"location": f"/next-{index}"}) for index in range(4)
    ]
    assert download_with(redirects) is None
    assert (
        download_with(
            [FakeResponse(headers={"content-length": str(art.MAX_ART_BYTES + 1)})]
        )
        is None
    )
    assert (
        download_with(
            [
                FakeResponse(
                    headers={"content-length": "invalid"},
                    chunks=[b"x" * (art.MAX_ART_BYTES + 1)],
                )
            ]
        )
        is None
    )
    assert download_with(
        [
            FakeResponse(
                headers={"content-type": "image/png; charset=binary"},
                chunks=[b"\x89PNG\r\n\x1a\npng-data"],
            )
        ]
    ) == (b"\x89PNG\r\n\x1a\npng-data", "image/png")

    item.cover_art_url = "https://example.com/a"
    with (
        patch("app.art.find_art_path", return_value=None),
        patch("app.art._download_remote_art", return_value=None),
    ):
        assert art.load_full_art(item, None) is None

    with patch("app.art.subprocess.run"):
        assert art._resize_art(tmp_path / "source.jpg", tmp_path / "out.jpg") is False


def test_broadcast_uses_recipient_snapshot_when_registry_changes():
    manager = ConnectionManager()

    sockets = {}

    class Socket:
        async def send_json(self, message):
            manager.disconnect(1, 2, sockets[2])

    sockets[1], sockets[2] = Socket(), Socket()
    manager.connect(1, 1, sockets[1])
    manager.connect(1, 2, sockets[2])
    asyncio.run(manager.broadcast(1, {"type": "state"}))


def test_malformed_play_source_payload_is_rejected(client):
    response = client.post(
        "/api/library/play-source",
        json={"kind": "album", "label": "Broken", "payload": {"album": []}},
    )
    assert response.status_code == 422


def test_play_source_schema_accepts_typed_artist_and_playlist_payloads():
    assert PlaySourceIn(
        kind="artist", label="Artist", payload={"artist": "A"}
    ).payload == {"artist": "A"}
    assert PlaySourceIn(
        kind="playlist", label="Playlist", payload={"playlist_id": 1}
    ).payload == {"playlist_id": 1}


def test_malformed_websocket_queue_values_do_not_close_connection(client):
    with client.websocket_connect("/ws/playback") as websocket:
        websocket.send_json({"type": "register_device", "name": "Browser"})
        websocket.receive_json()
        websocket.receive_json()
        websocket.send_json(
            {"type": "add_to_queue", "media_item_id": 1, "mode": {"bad": True}}
        )
        assert websocket.receive_json()["type"] == "error"
        websocket.send_json(
            {
                "type": "set_queue_context",
                "context": {"kind": "library", "filters": {"artist": []}},
                "start_media_item_id": 1,
            }
        )
        assert websocket.receive_json()["type"] == "error"


def test_invalid_stored_play_source_is_removed(client, db_session):
    from app.models import PlaySourceHistory

    db_session.add(
        PlaySourceHistory(
            user_id=1,
            kind="album",
            label="Broken",
            payload_json={"album": []},
        )
    )
    db_session.commit()

    assert client.get("/api/library/recently-played-sources").json() == []
    db_session.expire_all()
    assert db_session.query(PlaySourceHistory).count() == 0


def test_same_root_cannot_be_scanned_concurrently(db_session, tmp_path):
    from app import scanner

    root = LibraryRoot(path=str(tmp_path), display_name="Test")
    db_session.add(root)
    db_session.commit()
    lock = threading.Lock()
    lock.acquire()
    scanner._scan_locks[root.id] = lock
    try:
        try:
            scanner.scan_library_root(db_session, root)
        except RuntimeError as exc:
            assert "already running" in str(exc)
        else:
            raise AssertionError("Concurrent scan was accepted")
    finally:
        lock.release()
        scanner._scan_locks.pop(root.id, None)


def test_null_artist_filter_does_not_match_named_artist(db_session):
    user = db_session.query(User).one()
    root = LibraryRoot(path="/null-artist", display_name="Null artist")
    db_session.add(root)
    db_session.commit()
    db_session.add_all(
        [
            MediaItem(
                library_root_id=root.id,
                relative_path="unknown.mp3",
                title="Unknown",
                album="Shared album",
                artist=None,
            ),
            MediaItem(
                library_root_id=root.id,
                relative_path="named.mp3",
                title="Named",
                album="Shared album",
                artist="Named artist",
            ),
        ]
    )
    db_session.commit()
    rows = db_session.scalars(
        filter_tracks_query(
            db_session,
            user,
            {"artist_is_null": True, "album": "Shared album"},
        )
    ).all()
    assert [row.title for row in rows] == ["Unknown"]


def test_explicit_playlist_start_index_preserves_duplicate_occurrence(db_session):
    user = db_session.query(User).one()
    root = LibraryRoot(path="/duplicate-queue", display_name="Duplicate queue")
    db_session.add(root)
    db_session.commit()
    items = [
        MediaItem(
            library_root_id=root.id,
            relative_path=f"{i}.mp3",
            title=f"Track {i}",
        )
        for i in range(2)
    ]
    db_session.add_all(items)
    db_session.commit()
    context = {"kind": "ids", "ids": [items[0].id, items[1].id, items[1].id]}
    assert playback._natural_index_of(
        db_session, user, context, items[1].id, explicit_index=2
    ) == 2


def test_invalid_artist_null_queue_filter_is_rejected(db_session):
    user = db_session.query(User).one()
    assert (
        playback.set_queue_context(
            db_session,
            user,
            {"kind": "library", "filters": {"artist_is_null": "yes"}},
            1,
        )
        == "Invalid queue"
    )


def test_credential_envelope_handles_legacy_and_bad_keys(tmp_path, monkeypatch):
    from cryptography.fernet import Fernet

    from app import secrets

    monkeypatch.setattr(settings, "database_path", tmp_path / "data" / "app.db")
    secrets._cipher = None
    encrypted = secrets.encrypt_credential("token")
    assert encrypted.startswith(secrets.PREFIX)
    secrets._cipher = None  # exercise loading an existing key file
    assert secrets.decrypt_credential(encrypted) == "token"
    assert secrets.decrypt_credential("legacy-token") == "legacy-token"
    with pytest.raises(RuntimeError, match="cannot be decrypted"):
        secrets.decrypt_credential(secrets.PREFIX + "not-a-token")

    secrets._cipher = None
    settings.credential_key_path.write_bytes(b"bad\n")
    with pytest.raises(RuntimeError, match="Invalid credential key"):
        secrets.encrypt_credential("token")
    secrets._cipher = None
    settings.credential_key_path.write_bytes(Fernet.generate_key() + b"\n")
