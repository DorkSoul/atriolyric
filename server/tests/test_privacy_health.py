"""The ListenBrainz recommendations opt-out, and the real /api/health."""

from __future__ import annotations

from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import main, migrations
from app.config import settings
from app.models import Artist, MediaItem, User
from tests.conftest import played_now, startup_against
from tests.test_listenbrainz_data import _library

# --- ListenBrainz recommendations switch ------------------------------------


def test_the_switch_defaults_on_and_round_trips(client):
    assert client.get("/api/auth/privacy-settings").json() == {"online_recommendations": True}
    assert client.get("/api/auth/me").json()["online_recommendations"] is True
    resp = client.put("/api/auth/privacy-settings", json={"online_recommendations": False})
    assert resp.json() == {"online_recommendations": False}
    assert client.get("/api/auth/privacy-settings").json() == {"online_recommendations": False}
    assert client.get("/api/auth/me").json()["online_recommendations"] is False
    assert client.put("/api/auth/privacy-settings", json={"online_recommendations": True}).json() == {
        "online_recommendations": True
    }


def test_the_switch_requires_login_and_a_boolean(raw_client, client):
    assert raw_client.cookies.get("atriolyric_session")  # `client` logged this one in
    assert client.put("/api/auth/privacy-settings", json={}).status_code == 422
    client.post("/api/auth/logout")
    assert client.get("/api/auth/privacy-settings").status_code == 401
    assert client.put("/api/auth/privacy-settings", json={"online_recommendations": False}).status_code == 401


def test_a_played_song_is_prefetched_only_for_users_who_allow_it(client, db_session):
    _library(db_session)
    item = db_session.query(MediaItem).first()
    with patch("app.routers.stream.prefetch_similar_in_background") as prefetch, patch(
        "app.routers.stream.scrobble_track"
    ) as scrobble:
        client.put("/api/auth/privacy-settings", json={"online_recommendations": False})
        assert client.post(f"/api/stream/{item.id}/played", json=played_now()).status_code == 200
        prefetch.assert_not_called()
        assert scrobble.call_count == 1  # scrobbling is a separate, linked-account feature
        client.put("/api/auth/privacy-settings", json={"online_recommendations": True})
        assert client.post(f"/api/stream/{item.id}/played", json=played_now()).status_code == 200
        prefetch.assert_called_once_with(item.id)


def test_info_lookups_ask_listenbrainz_nothing_for_an_opted_out_user(client, db_session):
    _library(db_session)
    db_session.query(Artist).filter_by(name="Seed Band").one().mbid = "00000004-0000-4000-8000-000000000004"
    db_session.commit()
    client.put("/api/auth/privacy-settings", json={"online_recommendations": False})
    with patch("app.listenbrainz_data.listenbrainz.artist_popularity") as popularity, patch(
        "app.listenbrainz_data.listenbrainz.top_recordings_for_artist"
    ) as top, patch("app.metadata_info.musicbrainz.lookup_artist", return_value=type("R", (), {"error": None, "value": {}})()):
        assert client.get("/api/library/artist-info", params={"name": "Seed Band"}).status_code == 200
        assert client.get("/api/library/artist-top-tracks", params={"name": "Seed Band"}).status_code == 200
    popularity.assert_not_called()
    top.assert_not_called()


def test_existing_users_keep_recommendations_on_after_the_migration(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    factory = sessionmaker(bind=engine)
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT, "
            "password_hash TEXT, is_admin BOOLEAN, created_at DATETIME)"
        )
        conn.exec_driver_sql("INSERT INTO users (username, password_hash, is_admin) VALUES ('old', 'x', 0)")
    with startup_against(factory):
        main.seed_default_user()
    with engine.connect() as conn:
        assert conn.exec_driver_sql("SELECT online_recommendations FROM users").scalar() == 1
        columns = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(users)")}
    assert "lastfm_recommendations" not in columns
    assert migrations.rename_lastfm_columns in migrations.MIGRATIONS
    assert isinstance(User.online_recommendations.default.arg, bool)  # type: ignore[union-attr]


def test_an_opted_out_user_stays_opted_out_after_the_rename(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE users (id INTEGER PRIMARY KEY, lastfm_recommendations BOOLEAN)")
        conn.exec_driver_sql("INSERT INTO users (lastfm_recommendations) VALUES (0)")
        conn.exec_driver_sql("CREATE TABLE media_items (id INTEGER PRIMARY KEY, lastfm_fetched_at DATETIME)")
        conn.exec_driver_sql("INSERT INTO media_items (lastfm_fetched_at) VALUES ('2026-01-01 00:00:00')")
    with patch("app.migrations.engine", engine):
        migrations.rename_lastfm_columns()
        migrations.rename_lastfm_columns()  # idempotent
    with engine.connect() as conn:
        assert conn.exec_driver_sql("SELECT online_recommendations FROM users").scalar() == 0
        assert conn.exec_driver_sql("SELECT online_tags_fetched_at FROM media_items").scalar() is not None


# --- /api/health ----------------------------------------------------------------


def test_health_is_ok_when_the_database_answers_and_the_data_dir_is_writable(raw_client, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_path", tmp_path / "data.db")
    before = sorted(tmp_path.glob("*"))
    resp = raw_client.get("/api/health")
    assert (resp.status_code, resp.json()) == (200, {"status": "ok"})
    assert sorted(tmp_path.glob("*")) == before  # the probe file is gone again


def test_health_is_degraded_when_the_database_does_not_answer(raw_client):
    class Broken:
        def __enter__(self):
            raise RuntimeError("database is locked")

        def __exit__(self, *exc):
            return False

    with patch.object(main, "SessionLocal", Broken):
        resp = raw_client.get("/api/health")
    assert resp.status_code == 503
    assert resp.json() == {"status": "degraded", "problems": ["database"]}


def test_health_is_degraded_when_the_data_dir_is_not_writable(raw_client, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_path", tmp_path / "missing" / "data.db")
    resp = raw_client.get("/api/health")
    assert resp.status_code == 503
    assert resp.json() == {"status": "degraded", "problems": ["data_dir"]}


def test_health_needs_no_login_and_stays_out_of_the_request_log(raw_client, caplog):
    import logging

    caplog.set_level(logging.DEBUG)
    assert raw_client.get("/api/health").status_code == 200
    assert not [r for r in caplog.records if r.name.startswith("app") and "/api/health" in r.getMessage()]
