from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from sqlalchemy import Column, UniqueConstraint, create_engine, event, text
from sqlalchemy.orm import sessionmaker

from app import main, migrations
from app.db import configure_sqlite_connection, make_session_factory
from app.models import Base
from tests.conftest import startup_against


def _factory(tmp_path, name="db.sqlite"):
    engine = create_engine(f"sqlite:///{tmp_path / name}")
    return engine, sessionmaker(bind=engine)


def _version(engine) -> str | None:
    with engine.connect() as conn:
        return conn.exec_driver_sql(
            "SELECT value FROM system_settings WHERE key = 'schema_version'"
        ).scalar()


def _columns(engine, table) -> set[str]:
    with engine.connect() as conn:
        return {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})")}


def test_pre_versioning_database_upgrades_to_the_latest_version(tmp_path):
    engine, factory = _factory(tmp_path)
    with engine.begin() as conn:
        # Tables as an early release created them, and no schema_version row.
        conn.exec_driver_sql(
            "CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT, "
            "password_hash TEXT, is_admin BOOLEAN, created_at DATETIME)"
        )
        conn.exec_driver_sql("INSERT INTO users (username, password_hash, is_admin) VALUES ('old', 'x', 1)")
        conn.exec_driver_sql(
            "CREATE TABLE playlists (id INTEGER PRIMARY KEY, user_id INTEGER, name TEXT, "
            "is_auto_generated BOOLEAN, created_at DATETIME)"
        )
        conn.exec_driver_sql("CREATE TABLE ratings (user_id INTEGER, media_item_id INTEGER, stars INTEGER)")

    with startup_against(factory):
        main.seed_default_user()

    assert _version(engine) == str(migrations.latest_schema_version())
    assert "accent_color" in _columns(engine, "users")
    assert {"auto_kind", "mix_genre", "description", "preview_media_item_ids_json"} <= _columns(engine, "playlists")
    assert _columns(engine, "ratings") == set()  # dropped


def test_a_second_boot_runs_no_steps(tmp_path):
    engine, factory = _factory(tmp_path)
    with startup_against(factory):
        assert migrations.run_migrations() == migrations.latest_schema_version()
        step = Mock(__name__="spy")
        with patch.object(migrations, "MIGRATIONS", (*migrations.MIGRATIONS[:-1], step)):
            assert migrations.run_migrations() == 0
    step.assert_not_called()


def test_a_fresh_database_gets_the_latest_version(tmp_path):
    engine, factory = _factory(tmp_path)
    with startup_against(factory):
        main.seed_default_user()
    assert _version(engine) == str(len(migrations.MIGRATIONS))


def test_only_new_steps_run_and_each_is_recorded(tmp_path):
    engine, factory = _factory(tmp_path)
    with startup_against(factory):
        migrations.run_migrations()
        ran: list[str] = []
        new_step = Mock(__name__="add_something", side_effect=lambda: ran.append("new"))
        failing = Mock(__name__="breaks", side_effect=RuntimeError("disk full"))
        with patch.object(migrations, "MIGRATIONS", (*migrations.MIGRATIONS, new_step, failing)):
            with pytest.raises(RuntimeError):
                migrations.run_migrations()
    assert ran == ["new"]
    # The step that completed stays recorded; the failed one runs again next boot.
    assert _version(engine) == str(len(migrations.MIGRATIONS) + 1)


def test_unreadable_version_counts_as_zero(tmp_path):
    engine, factory = _factory(tmp_path)
    with startup_against(factory):
        migrations.run_migrations()
        with engine.begin() as conn:
            conn.exec_driver_sql("UPDATE system_settings SET value = 'garbage' WHERE key = 'schema_version'")
        assert migrations.run_migrations() == migrations.latest_schema_version()
    assert _version(engine) == str(migrations.latest_schema_version())


def test_a_newer_database_is_refused_unless_overridden(tmp_path, caplog, monkeypatch):
    engine, factory = _factory(tmp_path)
    with startup_against(factory):
        migrations.run_migrations()
        with engine.begin() as conn:
            conn.exec_driver_sql("UPDATE system_settings SET value = '999' WHERE key = 'schema_version'")
        with pytest.raises(migrations.NewerSchemaError) as refused:
            migrations.run_migrations()
        assert "999" in str(refused.value)
        assert str(migrations.latest_schema_version()) in str(refused.value)
        assert migrations.ALLOW_NEWER_SCHEMA_ENV in str(refused.value)

        monkeypatch.setenv(migrations.ALLOW_NEWER_SCHEMA_ENV, "true")
        caplog.set_level(logging.WARNING, logger="app.migrations")
        assert migrations.run_migrations() == 0
    assert "newer than this release" in caplog.text
    assert _version(engine) == "999"
    assert not (tmp_path / "backups").exists()


def test_audio_and_peak_columns_are_added_and_measured_tracks_requeued(tmp_path):
    engine, factory = _factory(tmp_path)
    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT)")
        conn.exec_driver_sql(
            "CREATE TABLE media_items (id INTEGER PRIMARY KEY, loudness_scanned_at DATETIME)"
        )
        conn.exec_driver_sql("INSERT INTO media_items (loudness_scanned_at) VALUES ('2026-01-01')")
    with startup_against(factory):
        migrations.ensure_user_audio_setting_columns()
        migrations.ensure_media_item_peak_column()
        migrations.ensure_user_audio_setting_columns()  # idempotent
        migrations.ensure_media_item_peak_column()
    assert {"volume_leveling", "eq_preset", "eq_gains_json"} <= _columns(engine, "users")
    assert "replaygain_track_peak" in _columns(engine, "media_items")
    with engine.connect() as conn:
        assert conn.exec_driver_sql("SELECT loudness_scanned_at FROM media_items").scalar() is None


def test_library_task_held_removal_columns_are_added(tmp_path):
    engine, factory = _factory(tmp_path)
    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE library_tasks (id INTEGER PRIMARY KEY, stage TEXT)")
        conn.exec_driver_sql("INSERT INTO library_tasks (stage) VALUES (NULL)")
    with startup_against(factory):
        migrations.ensure_library_task_missing_held_columns()
        migrations.ensure_library_task_missing_held_columns()  # idempotent
    assert {"missing_held", "note"} <= _columns(engine, "library_tasks")
    with engine.connect() as conn:
        assert conn.exec_driver_sql("SELECT missing_held, note FROM library_tasks").one() == (0, None)
    assert migrations.ensure_library_task_missing_held_columns in migrations.MIGRATIONS


# --- Model/migration parity -------------------------------------------------
# create_all never alters a table that already exists, so a column or index
# added to a model without a migration step exists on fresh databases (and
# in every other test) but not on upgraded ones. This upgrades a database
# frozen at an old release and compares it with today's models.

FROZEN_SCHEMA = Path(__file__).parent / "fixtures" / "schema_78621d2.sql"


def _affinity(declared: str) -> str:
    """SQLite's type affinity for a declared column type (its documented
    rules, in order), so e.g. VARCHAR and TEXT compare equal."""
    declared = declared.upper()
    if "INT" in declared:
        return "INTEGER"
    if any(word in declared for word in ("CHAR", "CLOB", "TEXT")):
        return "TEXT"
    if "BLOB" in declared or not declared:
        return "BLOB"
    if any(word in declared for word in ("REAL", "FLOA", "DOUB")):
        return "REAL"
    return "NUMERIC"


def _schema_drift(engine) -> list[str]:
    """Everything Base.metadata declares that the database lacks or has
    differently: tables, columns (affinity and NOT NULL), indexes (columns
    and uniqueness) and unique constraints."""
    dialect = engine.dialect
    problems: list[str] = []
    with engine.connect() as conn:
        tables = {
            row[0]
            for row in conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        for table in Base.metadata.sorted_tables:
            if table.name not in tables:
                problems.append(f"missing table {table.name}")
                continue
            columns = {
                row[1]: row for row in conn.exec_driver_sql(f"PRAGMA table_info({table.name})")
            }
            for column in table.columns:
                row = columns.get(column.name)
                if row is None:
                    problems.append(f"missing column {table.name}.{column.name}")
                    continue
                _cid, _name, db_type, db_notnull, _default, db_pk = row
                model_type = column.type.compile(dialect=dialect)
                if _affinity(db_type) != _affinity(model_type):
                    problems.append(
                        f"{table.name}.{column.name} is {db_type}, model says {model_type}"
                    )
                if bool(db_notnull or db_pk) != (not column.nullable):
                    problems.append(
                        f"{table.name}.{column.name} NOT NULL is {bool(db_notnull)}, "
                        f"model says {not column.nullable}"
                    )
            indexes = {}
            for _seq, name, unique, *_ in conn.exec_driver_sql(f"PRAGMA index_list({table.name})"):
                cols = tuple(
                    r[2] for r in conn.exec_driver_sql(f"PRAGMA index_info({name})")
                )
                indexes[name] = (cols, bool(unique))
            for index in table.indexes:
                # PRAGMA index_info names an expression part (lower(artist))
                # None, so compare it as None too.
                parts = tuple(e.name if isinstance(e, Column) else None for e in index.expressions)
                expected = (parts, bool(index.unique))
                if index.name not in indexes:
                    problems.append(f"missing index {index.name}")
                elif indexes[index.name] != expected:
                    problems.append(
                        f"index {index.name} is {indexes[index.name]}, model says {expected}"
                    )
            unique_sets = {cols for cols, unique in indexes.values() if unique}
            for constraint in table.constraints:
                if isinstance(constraint, UniqueConstraint):
                    cols = tuple(c.name for c in constraint.columns)
                    if cols not in unique_sets:
                        problems.append(f"missing unique constraint {table.name}{cols}")
    return problems


def _upgraded_frozen_database(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    # Production's connection setup, foreign keys included.
    event.listen(engine, "connect", lambda conn, _record: configure_sqlite_connection(conn))
    raw = engine.raw_connection()
    try:
        raw.executescript(FROZEN_SCHEMA.read_text())
    finally:
        raw.close()
    assert _schema_drift(engine), "the frozen schema should predate today's models"
    with startup_against(make_session_factory(engine)):
        migrations.run_migrations()
    return engine


def test_an_old_database_upgrades_to_exactly_what_the_models_declare(tmp_path):
    engine = _upgraded_frozen_database(tmp_path)
    assert _schema_drift(engine) == []
    assert _version(engine) == str(migrations.latest_schema_version())


def test_a_fresh_database_matches_the_models_too(tmp_path):
    # The check itself must agree with create_all, or the test above could
    # only pass by accident.
    engine = create_engine(f"sqlite:///{tmp_path / 'fresh.db'}")
    with startup_against(make_session_factory(engine)):
        migrations.run_migrations()
    assert _schema_drift(engine) == []


def test_playback_state_revision_column_is_added(tmp_path):
    engine, factory = _factory(tmp_path)
    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE playback_state (user_id INTEGER PRIMARY KEY)")
        conn.exec_driver_sql("INSERT INTO playback_state (user_id) VALUES (1)")
    with startup_against(factory):
        migrations.ensure_playback_state_revision_column()
        migrations.ensure_playback_state_revision_column()  # idempotent
    assert "revision" in _columns(engine, "playback_state")
    with engine.connect() as conn:
        assert conn.exec_driver_sql("SELECT revision FROM playback_state").scalar() == 0
    assert migrations.ensure_playback_state_revision_column in migrations.MIGRATIONS


def test_playback_state_queue_origin_column_reaches_an_up_to_date_database(tmp_path):
    """The column has a step of its own, so a database whose earlier steps
    have all run still gets it."""
    engine, factory = _factory(tmp_path)
    with startup_against(make_session_factory(engine)):
        migrations.run_migrations()
        step = migrations.MIGRATIONS.index(migrations.ensure_playback_state_queue_origin_column)
        with engine.begin() as conn:
            conn.exec_driver_sql("ALTER TABLE playback_state DROP COLUMN queue_origin_json")
            conn.exec_driver_sql(
                "UPDATE system_settings SET value = ? WHERE key = 'schema_version'", (str(step),)
            )
        assert "queue_origin_json" not in _columns(engine, "playback_state")
        migrations.run_migrations()
        migrations.ensure_playback_state_queue_origin_column()  # idempotent
    assert "queue_origin_json" in _columns(engine, "playback_state")
    assert _version(engine) == str(migrations.latest_schema_version())
    assert migrations.ensure_playback_state_queue_origin_column in migrations.MIGRATIONS


def test_blank_tag_strings_become_what_a_scan_stores(tmp_path):
    from app.models import Album, Artist, LibraryRoot, MediaItem, MediaKind, TrackArtist

    engine = create_engine(f"sqlite:///{tmp_path / 'blank.db'}")
    event.listen(engine, "connect", lambda conn, _record: configure_sqlite_connection(conn))
    factory = make_session_factory(engine)
    with startup_against(factory):
        migrations.run_migrations()
    db = factory()
    root = LibraryRoot(path="/blank", display_name="Blank")
    db.add(root)
    db.flush()
    blank = MediaItem(
        library_root_id=root.id, kind=MediaKind.track, relative_path="Rock/01 Song.flac",
        title="", artist="", album_artist="", album="", genre="",
    )
    kept = MediaItem(
        library_root_id=root.id, kind=MediaKind.track, relative_path="b.flac",
        title="Kept", artist="Band", album="LP", genre="Rock",
    )
    db.add_all([blank, kept])
    db.flush()
    orphan = Album(owner="", title="", owner_key="", title_key="")
    db.add_all([orphan, Artist(name="", normalized_name="")])
    db.commit()

    with startup_against(factory):
        migrations.backfill_blank_tag_strings()

    db.expire_all()
    blank = db.get(MediaItem, blank.id)
    assert blank.title == "01 Song"
    assert (blank.artist, blank.album_artist, blank.album, blank.genre) == (None, None, None, None)
    assert blank.album_id is None
    assert db.query(TrackArtist).filter_by(media_item_id=blank.id).count() == 0
    kept = db.get(MediaItem, kept.id)
    assert (kept.title, kept.artist, kept.album, kept.genre) == ("Kept", "Band", "LP", "Rock")
    # Rows no track uses any more are pruned, as after a scan.
    assert db.query(Album).filter_by(title="").count() == 0
    assert db.query(Artist).filter_by(name="").count() == 0
    db.close()
    assert ("backfill_blank_tag_strings", migrations.backfill_blank_tag_strings) in migrations.DATA_BACKFILLS


def _migrated_factory(tmp_path, name):
    engine = create_engine(f"sqlite:///{tmp_path / name}")
    event.listen(engine, "connect", lambda conn, _record: configure_sqlite_connection(conn))
    factory = make_session_factory(engine)
    with startup_against(factory):
        migrations.run_migrations()
    return engine, factory


def test_duplicate_favourites_playlists_are_merged_before_the_index(tmp_path):
    from app.models import (
        CustomArt,
        LibraryRoot,
        MediaItem,
        MediaKind,
        Playlist,
        PlaylistItem,
        User,
    )

    engine, factory = _migrated_factory(tmp_path, "fav.db")
    with engine.begin() as conn:  # as a database from before the index
        conn.exec_driver_sql("DROP INDEX uq_playlists_one_favorites_per_user")
    db = factory()
    user = User(username="u", password_hash="x")
    root = LibraryRoot(path="/fav", display_name="Fav")
    db.add_all([user, root])
    db.flush()
    songs = [
        MediaItem(library_root_id=root.id, kind=MediaKind.track, relative_path=f"{n}.flac", title=n)
        for n in ("a", "b", "c")
    ]
    db.add_all(songs)
    db.flush()
    a, b, c = (s.id for s in songs)
    oldest, extra1, extra2 = (
        Playlist(user_id=user.id, name="Favourites", is_auto_generated=True, auto_kind="favorites")
        for _ in range(3)
    )
    db.add_all([oldest, extra1, extra2])
    db.flush()
    for playlist, media in ((oldest, [a]), (extra1, [b, a]), (extra2, [c, b])):
        db.add_all(PlaylistItem(playlist_id=playlist.id, media_item_id=m, position=i) for i, m in enumerate(media))
    db.add(CustomArt(entity_type="playlist", entity_key=str(extra1.id), path="x.jpg", thumb_path="x.thumb.jpg"))
    db.commit()
    oldest_id = oldest.id

    with startup_against(factory):
        migrations.ensure_one_favorites_playlist_per_user()
        migrations.ensure_one_favorites_playlist_per_user()  # idempotent

    db.expire_all()
    favourites = db.query(Playlist).filter_by(auto_kind="favorites").all()
    assert [p.id for p in favourites] == [oldest_id]
    items = sorted(favourites[0].items, key=lambda i: (i.position, i.id))
    assert [i.media_item_id for i in items] == [a, b, c]
    assert db.query(CustomArt).count() == 0
    db.close()
    assert _schema_drift(engine) == []
    assert migrations.ensure_one_favorites_playlist_per_user in migrations.MIGRATIONS


def test_missing_favourites_playlists_are_rebuilt_from_the_likes(tmp_path):
    from app.models import (
        Favorite,
        LibraryRoot,
        MediaItem,
        MediaKind,
        Playlist,
        PlaylistItem,
        User,
    )

    engine, factory = _migrated_factory(tmp_path, "likes.db")
    db = factory()
    users = [User(username=n, password_hash="x") for n in ("lost", "partial", "none")]
    root = LibraryRoot(path="/likes", display_name="Likes")
    db.add_all([*users, root])
    db.flush()
    songs = [
        MediaItem(library_root_id=root.id, kind=MediaKind.track, relative_path=f"{n}.flac", title=n)
        for n in ("a", "b")
    ]
    db.add_all(songs)
    db.flush()
    lost, partial, _none = users
    a, b = (s.id for s in songs)
    db.add_all(Favorite(user_id=u.id, media_item_id=m) for u in (lost, partial) for m in (a, b))
    kept = Playlist(user_id=partial.id, name="Favourites", is_auto_generated=True, auto_kind="favorites")
    db.add(kept)
    db.flush()
    db.add(PlaylistItem(playlist_id=kept.id, media_item_id=b, position=0))
    db.commit()

    with startup_against(factory):
        migrations.backfill_favorites_playlists()

    db.expire_all()

    def listed(user):
        playlist = db.query(Playlist).filter_by(user_id=user.id, auto_kind="favorites").one()
        return [i.media_item_id for i in sorted(playlist.items, key=lambda i: i.position)]

    assert listed(lost) == [a, b]
    assert listed(partial) == [b, a]  # the existing order is kept; missing songs follow
    assert db.query(Playlist).filter_by(user_id=_none.id).count() == 0
    db.close()


# --- a copy before every upgrade --------------------------------------------


def test_a_fresh_database_and_an_up_to_date_one_are_not_copied(tmp_path):
    engine, factory = _factory(tmp_path)
    with startup_against(factory):
        migrations.run_migrations()
        migrations.run_migrations()
    assert not (tmp_path / "backups").exists()


def test_an_existing_database_is_copied_before_pending_steps_run(tmp_path, caplog):
    engine = create_engine(f"sqlite:///{tmp_path / 'live.db'}")
    event.listen(engine, "connect", lambda conn, _record: configure_sqlite_connection(conn))
    factory = make_session_factory(engine)
    latest = migrations.latest_schema_version()
    with startup_against(factory):
        migrations.run_migrations()
        caplog.set_level(logging.WARNING, logger="app.migrations")
        for run in range(5):
            with engine.begin() as conn:
                # A row only in the WAL file so far, and one step pending.
                conn.exec_driver_sql("INSERT INTO system_settings (key, value) VALUES (?, 'x')", (f"marker{run}",))
                conn.exec_driver_sql("UPDATE system_settings SET value = ? WHERE key = 'schema_version'", (str(latest - 1),))
            assert migrations.run_migrations() == 1
    copies = sorted((tmp_path / "backups").glob(f"atriolyric-v{latest - 1}-*.db"))
    assert len(copies) == migrations.BACKUPS_KEPT
    assert str(copies[-1]) in caplog.text
    import sqlite3

    newest = sqlite3.connect(copies[-1])
    try:
        keys = {row[0] for row in newest.execute("SELECT key FROM system_settings")}
        version = newest.execute("SELECT value FROM system_settings WHERE key = 'schema_version'").fetchone()
    finally:
        newest.close()
    assert "marker4" in keys
    assert version == (str(latest - 1),)  # the copy is from before the upgrade
    assert _version(engine) == str(latest)


def test_mix_previews_are_filled_by_a_data_backfill(tmp_path):

    from app.models import (
        LibraryRoot,
        MediaItem,
        MediaKind,
        Playlist,
        PlaylistItem,
        User,
    )

    engine, factory = _migrated_factory(tmp_path, "mix.db")
    db = factory()
    user = User(username="u", password_hash="x")
    root = LibraryRoot(path="/mix", display_name="Mix")
    db.add_all([user, root])
    db.flush()
    song = MediaItem(library_root_id=root.id, kind=MediaKind.track, relative_path="a.flac", title="A", artist="X")
    mix = Playlist(user_id=user.id, name="Rock", auto_kind="mix")
    db.add_all([song, mix])
    db.flush()
    db.add(PlaylistItem(playlist_id=mix.id, media_item_id=song.id, position=0))
    db.add(Playlist(user_id=user.id, name="Empty", auto_kind="mix"))
    db.commit()

    with startup_against(factory):
        migrations.ensure_playlist_preview_column()  # the schema step alone touches no row
        db.expire_all()
        stored = db.execute(text("SELECT preview_media_item_ids_json FROM playlists WHERE id = :id"), {"id": mix.id})
        assert stored.scalar_one() is None  # NULL: not filled yet (it reads as [])
        migrations.backfill_mix_preview_ids()

    db.expire_all()
    assert db.get(Playlist, mix.id).preview_media_item_ids_json == [song.id]
    db.close()
    assert ("backfill_mix_preview_ids", migrations.backfill_mix_preview_ids) in migrations.DATA_BACKFILLS


def test_play_history_gains_started_at_and_its_unique_index(tmp_path):
    # An existing play_history keeps its rows (started_at NULL)
    # and gets the index that makes a repeated report of one play a no-op.
    engine, factory = _factory(tmp_path)
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE play_history (id INTEGER PRIMARY KEY, user_id INTEGER, "
            "media_item_id INTEGER, played_at DATETIME)"
        )
        conn.exec_driver_sql("INSERT INTO play_history (user_id, media_item_id) VALUES (1, 2), (1, 2)")
    with startup_against(factory):
        migrations.ensure_play_history_started_at()
        migrations.ensure_play_history_started_at()  # idempotent
    assert "started_at" in _columns(engine, "play_history")
    with engine.connect() as conn:
        assert conn.exec_driver_sql("SELECT count(*) FROM play_history WHERE started_at IS NULL").scalar() == 2
        unique = {
            row[1]: row[2] for row in conn.exec_driver_sql("PRAGMA index_list(play_history)")
        }
    assert unique.get("uq_play_history_user_media_started_at") == 1
    assert migrations.ensure_play_history_started_at in migrations.MIGRATIONS


def test_listenbrainz_columns_are_dropped_and_rows_kept(tmp_path):
    # The two columns of the removed ListenBrainz fetch go; the
    # tracks and their other columns stay.
    engine, factory = _factory(tmp_path)
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE media_items (id INTEGER PRIMARY KEY, title TEXT, "
            "listenbrainz_metadata_json TEXT, listenbrainz_fetched_at DATETIME)"
        )
        conn.exec_driver_sql(
            "INSERT INTO media_items (title, listenbrainz_metadata_json) VALUES ('Song', '{}')"
        )
    with startup_against(factory):
        migrations.drop_listenbrainz_columns()
        migrations.drop_listenbrainz_columns()  # idempotent
    assert _columns(engine, "media_items") == {"id", "title"}
    with engine.connect() as conn:
        assert conn.exec_driver_sql("SELECT title FROM media_items").scalar() == "Song"
    assert migrations.drop_listenbrainz_columns in migrations.MIGRATIONS


def test_an_upgraded_database_has_no_listenbrainz_columns(tmp_path):
    engine = _upgraded_frozen_database(tmp_path)
    assert not {c for c in _columns(engine, "media_items") if c.startswith("listenbrainz_")}


def test_lastfm_and_deezer_data_is_removed_and_musicbrainz_tags_come_back(tmp_path, monkeypatch):
    """No Last.fm data and no stored Deezer data survives the upgrade, and
    the MusicBrainz tags Last.fm had taken over are restored from the stored
    recordings."""
    from app.config import settings
    from app.custom_art import store_custom_art
    from app.external_cache import store
    from app.metadata_taxonomy import replace_item_genres
    from app.models import (
        Artist,
        ArtistMetadataCache,
        CustomArt,
        DiscoveryState,
        ExternalCache,
        Genre,
        LibraryRoot,
        MediaItem,
        MediaItemGenre,
        MediaItemMusicBrainzTag,
        MediaKind,
        MusicBrainzTag,
        ScrobbleQueueItem,
        SystemSetting,
        User,
        UserIntegration,
        utcnow,
    )
    from tests.test_custom_art import _png

    monkeypatch.setattr(settings, "database_path", tmp_path / "art-home.db")
    engine, factory = _migrated_factory(tmp_path, "purge.db")
    db = factory()
    user = User(username="u", password_hash="x", dismissed_notices_json=["admin_lastfm_key", "other"])
    root = LibraryRoot(path="/p", display_name="P")
    db.add_all([user, root])
    db.flush()
    item = MediaItem(
        library_root_id=root.id, kind=MediaKind.track, relative_path="a.mp3", title="A", artist="X",
        musicbrainz_metadata_json={"tags": [{"name": "dreamy", "count": 3}], "genres": []},
        online_tags_fetched_at=utcnow(),
    )
    db.add(item)
    db.flush()
    tag = MusicBrainzTag(name="dreamy", normalized_name="dreamy")
    other = MusicBrainzTag(name="seen live", normalized_name="seen live")
    db.add_all([tag, other])
    db.flush()
    # Last.fm had taken "dreamy" over and added "seen live" and a genre.
    db.add_all([
        MediaItemMusicBrainzTag(media_item_id=item.id, musicbrainz_tag_id=tag.id, vote_count=80, source="lastfm"),
        MediaItemMusicBrainzTag(media_item_id=item.id, musicbrainz_tag_id=other.id, vote_count=50, source="lastfm"),
    ])
    replace_item_genres(db, item, ["Rock"], "lastfm")
    db.add_all([
        UserIntegration(user_id=user.id, provider="lastfm", credential="x"),
        UserIntegration(user_id=user.id, provider="listenbrainz", credential="y"),
        ScrobbleQueueItem(user_id=user.id, artist="X", title="A", started_at=utcnow(), next_attempt_at=utcnow()),
        SystemSetting(key="lastfm_server_api_key", value="secret"),
        ArtistMetadataCache(artist="X", similar_artists_json=["Y"], fetched_at=utcnow()),
        Artist(name="X", normalized_name="x", info_json={"lastfm": True}),
        DiscoveryState(user_id=user.id, explore_results_json=[{"artist": "Y", "preview_url": "https://cdnt-preview.dzcdn.net/p"}]),
    ])
    db.commit()
    store(db, "lastfm", "artist.getInfo", "x", {"listeners": 1})
    store(db, "deezer", "artist-pictures", "x", [{"url": "https://e-cdns-images.dzcdn.net/x.jpg"}])
    store(db, "musicbrainz", "artist", "m", {"name": "X"})
    store(db, "catalogart", "artist", "m", {"missing": True})
    picture = store_custom_art(db, "catalog_artist", "m", _png())
    cover = store_custom_art(db, "catalog_album", "g", _png())
    library_art = store_custom_art(db, "artist", "X", _png())
    picture_file = settings.custom_art_dir / picture.path
    cover_file = settings.custom_art_dir / cover.path
    library_file = settings.custom_art_dir / library_art.path
    assert picture_file.is_file() and cover_file.is_file()

    with startup_against(factory):
        migrations.purge_lastfm_and_deezer_data()
        migrations.restore_musicbrainz_tags_after_lastfm()
        migrations.drop_deezer_catalog_art()

    db.expire_all()
    assert {(row.provider, row.kind) for row in db.query(ExternalCache)} == {("musicbrainz", "artist")}
    assert db.query(ArtistMetadataCache).count() == 0
    assert [row.provider for row in db.query(UserIntegration)] == ["listenbrainz"]
    assert db.query(ScrobbleQueueItem).count() == 0
    assert db.get(SystemSetting, "lastfm_server_api_key") is None
    assert db.query(Artist).one().info_json is None
    assert db.get(DiscoveryState, user.id).explore_results_json == []
    rows = {(row.musicbrainz_tag_id, row.source, row.vote_count) for row in db.query(MediaItemMusicBrainzTag)}
    assert rows == {(tag.id, "musicbrainz", 3)}
    assert db.query(MediaItemGenre).filter_by(source="lastfm").count() == 0
    # Last.fm-only names are gone; every track counts as not yet asked for
    # ListenBrainz tags; the old notice mark is dropped, others kept, and a
    # user who had Last.fm linked is marked for the one-time notice.
    assert [t.name for t in db.query(MusicBrainzTag)] == ["dreamy"]
    assert db.query(Genre).count() == 0
    assert db.get(MediaItem, item.id).online_tags_fetched_at is None
    assert db.get(User, user.id).dismissed_notices == ["other", "lastfm_was_linked"]
    # Want-list art that may have come from Deezer is gone, files too; the
    # library art people chose themselves stays.
    assert {(row.entity_type, row.entity_key) for row in db.query(CustomArt)} == {("artist", "X")}
    assert not picture_file.exists() and not cover_file.exists()
    assert library_file.is_file()
    db.close()
    # Registered, and in this order (later steps may follow).
    steps = list(migrations.MIGRATIONS)
    assert steps.index(migrations.rename_lastfm_columns) < steps.index(migrations.purge_lastfm_and_deezer_data)
    names = [name for name, _run in migrations.DATA_BACKFILLS]
    assert (
        names.index("restore_musicbrainz_tags_after_lastfm")
        < names.index("drop_deezer_catalog_art")
        < names.index("compact_after_lastfm_and_deezer_purge")
    )
