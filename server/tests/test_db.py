from __future__ import annotations

import pytest
from sqlalchemy import text

from app.db import SessionLocal, engine
from tests.conftest import startup_against


def test_sqlite_uses_wal_mode_and_a_busy_timeout():
    """A long-running writer (a full library scan is the worst case, one
    transaction touching every row) must not make every other request fail
    outright with "database is locked" for the whole duration - see
    app.db for the full reasoning."""
    with engine.connect() as conn:
        journal_mode = conn.execute(text("PRAGMA journal_mode")).scalar()
        busy_timeout = conn.execute(text("PRAGMA busy_timeout")).scalar()
        foreign_keys = conn.execute(text("PRAGMA foreign_keys")).scalar()

    assert journal_mode == "wal"
    assert busy_timeout == 30000
    assert foreign_keys == 1


def test_commits_do_not_wait_for_the_disk_on_every_connection(session_factory):
    """synchronous=NORMAL (1) is a per-connection setting and SQLite's own
    default is FULL (2): every commit then waits for an fsync."""
    with engine.connect() as conn:
        assert conn.execute(text("PRAGMA synchronous")).scalar() == 1
    first = session_factory()
    second = session_factory()
    try:
        # Two sessions held at once are two different pooled connections.
        assert first.execute(text("PRAGMA synchronous")).scalar() == 1
        assert second.execute(text("PRAGMA synchronous")).scalar() == 1
    finally:
        first.close()
        second.close()


def test_session_does_not_expire_objects_on_commit():
    """Long operations that commit in batches (see scanner.scan_library_root)
    keep using the same ORM objects across those commits - expiring them
    would force an implicit re-SELECT on next attribute access instead of
    just reading the value already held in Python."""
    assert SessionLocal.kw.get("expire_on_commit") is False


def test_pool_is_sized_for_a_burst_of_art_requests():
    """A grid view requests art for every tile at once - hundreds to
    thousands of brief, near-simultaneous connections on a large library.
    The default pool (5 + 10 overflow) times out under exactly that burst
    even though each request holds its connection only briefly - see
    app.db."""
    assert engine.pool.size() == 20
    assert engine.pool._max_overflow == 60


def test_media_item_indexes_exist():
    """/artists, /albums, and /mixes/options filter and group by
    (library_root_id, kind, artist/album) or (..., genre) - without an
    index those were full table scans, cheap at a few thousand rows, a
    real cost at the library sizes this app handles. create_all
    only adds indexes when creating the table itself, so a database that
    already has media_items (every production database) needs this run
    explicitly - see app.migrations.ensure_media_item_indexes."""
    from app.migrations import ensure_media_item_indexes
    from app.models import Base

    Base.metadata.create_all(bind=engine)  # this test may run before any app startup
    ensure_media_item_indexes()
    with engine.connect() as conn:
        names = {row[1] for row in conn.exec_driver_sql("PRAGMA index_list(media_items)")}
    assert "idx_media_items_root_kind_artist_album" in names
    assert "idx_media_items_root_kind_genre" in names
    assert "idx_media_items_root_created_at" in names


def test_media_item_release_date_column_migration():
    """The release-date upgrade preserves a legacy `year` column until a
    separately versioned migration can remove it safely."""
    from app.migrations import ensure_media_item_release_date_column
    from app.models import Base

    Base.metadata.create_all(bind=engine)
    with engine.connect() as conn:
        conn.exec_driver_sql("ALTER TABLE media_items ADD COLUMN year INTEGER")
        conn.commit()
        columns = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(media_items)")}
    assert "year" in columns

    for _ in range(2):  # must be idempotent
        ensure_media_item_release_date_column()
        with engine.connect() as conn:
            columns = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(media_items)")}
        assert "year" in columns
        assert "release_date" in columns


def test_play_source_history_materialized_fields_migration_and_index(tmp_path):
    from unittest.mock import patch

    from sqlalchemy import create_engine

    from app.migrations import ensure_play_source_history_columns

    old_engine = create_engine(f"sqlite:///{tmp_path / 'play-source-history.db'}")
    with old_engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE play_source_history ("
            "id INTEGER PRIMARY KEY, user_id INTEGER, kind TEXT, label TEXT, "
            "payload_json TEXT, updated_at DATETIME)"
        )

    with patch("app.migrations.engine", old_engine):
        ensure_play_source_history_columns()
        ensure_play_source_history_columns()

    with old_engine.connect() as conn:
        columns = {
            row[1] for row in conn.exec_driver_sql("PRAGMA table_info(play_source_history)")
        }
        indexes = {
            row[1] for row in conn.exec_driver_sql("PRAGMA index_list(play_source_history)")
        }
    assert {"art_media_item_id", "preview_media_item_ids_json"} <= columns
    assert "idx_play_source_history_user_updated_at" in indexes


def test_musicbrainz_metadata_columns_migration(tmp_path):
    from unittest.mock import patch

    from sqlalchemy import create_engine

    from app.migrations import ensure_musicbrainz_metadata_columns

    old_engine = create_engine(f"sqlite:///{tmp_path / 'musicbrainz-metadata.db'}")
    with old_engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE media_items (id INTEGER PRIMARY KEY)")

    with patch("app.migrations.engine", old_engine):
        ensure_musicbrainz_metadata_columns()
        ensure_musicbrainz_metadata_columns()

    with old_engine.connect() as conn:
        columns = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(media_items)")}
    assert {
        "musicbrainz_metadata_json",
        "musicbrainz_fetched_at",
        "musicbrainz_isrc",
        "musicbrainz_lookup_status",
        "musicbrainz_last_attempt_at",
        "musicbrainz_last_error",
        "musicbrainz_isrc_scanned_at",
    } <= columns


def test_album_artist_columns_migration(tmp_path):
    from unittest.mock import patch

    from sqlalchemy import create_engine

    from app.migrations import ensure_album_artist_columns

    old_engine = create_engine(f"sqlite:///{tmp_path / 'album-artist.db'}")
    with old_engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE media_items (id INTEGER PRIMARY KEY)")

    with patch("app.migrations.engine", old_engine):
        ensure_album_artist_columns()
        ensure_album_artist_columns()

    with old_engine.connect() as conn:
        columns = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(media_items)")}
    assert {"album_artist", "album_artist_scanned_at"} <= columns


def test_playlist_description_column_migration(tmp_path):
    from unittest.mock import patch

    from sqlalchemy import create_engine

    from app.migrations import ensure_playlist_columns

    old_engine = create_engine(f"sqlite:///{tmp_path / 'playlists.db'}")
    with old_engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE playlists (id INTEGER PRIMARY KEY, user_id INTEGER, name TEXT, "
            "is_auto_generated BOOLEAN, auto_kind TEXT, mix_genre TEXT)"
        )
        conn.exec_driver_sql("INSERT INTO playlists (user_id, name) VALUES (1, 'Old')")

    with patch("app.migrations.engine", old_engine):
        ensure_playlist_columns()
        ensure_playlist_columns()

    with old_engine.connect() as conn:
        columns = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(playlists)")}
        description = conn.exec_driver_sql("SELECT description FROM playlists").scalar_one()
    assert "description" in columns
    assert description is None


def test_media_item_loudness_columns_migration(tmp_path):
    from unittest.mock import patch

    from sqlalchemy import create_engine

    from app.migrations import ensure_media_item_loudness_columns

    old_engine = create_engine(f"sqlite:///{tmp_path / 'loudness.db'}")
    with old_engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE media_items (id INTEGER PRIMARY KEY)")

    with patch("app.migrations.engine", old_engine):
        ensure_media_item_loudness_columns()
        ensure_media_item_loudness_columns()

    with old_engine.connect() as conn:
        columns = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(media_items)")}
    assert {"replaygain_track_gain", "replaygain_album_gain", "loudness_scanned_at"} <= columns


def test_want_list_media_item_column_migration(tmp_path):
    from unittest.mock import patch

    from sqlalchemy import create_engine

    from app.migrations import ensure_want_list_columns

    old_engine = create_engine(f"sqlite:///{tmp_path / 'want-list.db'}")
    with old_engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE want_list_items (id INTEGER PRIMARY KEY, user_id INTEGER, "
            "artist TEXT, title TEXT, station_name TEXT, created_at DATETIME)"
        )

    with patch("app.migrations.engine", old_engine):
        ensure_want_list_columns()
        ensure_want_list_columns()

    with old_engine.connect() as conn:
        columns = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(want_list_items)")}
    assert "media_item_id" in columns


def test_user_accent_color_column_migration(tmp_path):
    """A pre-existing users table (created before account-wide accent
    colors) gets the new column, idempotently."""
    from unittest.mock import patch

    from sqlalchemy import create_engine

    from app.migrations import ensure_user_columns

    old_engine = create_engine(f"sqlite:///{tmp_path / 'users-accent.db'}")
    with old_engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT, "
            "password_hash TEXT, is_admin BOOLEAN, created_at DATETIME)"
        )

    with patch("app.migrations.engine", old_engine):
        ensure_user_columns()
        ensure_user_columns()

    with old_engine.connect() as conn:
        columns = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(users)")}
    assert "accent_color" in columns


def test_seed_default_user_migrates_legacy_users_table(tmp_path):
    """Startup, not just the helper, must add `users.accent_color`. A
    database created before account-wide accent colors never runs the
    ALTER otherwise, and PUT /api/auth/accent-color and GET /api/auth/me
    would 500 on such a database."""

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app import main

    legacy_engine = create_engine(f"sqlite:///{tmp_path / 'legacy-users.db'}")
    with legacy_engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT, "
            "password_hash TEXT, is_admin BOOLEAN, created_at DATETIME)"
        )
        # A pre-existing account, so the bootstrap insert can't be what
        # carries the column.
        conn.exec_driver_sql(
            "INSERT INTO users (id, username, password_hash, is_admin) "
            "VALUES (1, 'legacy', 'x', 1)"
        )
    factory = sessionmaker(bind=legacy_engine)

    with startup_against(factory):
        main.seed_default_user()

    with legacy_engine.connect() as conn:
        columns = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(users)")}
    assert "accent_color" in columns


def test_playback_device_columns_migration_and_restart_disconnects(tmp_path):
    from unittest.mock import patch

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app import main, migrations
    from app.models import Device, User

    old_engine = create_engine(f"sqlite:///{tmp_path / 'playback-devices.db'}")
    with old_engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE devices (id INTEGER PRIMARY KEY, user_id INTEGER, name TEXT, device_type TEXT)"
        )
    with patch.object(migrations, "engine", old_engine):
        migrations.ensure_playback_columns()
        migrations.ensure_playback_columns()
    with old_engine.connect() as conn:
        columns = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(devices)")}
    assert {"client_token", "is_connected"} <= columns

    test_engine = create_engine("sqlite:///:memory:")
    from app.models import Base

    Base.metadata.create_all(test_engine)
    factory = sessionmaker(bind=test_engine)
    session = factory()
    user = User(username="device-test", password_hash="x")
    session.add(user)
    session.flush()
    session.add(Device(user_id=user.id, name="Phone", is_connected=True))
    session.commit()
    with patch.object(main, "SessionLocal", factory):
        main.mark_playback_devices_disconnected()
    assert session.query(Device).one().is_connected is False
    session.close()


def test_seed_backfills_legacy_primary_genres(db_session, session_factory):

    from app.main import seed_default_user
    from app.models import LibraryRoot, MediaItem, MediaKind

    root = LibraryRoot(path="/legacy", display_name="Legacy")
    db_session.add(root)
    db_session.commit()
    item = MediaItem(
        library_root_id=root.id,
        kind=MediaKind.track,
        relative_path="legacy.mp3",
        title="Legacy",
        genre="Rock",
    )
    db_session.add(item)
    db_session.commit()

    with startup_against(session_factory):
        seed_default_user()

    db_session.expire_all()
    assert {genre.name for genre in db_session.get(MediaItem, item.id).genres} == {"Rock"}


def test_video_folder_column_migration_backfills_and_indexes_old_rows(tmp_path):
    from unittest.mock import patch

    from sqlalchemy import create_engine

    from app.migrations import ensure_media_item_video_folder_column

    old_engine = create_engine(f"sqlite:///{tmp_path / 'video-folder.db'}")
    with old_engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE media_items (id INTEGER PRIMARY KEY, relative_path TEXT NOT NULL)"
        )
        conn.exec_driver_sql(
            "INSERT INTO media_items (id, relative_path) VALUES (1, 'Band/video.mp4'), (2, 'loose.mp4')"
        )

    with patch("app.migrations.engine", old_engine):
        ensure_media_item_video_folder_column()
        ensure_media_item_video_folder_column()

    with old_engine.connect() as conn:
        assert conn.exec_driver_sql(
            "SELECT video_folder FROM media_items ORDER BY id"
        ).scalars().all() == ["Band", "Unsorted"]
        indexes = {
            row[1] for row in conn.exec_driver_sql("PRAGMA index_list(media_items)")
        }
        assert "ix_media_items_video_folder" in indexes


def test_startup_drops_the_old_ratings_table(tmp_path):
    from unittest.mock import patch

    from sqlalchemy import create_engine, inspect

    from app.migrations import drop_ratings_table

    old_engine = create_engine(f"sqlite:///{tmp_path / 'ratings.db'}")
    with old_engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE ratings (user_id INTEGER, media_item_id INTEGER, "
            "stars INTEGER, created_at DATETIME, PRIMARY KEY (user_id, media_item_id))"
        )
        conn.exec_driver_sql("INSERT INTO ratings VALUES (1, 1, 5, NULL)")

    with patch("app.migrations.engine", old_engine):
        drop_ratings_table()
        # Idempotent: a second start with no ratings table is fine too.
        drop_ratings_table()

    assert "ratings" not in inspect(old_engine).get_table_names()


def test_startup_creates_the_preference_tables(tmp_path):

    from sqlalchemy import create_engine, inspect
    from sqlalchemy.orm import sessionmaker

    from app import main

    engine = create_engine(f"sqlite:///{tmp_path / 'fresh.db'}")
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE ratings (user_id INTEGER, media_item_id INTEGER, stars INTEGER)"
        )
    factory = sessionmaker(bind=engine)
    with startup_against(factory):
        main.seed_default_user()

    tables = set(inspect(engine).get_table_names())
    assert "ratings" not in tables
    assert {"track_dislikes", "collection_preferences"} <= tables


def test_media_item_file_size_column_migration(tmp_path):
    from unittest.mock import patch

    from sqlalchemy import create_engine

    from app.migrations import ensure_media_item_file_size_column

    old_engine = create_engine(f"sqlite:///{tmp_path / 'size.db'}")
    with old_engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE media_items (id INTEGER PRIMARY KEY)")

    with patch("app.migrations.engine", old_engine):
        ensure_media_item_file_size_column()
        ensure_media_item_file_size_column()

    with old_engine.connect() as conn:
        columns = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(media_items)")}
    assert "file_size" in columns


def test_the_suite_ignores_atriolyric_settings_exported_in_the_shell(tmp_path):
    """An exported ATRIOLYRIC_DATABASE_PATH does not win over the suite's
    own throwaway path (every test client's startup would migrate that real
    database and wipe the hls/ and video_cache/ folders beside it), and no
    other exported setting (COOKIE_SECURE=true breaks cookie auth over the
    test client) changes what the suite tests."""
    import os
    import subprocess
    import sys
    from pathlib import Path

    real = tmp_path / "real" / "atriolyric.db"
    script = (
        "import tests.conftest\n"
        "from app.config import settings\n"
        "print(settings.database_path)\n"
        "print(settings.cookie_secure)\n"
    )
    env = {
        **os.environ,
        "ATRIOLYRIC_DATABASE_PATH": str(real),
        "ATRIOLYRIC_COOKIE_SECURE": "true",
    }
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    database_path, cookie_secure = result.stdout.split()
    assert Path(database_path) != real
    assert Path(database_path).name == "atriolyric-test.db"
    assert cookie_secure == "false"
    assert not real.parent.exists()


def test_the_test_database_is_configured_like_production(session_factory):
    """The suite's per-test database must catch what production would: a
    dangling foreign key fails, objects stay loaded across a commit, and
    the schema went through the migration steps."""
    from sqlalchemy.exc import IntegrityError

    from app.migrations import latest_schema_version
    from app.models import LibraryTask, SystemSetting

    assert session_factory.kw["expire_on_commit"] is False
    db = session_factory()
    try:
        assert db.execute(text("PRAGMA foreign_keys")).scalar() == 1
        assert db.execute(text("PRAGMA journal_mode")).scalar() == "wal"
        assert db.get(SystemSetting, "schema_version").value == str(latest_schema_version())
        db.add(LibraryTask(library_root_id=999, kind="scan", status="running"))
        with pytest.raises(IntegrityError):
            db.commit()
    finally:
        db.close()


@pytest.mark.real_background_jobs
def test_tests_marked_real_background_jobs_get_the_lifespan_starters(session_factory):
    """The suite skips the lifespan's backfill threads and scheduler by
    default (tests/conftest.py); the marker restores them. The threads are
    observed rather than run, and the real scheduler task is started and
    cancelled at shutdown."""
    from unittest.mock import patch

    from fastapi.testclient import TestClient

    from app import main

    real_loop = main.discovery_scheduler_loop
    started: list[str] = []

    async def scheduler() -> None:
        started.append("scheduler")
        await real_loop()

    with (
        patch.object(main, "_start_art_thumb_backfill") as art_backfill,
        patch.object(main, "start_loudness_backfill") as loudness_backfill,
        patch.object(main, "discovery_scheduler_loop", scheduler),
    ):
        with TestClient(main.app) as client:
            assert client.get("/api/health").status_code == 200
    art_backfill.assert_called_once_with()
    loudness_backfill.assert_called_once_with()
    assert started == ["scheduler"]


def test_sql_errors_do_not_expose_bound_parameters():
    """An unhandled SQL error is logged with its exception text; bound values
    (password hashes, session digests, credentials) must not be in it."""
    from sqlalchemy.exc import OperationalError

    with engine.connect() as conn:
        with pytest.raises(OperationalError) as excinfo:
            conn.execute(text("SELECT * FROM no_such_table WHERE x = :v"), {"v": "s3cret-value"})
    assert "s3cret-value" not in str(excinfo.value)
    assert "s3cret-value" not in repr(excinfo.value)
