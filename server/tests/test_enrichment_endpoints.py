from __future__ import annotations

from unittest.mock import patch

from app.models import LibraryRoot, MediaItem
from app.scanner import scan_library_root
from tests.conftest import make_audio_fixture, wait_until
from tests.test_admin import _settled_last_task


def _scan_one_track(db_session, library_dir):
    make_audio_fixture(
        library_dir / "song.wav", title="Bohemian Rhapsody", artist=None, album=None, genre=None
    )
    root = LibraryRoot(path=str(library_dir), display_name="Lib")
    db_session.add(root)
    db_session.commit()
    db_session.refresh(root)
    scan_library_root(db_session, root)

    return db_session.query(MediaItem).one()


def test_admin_bulk_enrich_root(client, db_session, library_dir, session_factory, monkeypatch):
    monkeypatch.setattr("app.library_tasks.SessionLocal", session_factory)
    _scan_one_track(db_session, library_dir)
    from app.models import LibraryRoot

    root = db_session.query(LibraryRoot).one()

    with patch("app.library_tasks.enrich_media_item", return_value={"matched": True}) as mock_enrich:
        resp = client.post(
            f"/api/admin/roots/{root.id}/enrich", json={"mode": "musicbrainz_fallback"}
        )
        assert resp.status_code == 202
        task = wait_until(lambda: _settled_last_task(client, root.id), what="the enrichment task")

    assert task["status"] == "completed"
    assert task["total"] == 1
    assert task["matched"] == 1
    mock_enrich.assert_called_once()


def test_lyrics_endpoint_returns_embedded(client, db_session, library_dir):
    item = _scan_one_track(db_session, library_dir)
    with patch("app.enrichment.lyrics.get_embedded_lyrics", return_value="La la la"):
        resp = client.get(f"/api/library/media/{item.id}/lyrics")
    assert resp.status_code == 200
    assert resp.json() == {"plain": "La la la", "synced": None, "source": "embedded", "pending": False}


def test_lyrics_endpoint_404s_for_inaccessible_item(client):
    resp = client.get("/api/library/media/999999/lyrics")
    assert resp.status_code == 404


def test_admin_listenbrainz_tags_need_no_account(client, db_session, library_dir):
    from unittest.mock import patch

    _scan_one_track(db_session, library_dir)
    root = db_session.query(LibraryRoot).one()

    with patch.dict("app.library_tasks.TASK_WORKERS", {"lb_tags": lambda *a: None, "lb_tags_all": lambda *a: None}):
        response = client.post(f"/api/admin/roots/{root.id}/enrich", json={"mode": "listenbrainz_missing"})
        assert response.status_code == 202, response.text
        assert response.json()["kind"] == "lb_tags"
    old = client.post(f"/api/admin/roots/{root.id}/enrich", json={"mode": "lastfm_missing"})
    assert old.status_code == 422
