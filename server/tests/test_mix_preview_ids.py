"""Persisted mix collage previews: refresh_all_genre_mixes and
add_specified_mix_playlist must materialize each mix's Home collage ids onto
the playlist row, so /api/library/mixes can serve them without the client
fetching every mix's full track list."""

from __future__ import annotations

from unittest.mock import patch

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.discovery import (
    add_specified_mix_playlist,
    refresh_all_genre_mixes,
)
from app.migrations import (
    backfill_mix_preview_ids,
    ensure_playlist_columns,
    ensure_playlist_preview_column,
)
from app.models import (
    Base,
    LibraryRoot,
    MediaItem,
    MediaKind,
    Playlist,
    PlaylistItem,
    User,
)


def test_refresh_all_genre_mixes_persists_preview_ids(db_session):
    """Every generated mix stores its collage snapshot: up to 4 ids, distinct
    by artist where possible."""
    user = db_session.query(User).one()
    root = LibraryRoot(path="/preview-mixes", display_name="Preview mixes")
    db_session.add(root)
    db_session.commit()
    # 15 tracks across 15 artists clears GENRE_MIX_MIN_TRACKS, and the
    # first 4 in collage order are distinct artists.
    for i in range(15):
        item = MediaItem(
            library_root_id=root.id,
            kind=MediaKind.track,
            relative_path=f"rock-{i}.mp3",
            title=f"Rock track {i}",
            genre="Rock",
            artist=f"Artist {i}",
        )
        db_session.add(item)
    db_session.commit()

    mixes = refresh_all_genre_mixes(db_session, user)
    assert len(mixes) == 1
    rock = mixes[0]
    assert rock.auto_kind == "mix"
    assert rock.preview_media_item_ids_json is not None
    preview_ids = rock.preview_media_item_ids_json
    assert isinstance(preview_ids, list)
    assert 1 <= len(preview_ids) <= 4
    # Every chosen preview track must be one of the mix's own items.
    item_ids = {item.media_item_id for item in rock.items}
    assert set(preview_ids) <= item_ids
    # All 15 artists are distinct, so the snapshot reaches the 4-id limit.
    assert len(preview_ids) == 4
    # And the snapshot is artist-distinct.
    artists_by_id = {
        item.media_item_id: item.media_item.artist for item in rock.items
    }
    assert len({artists_by_id[pid] for pid in preview_ids}) == len(preview_ids)


def test_add_specified_mix_persists_preview_ids(db_session):
    """Specified (user-requested) mixes persist their collage snapshot too."""
    user = db_session.query(User).one()
    root = LibraryRoot(path="/specified-previews", display_name="Specified previews")
    db_session.add(root)
    db_session.commit()
    for i in range(20):
        item = MediaItem(
            library_root_id=root.id,
            kind=MediaKind.track,
            relative_path=f"metal-{i}.mp3",
            title=f"Metal track {i}",
            genre="Metal",
        )
        db_session.add(item)
    db_session.commit()

    mix = add_specified_mix_playlist(db_session, user, "genre", "Metal")
    assert mix.auto_kind == "mix"
    assert mix.preview_media_item_ids_json is not None
    preview_ids = mix.preview_media_item_ids_json
    assert isinstance(preview_ids, list)
    assert 1 <= len(preview_ids) <= 4
    item_ids = {item.media_item_id for item in mix.items}
    assert set(preview_ids) <= item_ids

    # Refreshing the same mix rebuilds its items and the snapshot stays
    # consistent with (one of) the rebuilt item sets.
    refreshed = add_specified_mix_playlist(db_session, user, "genre", "Metal")
    assert refreshed.id == mix.id
    assert refreshed.preview_media_item_ids_json is not None
    refreshed_ids = refreshed.preview_media_item_ids_json
    refreshed_item_ids = {item.media_item_id for item in refreshed.items}
    assert set(refreshed_ids) <= refreshed_item_ids


def test_add_specified_mix_preview_skips_repeated_artists(db_session):
    """Same selection semantics as everywhere else: one track per artist."""
    user = db_session.query(User).one()
    root = LibraryRoot(path="/dedupe", display_name="Dedupe")
    db_session.add(root)
    db_session.commit()
    # 20 tracks but only 2 artists - the collage can hold at most 2 ids.
    for i in range(20):
        item = MediaItem(
            library_root_id=root.id,
            kind=MediaKind.track,
            relative_path=f"punk-{i}.mp3",
            title=f"Punk track {i}",
            genre="Punk",
            artist="Solo" if i % 2 == 0 else "Duo",
        )
        db_session.add(item)
    db_session.commit()

    mix = add_specified_mix_playlist(db_session, user, "genre", "Punk")
    assert mix.preview_media_item_ids_json is not None
    preview_ids = mix.preview_media_item_ids_json
    assert len(preview_ids) == 2
    artists = {
        item.media_item.artist
        for item in mix.items
        if item.media_item_id in preview_ids
    }
    assert len(artists) == 2


def test_mix_preview_backfill_on_startup(tmp_path):
    """Mix playlists whose row predates the preview column (snapshot NULL)
    get backfilled by the startup migration; a mix with no items is left
    alone rather than given an empty snapshot. Mirrors the pattern of
    test_db.py's migration tests: a real database file with the *old*
    schema, then the ensure function against that engine."""
    old_engine = create_engine(f"sqlite:///{tmp_path / 'backfill.db'}")
    Base.metadata.create_all(bind=old_engine)
    # Drop the column the migration is meant to add, recreating the
    # pre-migration playlists table.
    with old_engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE playlists")
        conn.exec_driver_sql(
            "CREATE TABLE playlists ("
            "id INTEGER PRIMARY KEY, user_id INTEGER REFERENCES users (id), "
            "name VARCHAR NOT NULL, is_auto_generated BOOLEAN, "
            "auto_kind VARCHAR, mix_genre VARCHAR, created_at DATETIME)"
        )

    factory = sessionmaker(bind=old_engine, autoflush=False, autocommit=False)
    db = factory()
    try:
        user = User(username="backfill-user", password_hash="x", is_admin=True)
        db.add(user)
        db.commit()
        root = LibraryRoot(path="/backfill", display_name="Backfill")
        db.add(root)
        db.commit()
        db.refresh(root)
        artist_names = ["Alpha", "Beta", "Beta", "Gamma", "Delta"]
        for i, artist in enumerate(artist_names):
            db.add(
                MediaItem(
                    library_root_id=root.id,
                    kind=MediaKind.track,
                    relative_path=f"blues-{i}.mp3",
                    title=f"Blues track {i}",
                    genre="Blues",
                    artist=artist,
                )
            )
        db.commit()
        # Insert the pre-migration mix rows with raw SQL: the old table has
        # no preview_media_item_ids_json column, so the ORM cannot even
        # SELECT from it until the migration has run.
        with old_engine.begin() as conn:
            conn.exec_driver_sql(
                "INSERT INTO playlists (user_id, name, is_auto_generated, "
                "auto_kind, mix_genre, created_at) VALUES "
                "(1, 'Blues Mix', 1, 'mix', 'Blues', '2026-01-01 00:00:00')"
            )
            with_items_id = conn.exec_driver_sql("SELECT last_insert_rowid()").scalar()
            conn.exec_driver_sql(
                "INSERT INTO playlists (user_id, name, is_auto_generated, "
                "auto_kind, mix_genre, created_at) VALUES "
                "(1, 'Empty Mix', 1, 'mix', 'tag:Nothing', '2026-01-01 00:00:00')"
            )
            empty_mix_id = conn.exec_driver_sql("SELECT last_insert_rowid()").scalar()
        for position, item in enumerate(
            db.query(MediaItem).filter(MediaItem.genre == "Blues").all()
        ):
            db.add(
                PlaylistItem(
                    playlist_id=with_items_id,
                    media_item_id=item.id,
                    position=position,
                )
            )
        db.commit()

        with patch("app.migrations.engine", old_engine), patch(
            "app.migrations.SessionLocal", factory
        ):
            # Startup adds playlists' plain columns first; the preview
            # backfill reads rows through the ORM, which selects them all,
            # so it is a data backfill that runs after every schema step.
            ensure_playlist_columns()
            ensure_playlist_preview_column()
            ensure_playlist_preview_column()  # must be idempotent
            backfill_mix_preview_ids()

        # Only after the migration may the ORM read the playlists rows.
        with_items = db.get(Playlist, with_items_id)
        empty_mix = db.get(Playlist, empty_mix_id)
        assert with_items is not None and empty_mix is not None
        db.refresh(with_items)
        db.refresh(empty_mix)
        assert with_items.preview_media_item_ids_json is not None
        preview_ids = with_items.preview_media_item_ids_json
        assert len(preview_ids) == 4  # Alpha, Beta, Gamma, Delta
        artists = {
            artist
            for (artist,) in db.query(MediaItem.artist)
            .filter(MediaItem.id.in_(preview_ids))
            .all()
        }
        assert artists == {"Alpha", "Beta", "Gamma", "Delta"}
        # A mix with no items has nothing to collage - stays NULL.
        stored = db.execute(
            text("SELECT preview_media_item_ids_json FROM playlists WHERE id = :id"), {"id": empty_mix.id}
        )
        assert stored.scalar_one() is None
    finally:
        db.close()

    with old_engine.connect() as conn:
        columns = {
            row[1]
            for row in conn.exec_driver_sql("PRAGMA table_info(playlists)")
        }
    assert "preview_media_item_ids_json" in columns
    old_engine.dispose()
