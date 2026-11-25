"""Schema migrations, in the ensure_* style rather than Alembic.

Each step brings an older database up to what the models expect - mostly
adding columns that ``create_all`` won't add to a table that already
exists. ``system_settings['schema_version']`` records how many steps a
database has had, so each step runs once. A database from before
versioning has no row and counts as version 0: every step then runs, which
is safe because each one checks before it changes anything.

Add a new step by appending it to ``MIGRATIONS``; never reorder or remove
one, or databases already past it would skip whatever moves into its slot.
"""

import logging
import os
import sqlite3
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import undefer

from app.albums import assign_album, prune_orphan_albums, rebuild_albums
from app.artist_credits import (
    credits_from_columns,
    load_split_rules,
    prune_orphan_artists,
    rebuild_all_credits,
    sync_track_credits,
)
from app.collage import collage_media_item_ids
from app.db import SessionLocal, engine
from app.default_radio_stations import DEFAULT_RADIO_STATIONS
from app.models import (
    Base,
    CollectionPreference,
    Favorite,
    MediaItem,
    MediaKind,
    Playlist,
    PlaylistItem,
    User,
)
from app.preferences import get_or_create_favorites_playlist

logger = logging.getLogger(__name__)

SCHEMA_VERSION_KEY = "schema_version"
# Read straight from the environment rather than app.config: it is only ever
# set by hand, for one start, after a downgrade the operator has decided on.
ALLOW_NEWER_SCHEMA_ENV = "ATRIOLYRIC_ALLOW_NEWER_SCHEMA"
# Copies of the database made before an upgrade, in <data dir>/backups.
BACKUPS_KEPT = 3


class NewerSchemaError(RuntimeError):
    """The database was upgraded by a newer release than this one."""

def ensure_user_columns() -> None:
    """The account-wide accent color. Like the
    other ensure_* helpers, `create_all` won't add this column to an
    existing production table, so it's altered explicitly here."""
    with engine.connect() as conn:
        existing = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(users)")}
        if existing and "accent_color" not in existing:
            conn.exec_driver_sql("ALTER TABLE users ADD COLUMN accent_color TEXT")
        conn.commit()


def ensure_user_audio_setting_columns() -> None:
    """Account-wide volume levelling and equaliser settings."""
    with engine.connect() as conn:
        existing = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(users)")}
        for column, ddl in (
            ("volume_leveling", "BOOLEAN NOT NULL DEFAULT 0"),
            ("eq_preset", "TEXT NOT NULL DEFAULT 'flat'"),
            ("eq_gains_json", "TEXT"),
        ):
            if existing and column not in existing:
                conn.exec_driver_sql(f"ALTER TABLE users ADD COLUMN {column} {ddl}")
        conn.commit()


def ensure_media_item_peak_column() -> None:
    """Track peaks join the ReplayGain columns. Tracks measured before the
    column existed are queued for another pass so the background backfill
    can record their peak too (tagged files are re-read, not re-measured)."""
    with engine.connect() as conn:
        existing = {
            row[1] for row in conn.exec_driver_sql("PRAGMA table_info(media_items)")
        }
        if existing and "replaygain_track_peak" not in existing:
            conn.exec_driver_sql(
                "ALTER TABLE media_items ADD COLUMN replaygain_track_peak FLOAT"
            )
            if "loudness_scanned_at" in existing:
                conn.exec_driver_sql(
                    "UPDATE media_items SET loudness_scanned_at = NULL"
                )
        conn.commit()


def ensure_radio_station_columns() -> None:
    """Add columns introduced after the table already existed in production.
    No migration framework here - `create_all` only creates missing tables,
    never alters existing ones, so a plain model change would otherwise 500
    on any database that already had a radio_stations table."""
    with engine.connect() as conn:
        existing = {
            row[1] for row in conn.exec_driver_sql("PRAGMA table_info(radio_stations)")
        }
        if existing and "description" not in existing:
            conn.exec_driver_sql(
                "ALTER TABLE radio_stations ADD COLUMN description TEXT"
            )
        if existing and "provider" not in existing:
            conn.exec_driver_sql("ALTER TABLE radio_stations ADD COLUMN provider TEXT")
        conn.commit()


def ensure_playback_columns() -> None:
    """Same reasoning as ensure_radio_station_columns - shuffle support
    added columns to two tables that already existed in production."""
    with engine.connect() as conn:
        state_cols = {
            row[1] for row in conn.exec_driver_sql("PRAGMA table_info(playback_state)")
        }
        if state_cols and "is_shuffled" not in state_cols:
            conn.exec_driver_sql(
                "ALTER TABLE playback_state ADD COLUMN is_shuffled BOOLEAN NOT NULL DEFAULT 0"
            )
        queue_cols = {
            row[1]
            for row in conn.exec_driver_sql("PRAGMA table_info(playback_queue_items)")
        }
        if queue_cols and "original_position" not in queue_cols:
            conn.exec_driver_sql(
                "ALTER TABLE playback_queue_items ADD COLUMN original_position INTEGER NOT NULL DEFAULT 0"
            )
            # Backfill so existing queues (this column didn't exist to sort
            # by before) at least restore to their current play order
            # rather than all colliding on the same default of 0.
            conn.exec_driver_sql(
                "UPDATE playback_queue_items SET original_position = position"
            )
        state_column_ddl = {
            "shuffle_seed": "INTEGER",
            "repeat_mode": "TEXT NOT NULL DEFAULT 'off'",
            "queue_context_json": "TEXT",
            "queue_context_count": "INTEGER NOT NULL DEFAULT 0",
            "tail_cursor": "INTEGER NOT NULL DEFAULT 0",
        }
        for column, ddl in state_column_ddl.items():
            if state_cols and column not in state_cols:
                conn.exec_driver_sql(
                    f"ALTER TABLE playback_state ADD COLUMN {column} {ddl}"
                )
        if queue_cols and "source" not in queue_cols:
            # Existing rows predate windowing entirely - treat them as
            # "explicit" so nothing already queued gets silently trimmed by
            # the new generated-row cleanup the first time it runs.
            conn.exec_driver_sql(
                "ALTER TABLE playback_queue_items ADD COLUMN source TEXT NOT NULL DEFAULT 'explicit'"
            )
        device_cols = {
            row[1] for row in conn.exec_driver_sql("PRAGMA table_info(devices)")
        }
        if device_cols and "client_token" not in device_cols:
            conn.exec_driver_sql("ALTER TABLE devices ADD COLUMN client_token TEXT")
        if device_cols and "is_connected" not in device_cols:
            conn.exec_driver_sql(
                "ALTER TABLE devices ADD COLUMN is_connected BOOLEAN NOT NULL DEFAULT 0"
            )
        conn.commit()


def ensure_playlist_columns() -> None:
    """Same reasoning as ensure_radio_station_columns - the generated-playlist
    feature added a column to a table that already existed in production."""
    with engine.connect() as conn:
        existing = {
            row[1] for row in conn.exec_driver_sql("PRAGMA table_info(playlists)")
        }
        if existing and "auto_kind" not in existing:
            conn.exec_driver_sql("ALTER TABLE playlists ADD COLUMN auto_kind TEXT")
            # Backfill the one auto-kind that already existed before this
            # column did, so it's still recognized as the same managed slot.
            conn.exec_driver_sql(
                "UPDATE playlists SET auto_kind = 'discovery' "
                "WHERE is_auto_generated = 1 AND name = 'Discovery'"
            )
        if existing and "mix_genre" not in existing:
            conn.exec_driver_sql("ALTER TABLE playlists ADD COLUMN mix_genre TEXT")
        if existing and "description" not in existing:
            conn.exec_driver_sql("ALTER TABLE playlists ADD COLUMN description TEXT")
        conn.commit()


def ensure_play_source_history_columns() -> None:
    """Add cached media references used by recently played cards.

    A mix refresh may remove a generated playlist after it has appeared in
    history.  ``create_all`` cannot add these fields to an existing database,
    so production databases need the same small, idempotent migration as the
    other long-lived tables above.
    """
    with engine.connect() as conn:
        existing = {
            row[1]
            for row in conn.exec_driver_sql("PRAGMA table_info(play_source_history)")
        }
        if existing and "art_media_item_id" not in existing:
            conn.exec_driver_sql(
                "ALTER TABLE play_source_history ADD COLUMN art_media_item_id INTEGER"
            )
        if existing and "preview_media_item_ids_json" not in existing:
            conn.exec_driver_sql(
                "ALTER TABLE play_source_history ADD COLUMN preview_media_item_ids_json TEXT"
            )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_play_source_history_user_updated_at "
            "ON play_source_history (user_id, updated_at DESC)"
        )
        conn.commit()


def ensure_playlist_preview_column() -> None:
    """Add the materialized Home collage snapshot to existing playlists
    tables. Filling it for mixes that predate the column is a data backfill
    (backfill_mix_preview_ids): an ORM query here would select columns that
    later steps add.

    ``create_all`` only creates missing tables, never alters existing ones,
    so production databases need the same small, idempotent migration as
    the other long-lived tables above.
    """
    with engine.connect() as conn:
        existing = {
            row[1] for row in conn.exec_driver_sql("PRAGMA table_info(playlists)")
        }
        if existing and "preview_media_item_ids_json" not in existing:
            conn.exec_driver_sql(
                "ALTER TABLE playlists ADD COLUMN preview_media_item_ids_json TEXT"
            )
        conn.commit()


def ensure_media_item_release_date_column() -> None:
    """Same reasoning as ensure_radio_station_columns - "sort albums/artists
    by date published" added a column to media_items that already existed
    in production. Existing rows stay NULL until their file's mtime
    changes and a rescan re-reads tags - there's no bulk backfill here.

    An older database may also have a `year` column holding just the
    release year; `release_date` holds a full "YYYY-MM-DD", since most
    tagged files actually carry a full date and artists often release
    several singles in one year."""
    with engine.connect() as conn:
        existing = {
            row[1] for row in conn.exec_driver_sql("PRAGMA table_info(media_items)")
        }
        # Never drop the transitional `year` column at startup. Older
        # databases may still contain user data in it; release_date can be
        # added alongside it and the old column can be removed safely later
        # through a versioned, backed-up migration.
        if existing and "release_date" not in existing:
            conn.exec_driver_sql("ALTER TABLE media_items ADD COLUMN release_date TEXT")
        conn.commit()


def ensure_media_item_loudness_columns() -> None:
    """ReplayGain added three nullable columns to media_items. Existing rows
    stay NULL until the loudness backfill (app.loudness) reaches them."""
    with engine.connect() as conn:
        existing = {
            row[1] for row in conn.exec_driver_sql("PRAGMA table_info(media_items)")
        }
        for column, sql_type in (
            ("replaygain_track_gain", "FLOAT"),
            ("replaygain_album_gain", "FLOAT"),
            ("loudness_scanned_at", "DATETIME"),
        ):
            if existing and column not in existing:
                conn.exec_driver_sql(
                    f"ALTER TABLE media_items ADD COLUMN {column} {sql_type}"
                )
        conn.commit()


def ensure_media_item_file_size_column() -> None:
    """Rescans compare file size as well as mtime to catch in-place tag
    edits. Existing rows stay NULL and are filled in by the next scan."""
    with engine.connect() as conn:
        existing = {
            row[1] for row in conn.exec_driver_sql("PRAGMA table_info(media_items)")
        }
        if existing and "file_size" not in existing:
            conn.exec_driver_sql("ALTER TABLE media_items ADD COLUMN file_size INTEGER")
        conn.commit()


def ensure_want_list_columns() -> None:
    """Matching want-list entries to the library added media_item_id."""
    with engine.connect() as conn:
        existing = {
            row[1] for row in conn.exec_driver_sql("PRAGMA table_info(want_list_items)")
        }
        if existing and "media_item_id" not in existing:
            conn.exec_driver_sql(
                "ALTER TABLE want_list_items ADD COLUMN media_item_id INTEGER "
                "REFERENCES media_items (id)"
            )
        conn.commit()


def ensure_musicbrainz_metadata_columns() -> None:
    """Add MusicBrainz cache and lookup-state fields to older databases.

    The normalized genre/tag tables are created by ``create_all``. These two
    fields live on the long-standing media_items table and therefore need the
    same idempotent migration treatment as the other metadata columns.
    """
    with engine.connect() as conn:
        existing = {
            row[1] for row in conn.exec_driver_sql("PRAGMA table_info(media_items)")
        }
        if existing and "musicbrainz_metadata_json" not in existing:
            conn.exec_driver_sql(
                "ALTER TABLE media_items ADD COLUMN musicbrainz_metadata_json TEXT"
            )
        if existing and "musicbrainz_fetched_at" not in existing:
            conn.exec_driver_sql(
                "ALTER TABLE media_items ADD COLUMN musicbrainz_fetched_at DATETIME"
            )
        additions = {
            "musicbrainz_isrc": "TEXT",
            "musicbrainz_lookup_status": "TEXT",
            "musicbrainz_last_attempt_at": "DATETIME",
            "musicbrainz_last_error": "TEXT",
            "musicbrainz_isrc_scanned_at": "DATETIME",
        }
        for name, column_type in additions.items():
            if existing and name not in existing:
                conn.exec_driver_sql(
                    f"ALTER TABLE media_items ADD COLUMN {name} {column_type}"
                )
        conn.commit()


def ensure_album_artist_columns() -> None:
    """Add embedded album-artist storage and its one-time scan marker."""
    with engine.connect() as conn:
        existing = {
            row[1] for row in conn.exec_driver_sql("PRAGMA table_info(media_items)")
        }
        if existing and "album_artist" not in existing:
            conn.exec_driver_sql("ALTER TABLE media_items ADD COLUMN album_artist TEXT")
        if existing and "album_artist_scanned_at" not in existing:
            conn.exec_driver_sql(
                "ALTER TABLE media_items ADD COLUMN album_artist_scanned_at DATETIME"
            )
        conn.commit()


def ensure_media_item_video_folder_column() -> None:
    """Add and backfill the indexed folder key used by paginated video views."""
    with engine.connect() as conn:
        columns = {
            row[1] for row in conn.exec_driver_sql("PRAGMA table_info(media_items)")
        }
        if columns and "video_folder" not in columns:
            conn.exec_driver_sql("ALTER TABLE media_items ADD COLUMN video_folder TEXT")
        if columns:
            rows = conn.exec_driver_sql(
                "SELECT id, relative_path FROM media_items WHERE video_folder IS NULL"
            ).all()
            for media_id, relative_path in rows:
                folder = Path(relative_path).parent.name or "Unsorted"
                conn.exec_driver_sql(
                    "UPDATE media_items SET video_folder = ? WHERE id = ?",
                    (folder, media_id),
                )
            conn.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_media_items_video_folder "
                "ON media_items (video_folder)"
            )
        conn.commit()


def ensure_media_item_indexes() -> None:
    """`create_all` only adds indexes when it's creating the table itself -
    on a database that already has media_items (i.e. every production
    database), the ones declared on the model are silently never created.
    CREATE INDEX IF NOT EXISTS makes this safe to just run every startup,
    unlike the ALTER TABLE column migrations above which need their own
    existence check first."""
    with engine.connect() as conn:
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_media_items_root_kind_artist_album "
            "ON media_items (library_root_id, kind, artist, album)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_media_items_root_kind_album_artist "
            "ON media_items (library_root_id, kind, album_artist)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_media_items_root_kind_genre "
            "ON media_items (library_root_id, kind, genre)"
        )
        # Recently-added includes every media kind, so `kind` cannot sit
        # between the root filter and created_at. Keep the sort key adjacent
        # to the constrained root so SQLite can satisfy both the filter and
        # descending order from the index rather than sorting all its rows.
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_media_items_root_created_at "
            "ON media_items (library_root_id, created_at DESC)"
        )
        conn.commit()


def ensure_play_history_indexes() -> None:
    """Add indexes needed by the user-scoped history aggregates on existing
    databases. This is idempotent until versioned migrations are introduced.
    """
    with engine.connect() as conn:
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_play_history_user_played_at "
            "ON play_history (user_id, played_at)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_play_history_user_media_played_at "
            "ON play_history (user_id, media_item_id, played_at)"
        )
        conn.commit()


def drop_ratings_table() -> None:
    """Drop the unused 1-5 star ratings table without converting it.
    ``create_all`` never drops a table, so an existing
    database needs this to lose it. Idempotent."""
    with engine.connect() as conn:
        conn.exec_driver_sql("DROP TABLE IF EXISTS ratings")
        conn.commit()


def ensure_lastfm_metadata_columns() -> None:
    """Last.fm tags share the MusicBrainz tag table, told apart by source."""
    with engine.connect() as conn:
        items = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(media_items)")}
        if items and "lastfm_fetched_at" not in items:
            conn.exec_driver_sql("ALTER TABLE media_items ADD COLUMN lastfm_fetched_at DATETIME")
        tags = {
            row[1]
            for row in conn.exec_driver_sql("PRAGMA table_info(media_item_musicbrainz_tags)")
        }
        if tags and "source" not in tags:
            conn.exec_driver_sql(
                "ALTER TABLE media_item_musicbrainz_tags "
                "ADD COLUMN source TEXT NOT NULL DEFAULT 'musicbrainz'"
            )
        conn.commit()


def ensure_artist_credits() -> None:
    """Multi-artist support: the per-track re-read marker and the stored
    multi-value tags. Linking existing tracks happens in a data backfill
    (see DATA_BACKFILLS), once every column exists. The next scan re-reads
    each file once (artists_scanned_at is NULL) for multi-value tags."""
    with engine.connect() as conn:
        existing = {
            row[1] for row in conn.exec_driver_sql("PRAGMA table_info(media_items)")
        }
        if existing and "artists_scanned_at" not in existing:
            conn.exec_driver_sql(
                "ALTER TABLE media_items ADD COLUMN artists_scanned_at DATETIME"
            )
        if existing and "artist_tags_json" not in existing:
            conn.exec_driver_sql("ALTER TABLE media_items ADD COLUMN artist_tags_json TEXT")
        if existing and "album_owner" not in existing:
            conn.exec_driver_sql("ALTER TABLE media_items ADD COLUMN album_owner TEXT")
        conn.commit()


def ensure_albums() -> None:
    """Album rows: add media_items.album_id. Assigning albums and deriving
    their release groups happens in a data backfill (DATA_BACKFILLS)."""
    with engine.connect() as conn:
        existing = {
            row[1] for row in conn.exec_driver_sql("PRAGMA table_info(media_items)")
        }
        if existing and "album_id" not in existing:
            conn.exec_driver_sql(
                "ALTER TABLE media_items ADD COLUMN album_id INTEGER REFERENCES albums(id)"
            )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_media_items_album_id ON media_items (album_id)"
        )
        conn.commit()


def ensure_lastfm_data_columns() -> None:
    """Similar-artist match scores, kept beside the cached names."""
    with engine.connect() as conn:
        existing = {
            row[1]
            for row in conn.exec_driver_sql("PRAGMA table_info(artist_metadata_cache)")
        }
        if existing and "similar_scores_json" not in existing:
            conn.exec_driver_sql(
                "ALTER TABLE artist_metadata_cache ADD COLUMN similar_scores_json TEXT"
            )
        conn.commit()


def ensure_want_list_kinds() -> None:
    """Want-list entries can be artists, albums or songs. Existing rows are
    songs, which is the column default."""
    additions = {
        "kind": "TEXT NOT NULL DEFAULT 'track'",
        "album": "TEXT",
        "artist_mbid": "TEXT",
        "release_group_mbid": "TEXT",
        "recording_mbid": "TEXT",
        "release_type": "TEXT",
        "excluded": "BOOLEAN NOT NULL DEFAULT 0",
        "owned_count": "INTEGER",
        "total_count": "INTEGER",
    }
    with engine.connect() as conn:
        existing = {
            row[1] for row in conn.exec_driver_sql("PRAGMA table_info(want_list_items)")
        }
        for name, column_type in additions.items():
            if existing and name not in existing:
                conn.exec_driver_sql(
                    f"ALTER TABLE want_list_items ADD COLUMN {name} {column_type}"
                )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_want_list_items_artist_mbid ON want_list_items (artist_mbid)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_want_list_items_release_group_mbid "
            "ON want_list_items (release_group_mbid)"
        )
        conn.commit()


def ensure_user_dismissed_notices_column() -> None:
    """Per-account "Never show again" notices."""
    with engine.connect() as conn:
        existing = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(users)")}
        if existing and "dismissed_notices_json" not in existing:
            conn.exec_driver_sql("ALTER TABLE users ADD COLUMN dismissed_notices_json TEXT")
        conn.commit()


def ensure_library_task_stage_column() -> None:
    """The full setup scan reports which of its steps is running."""
    with engine.connect() as conn:
        existing = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(library_tasks)")}
        if existing and "stage" not in existing:
            conn.exec_driver_sql("ALTER TABLE library_tasks ADD COLUMN stage TEXT")
        conn.commit()


def ensure_library_task_missing_held_columns() -> None:
    """A scan that held back removing missing tracks says how many, and why."""
    with engine.connect() as conn:
        existing = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(library_tasks)")}
        if existing and "missing_held" not in existing:
            conn.exec_driver_sql(
                "ALTER TABLE library_tasks ADD COLUMN missing_held INTEGER NOT NULL DEFAULT 0"
            )
        if existing and "note" not in existing:
            conn.exec_driver_sql("ALTER TABLE library_tasks ADD COLUMN note TEXT")
        conn.commit()


def ensure_session_expiry_index() -> None:
    """Expired sessions are purged by `expires_at`; an existing sessions
    table never gets the model's index from `create_all`."""
    with engine.connect() as conn:
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_sessions_expires_at ON sessions (expires_at)"
        )
        conn.commit()


def ensure_playback_state_revision_column() -> None:
    """Playback snapshots carry a revision so devices can drop stale ones."""
    with engine.connect() as conn:
        existing = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(playback_state)")}
        if existing and "revision" not in existing:
            conn.exec_driver_sql(
                "ALTER TABLE playback_state ADD COLUMN revision INTEGER NOT NULL DEFAULT 0"
            )
        conn.commit()


def ensure_user_lastfm_recommendations_column() -> None:
    """Per-user opt-out of sending plays to Last.fm for recommendations;
    existing users keep the behaviour they had (on)."""
    with engine.connect() as conn:
        existing = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(users)")}
        if existing and "lastfm_recommendations" not in existing:
            conn.exec_driver_sql(
                "ALTER TABLE users ADD COLUMN lastfm_recommendations BOOLEAN NOT NULL DEFAULT 1"
            )
        conn.commit()


def purge_plaintext_sessions() -> None:
    """Sessions are looked up by the SHA-256 digest of the cookie only; rows
    from before digests were stored (the raw urlsafe token) can no longer
    authenticate, and would leave a bearer secret in the database. Delete
    every row whose token is not a 64-character lowercase hex digest - those
    users simply sign in again."""
    with engine.connect() as conn:
        conn.exec_driver_sql(
            "DELETE FROM sessions WHERE length(token) != 64 OR token GLOB '*[^0-9a-f]*'"
        )
        conn.commit()


def ensure_one_favorites_playlist_per_user() -> None:
    """At most one Favourites playlist per user, enforced by a partial
    unique index. Any duplicates are merged into the user's oldest one
    first: their songs move across (after its own, each song once) and the
    extra playlists and their custom art rows go."""
    with engine.connect() as conn:
        users = conn.exec_driver_sql(
            "SELECT user_id, MIN(id) FROM playlists WHERE auto_kind = 'favorites' "
            "GROUP BY user_id HAVING COUNT(*) > 1"
        ).all()
        for user_id, keeper in users:
            extras = [
                row[0]
                for row in conn.exec_driver_sql(
                    "SELECT id FROM playlists WHERE auto_kind = 'favorites' "
                    "AND user_id = ? AND id != ? ORDER BY id",
                    (user_id, keeper),
                )
            ]
            for extra in extras:
                offset = conn.exec_driver_sql(
                    "SELECT COALESCE(MAX(position), -1) + 1 FROM playlist_items "
                    "WHERE playlist_id = ?",
                    (keeper,),
                ).scalar()
                conn.exec_driver_sql(
                    "UPDATE playlist_items SET playlist_id = ?, position = position + ? "
                    "WHERE playlist_id = ? AND media_item_id NOT IN "
                    "(SELECT media_item_id FROM playlist_items WHERE playlist_id = ?)",
                    (keeper, offset, extra, keeper),
                )
                conn.exec_driver_sql("DELETE FROM playlist_items WHERE playlist_id = ?", (extra,))
                conn.exec_driver_sql(
                    "DELETE FROM custom_art WHERE entity_type = 'playlist' AND entity_key = ?",
                    (str(extra),),
                )
                conn.exec_driver_sql("DELETE FROM playlists WHERE id = ?", (extra,))
            # A song listed twice within one playlist (moved from two extras).
            conn.exec_driver_sql(
                "DELETE FROM playlist_items WHERE playlist_id = ? AND id NOT IN "
                "(SELECT MIN(id) FROM playlist_items WHERE playlist_id = ? GROUP BY media_item_id)",
                (keeper, keeper),
            )
        conn.exec_driver_sql(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_playlists_one_favorites_per_user "
            "ON playlists (user_id) WHERE auto_kind = 'favorites'"
        )
        conn.commit()


def ensure_media_dependent_indexes() -> None:
    """Indexes for reading a playlist in order and for deleting a track's
    dependent rows: every table that references a
    media item gets one on media_item_id, since composite indexes that lead
    with user_id cannot serve `WHERE media_item_id IN (...)`."""
    with engine.connect() as conn:
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_playlist_items_playlist_position "
            "ON playlist_items (playlist_id, position)"
        )
        for table in (
            "playlist_items",
            "play_history",
            "favorites",
            "track_dislikes",
            "playback_queue_items",
            "want_list_items",
        ):
            conn.exec_driver_sql(
                f"CREATE INDEX IF NOT EXISTS ix_{table}_media_item_id ON {table} (media_item_id)"
            )
        conn.commit()


def ensure_want_match_indexes() -> None:
    """Indexes for want-list matching: a track by
    recording MBID, and by lower(artist) for rows matched on the artist
    string. Each also covers root and kind - see the model's comment."""
    with engine.connect() as conn:
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_media_items_recording_mbid_root_kind "
            "ON media_items (musicbrainz_recording_mbid, library_root_id, kind)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_media_items_lower_artist_root_kind "
            "ON media_items (lower(artist), library_root_id, kind)"
        )
        conn.commit()


def ensure_media_item_art_checked_column() -> None:
    """The startup art backfill remembers which file version it found no art
    in. Existing rows stay NULL: probed once more."""
    with engine.connect() as conn:
        existing = {
            row[1] for row in conn.exec_driver_sql("PRAGMA table_info(media_items)")
        }
        if existing and "art_checked_mtime" not in existing:
            conn.exec_driver_sql("ALTER TABLE media_items ADD COLUMN art_checked_mtime FLOAT")
        conn.commit()


def ensure_play_history_started_at() -> None:
    """Plays record when the listening began, and one play is one row: a
    client retrying a report it could not confirm
    sends the same started_at again. Existing rows keep NULL, which the
    unique index never treats as equal."""
    with engine.connect() as conn:
        existing = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(play_history)")}
        if existing and "started_at" not in existing:
            conn.exec_driver_sql("ALTER TABLE play_history ADD COLUMN started_at DATETIME")
        conn.exec_driver_sql(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_play_history_user_media_started_at "
            "ON play_history (user_id, media_item_id, started_at)"
        )
        conn.commit()


def drop_listenbrainz_columns() -> None:
    """Nothing reads the two media_items columns of the ListenBrainz
    metadata fetch, so they are dropped. ALTER TABLE ... DROP
    COLUMN needs SQLite 3.35; the image's Debian trixie ships 3.46 (bookworm
    had 3.40).
    Neither column is indexed or constrained, so the drop is allowed.
    Idempotent: a database without them (fresh, or already dropped) is left
    alone."""
    with engine.connect() as conn:
        existing = {
            row[1] for row in conn.exec_driver_sql("PRAGMA table_info(media_items)")
        }
        for name in ("listenbrainz_metadata_json", "listenbrainz_fetched_at"):
            if name in existing:
                conn.exec_driver_sql(f"ALTER TABLE media_items DROP COLUMN {name}")
        conn.commit()


def ensure_listen_events_indexes() -> None:
    """How each listen ended (app.listening). create_all makes the table, as
    for the scrobble queue; this makes sure its indexes are there: one row
    per (user, item, start), the user's listens by time, and media_item_id
    for deleting a track's dependent rows (see ensure_media_dependent_indexes).
    Guarded on the table so it is a no-op anywhere it does not exist."""
    with engine.connect() as conn:
        existing = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(listen_events)")}
        if existing:
            conn.exec_driver_sql(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_listen_events_user_media_started_at "
                "ON listen_events (user_id, media_item_id, started_at)"
            )
            conn.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS idx_listen_events_user_started_at "
                "ON listen_events (user_id, started_at)"
            )
            conn.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_listen_events_media_item_id "
                "ON listen_events (media_item_id)"
            )
        conn.commit()


def ensure_audio_features_indexes() -> None:
    """Audio analysis results (app.audio_analysis). create_all makes the
    table; this makes sure the analysed_at index the similarity index's
    incremental refresh reads by is there. Guarded on the table, so a
    no-op anywhere it does not exist."""
    with engine.connect() as conn:
        existing = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(audio_features)")}
        if existing:
            conn.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_audio_features_analysed_at "
                "ON audio_features (analysed_at)"
            )
        conn.commit()


def ensure_media_item_audio_quality_columns() -> None:
    """Tracks store their codec, bitrate, sample rate and bit depth (the
    format label and Library info). Existing rows stay NULL: the next normal
    scan reads each file's stream header once and fills them, without
    treating the file as changed. create_all makes the activity_events
    table, with its index."""
    with engine.connect() as conn:
        existing = {
            row[1] for row in conn.exec_driver_sql("PRAGMA table_info(media_items)")
        }
        for column, ddl in (
            ("codec", "VARCHAR"),
            ("bitrate_kbps", "INTEGER"),
            ("sample_rate_hz", "INTEGER"),
            ("bit_depth", "INTEGER"),
        ):
            if existing and column not in existing:
                conn.exec_driver_sql(f"ALTER TABLE media_items ADD COLUMN {column} {ddl}")
        conn.commit()


def ensure_playback_state_queue_origin_column() -> None:
    """What the queue was started from. A new column always needs a step of
    its own, at the end: added to an earlier step (ensure_playback_columns)
    it would never reach a database that already ran that step."""
    with engine.connect() as conn:
        existing = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(playback_state)")}
        if existing and "queue_origin_json" not in existing:
            conn.exec_driver_sql("ALTER TABLE playback_state ADD COLUMN queue_origin_json TEXT")
        conn.commit()


# Columns renamed from their Last.fm names.
_LASTFM_RENAMES = (
    ("users", "lastfm_recommendations", "online_recommendations"),
    ("media_items", "lastfm_fetched_at", "online_tags_fetched_at"),
)


def rename_lastfm_columns() -> None:
    """users.lastfm_recommendations -> online_recommendations (the privacy
    switch covers ListenBrainz) and media_items.lastfm_fetched_at ->
    online_tags_fetched_at. RENAME COLUMN needs SQLite 3.25 and DROP COLUMN
    3.35 (the image ships 3.46). On a fresh database create_all made the new names and the old steps added the old
    ones beside them: the old one is dropped (neither is indexed). On an
    upgraded one the old column is renamed, keeping its values."""
    with engine.connect() as conn:
        for table, old, new in _LASTFM_RENAMES:
            existing = {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})")}
            if old not in existing:
                continue
            if new in existing:
                conn.exec_driver_sql(f"ALTER TABLE {table} DROP COLUMN {old}")
            else:
                conn.exec_driver_sql(f"ALTER TABLE {table} RENAME COLUMN {old} TO {new}")
        conn.commit()


def _table_exists(conn, table: str) -> bool:
    return bool(list(conn.exec_driver_sql(f"PRAGMA table_info({table})")))


def purge_lastfm_and_deezer_data() -> None:
    """Last.fm is not used and Deezer answers may not be kept (its terms allow no
    local copy, metadata included): remove what either left behind. Last.fm
    tag and genre rows are removed by the data backfill
    `restore_musicbrainz_tags_after_lastfm`, which also puts back the
    MusicBrainz tags Last.fm had taken over; Deezer-fetched art by
    `drop_deezer_catalog_art`."""
    statements = (
        # Who had Last.fm linked, before the links go: the web app tells
        # them once that scrobbling moved to ListenBrainz (notice
        # "lastfm_removed"; this marker rides in the notices list).
        (
            "user_integrations",
            "UPDATE users SET dismissed_notices_json = json_insert("
            "COALESCE(dismissed_notices_json, '[]'), '$[#]', 'lastfm_was_linked') "
            "WHERE id IN (SELECT user_id FROM user_integrations WHERE provider = 'lastfm') "
            "AND COALESCE(dismissed_notices_json, '[]') NOT LIKE '%lastfm_was_linked%'",
        ),
        ("external_cache", "DELETE FROM external_cache WHERE provider IN ('lastfm', 'deezer')"),
        # Last.fm's similar artists, keyed by name: refilled from ListenBrainz.
        ("artist_metadata_cache", "DELETE FROM artist_metadata_cache"),
        ("user_integrations", "DELETE FROM user_integrations WHERE provider = 'lastfm'"),
        # Queued Last.fm scrobbles: they cannot be sent anywhere now.
        ("scrobble_queue", "DELETE FROM scrobble_queue"),
        ("system_settings", "DELETE FROM system_settings WHERE key = 'lastfm_server_api_key'"),
        ("artists", "UPDATE artists SET info_json = NULL WHERE info_json IS NOT NULL"),
        ("albums", "UPDATE albums SET info_json = NULL WHERE info_json IS NOT NULL"),
        # Explore stored Deezer's previews, artwork and links per user.
        ("discovery_state", "UPDATE discovery_state SET explore_results_json = '[]'"),
        # The renamed lastfm_fetched_at marks said "Last.fm was asked": cleared,
        # so the ListenBrainz tags task treats every track as not yet asked.
        ("media_items", "UPDATE media_items SET online_tags_fetched_at = NULL"),
        # Users' "never show again" marks for the old Last.fm key notice.
        (
            "users",
            "UPDATE users SET dismissed_notices_json = ("
            "SELECT json_group_array(value) FROM json_each(users.dismissed_notices_json) "
            "WHERE value != 'admin_lastfm_key') "
            "WHERE dismissed_notices_json LIKE '%admin_lastfm_key%'",
        ),
    )
    with engine.connect() as conn:
        for table, sql in statements:
            if _table_exists(conn, table):
                conn.exec_driver_sql(sql)
        conn.commit()



def ensure_source_and_mbid_columns() -> None:
    """custom_art.source / credit_json (where an image came from, and the
    credit a Commons licence asks for) and scrobble_queue.recording_mbid /
    artist_mbids (a retried listen keeps its MusicBrainz IDs)."""
    columns = (
        ("custom_art", "source", "VARCHAR"),
        ("custom_art", "credit_json", "TEXT"),
        ("scrobble_queue", "recording_mbid", "VARCHAR"),
        ("scrobble_queue", "artist_mbids", "VARCHAR"),
    )
    with engine.connect() as conn:
        for table, name, kind in columns:
            existing = {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})")}
            if existing and name not in existing:
                conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")
        conn.commit()

MIGRATIONS: tuple[Callable[[], None], ...] = (
    drop_ratings_table,
    ensure_user_columns,
    ensure_radio_station_columns,
    ensure_playback_columns,
    ensure_playlist_columns,
    ensure_playlist_preview_column,
    ensure_play_source_history_columns,
    ensure_media_item_release_date_column,
    ensure_musicbrainz_metadata_columns,
    ensure_album_artist_columns,
    ensure_media_item_video_folder_column,
    ensure_media_item_loudness_columns,
    ensure_want_list_columns,
    ensure_media_item_indexes,
    ensure_play_history_indexes,
    ensure_lastfm_metadata_columns,
    ensure_media_item_file_size_column,
    ensure_user_audio_setting_columns,
    ensure_media_item_peak_column,
    ensure_artist_credits,
    ensure_albums,
    ensure_lastfm_data_columns,
    ensure_want_list_kinds,
    ensure_library_task_stage_column,
    ensure_user_dismissed_notices_column,
    ensure_library_task_missing_held_columns,
    ensure_session_expiry_index,
    ensure_playback_state_revision_column,
    purge_plaintext_sessions,
    ensure_user_lastfm_recommendations_column,
    ensure_one_favorites_playlist_per_user,
    ensure_media_dependent_indexes,
    ensure_want_match_indexes,
    ensure_media_item_art_checked_column,
    ensure_play_history_started_at,
    drop_listenbrainz_columns,
    ensure_listen_events_indexes,
    ensure_audio_features_indexes,
    ensure_media_item_audio_quality_columns,
    ensure_playback_state_queue_origin_column,
    rename_lastfm_columns,
    purge_lastfm_and_deezer_data,
    ensure_source_and_mbid_columns,
)


def backfill_artist_credits() -> None:
    """Link every existing track to its artists from the stored strings,
    so artist pages work before the next scan."""
    db = SessionLocal()
    try:
        rebuild_all_credits(db, only_missing=True)
    finally:
        db.close()


def backfill_albums() -> None:
    """Assign every track its album and derive release groups, types,
    dates and artist MBIDs from the stored MusicBrainz recordings - no
    provider request."""
    db = SessionLocal()
    try:
        rebuild_albums(db)
    finally:
        db.close()


# One-off data fills that use the ORM. They run after every schema step,
# so the models' columns all exist however old the database was (an ORM
# query inside a schema step would select columns later steps add). Each
# runs once, recorded under its own key; append new ones, never rename.
def backfill_artist_comma_display() -> None:
    """Multi-value artist tags are displayed joined with ", ", not "; ".
    Rewrite the stored display strings (the separate
    artists live in artist_tags_json, so nothing is re-split), carry the
    per-album preferences over to the new owner string, then relink."""


    db = SessionLocal()
    try:
        renames: dict[str, str] = {}
        rows = db.execute(
            select(MediaItem.id, MediaItem.artist, MediaItem.album_artist, MediaItem.artist_tags_json).where(
                MediaItem.artist_tags_json.is_not(None)
            )
        ).all()
        for media_id, artist, album_artist, tags in rows:
            # The column is decoded on read (app.json_column): None for an
            # unreadable value, which has nothing to rewrite.
            if not isinstance(tags, dict):
                continue
            values: dict = {}
            for key, column, old in (("artists", "artist", artist), ("album_artists", "album_artist", album_artist)):
                names = tags.get(key)
                if isinstance(names, list) and len(names) > 1:
                    new = ", ".join(names)
                    values[column] = new
                    if old and old != new:
                        renames[old] = new
            if values:
                db.execute(update(MediaItem).where(MediaItem.id == media_id).values(**values))
        for old, new in renames.items():
            db.execute(update(CollectionPreference).where(CollectionPreference.artist == old).values(artist=new))
        db.commit()
        rebuild_all_credits(db)
        rebuild_albums(db)
    finally:
        db.close()


def backfill_blank_tag_strings() -> None:
    """A tag edit may have stored a cleared field as '' where a scan stores
    NULL (and the file name for a title); '' shows as a blank album tile, an
    artist that is neither unknown nor anyone, an empty genre. Rewrite those
    rows as a scan would, then relink their artists and albums."""


    nullable = ("artist", "album_artist", "album", "genre")
    db = SessionLocal()
    try:
        ids = list(
            db.execute(
                select(MediaItem.id).where(
                    or_(*(getattr(MediaItem, c) == "" for c in (*nullable, "title")))
                )
            ).scalars()
        )
        if not ids:
            return
        for column in nullable:
            db.execute(
                update(MediaItem).where(getattr(MediaItem, column) == "").values({column: None})
            )
        rules = load_split_rules(db)
        # credits_from_columns reads the (deferred) multi-value artist tags.
        for item in db.scalars(
            select(MediaItem).options(undefer(MediaItem.artist_tags_json)).where(MediaItem.id.in_(ids))
        ):
            if item.title == "":
                item.title = Path(item.relative_path).stem
            if item.kind == MediaKind.track:
                sync_track_credits(db, item, credits_from_columns(item, rules))
            assign_album(db, item)
        db.commit()
        prune_orphan_artists(db)
        prune_orphan_albums(db)
    finally:
        db.close()


def backfill_favorites_playlists() -> None:
    """Every liked song is in its user's Favourites playlist: a missing
    Favourites playlist is recreated, and songs
    missing from it are appended in the order they were liked."""


    db = SessionLocal()
    try:
        user_ids = list(db.execute(select(Favorite.user_id).distinct()).scalars())
        for user_id in user_ids:
            user = db.get(User, user_id)
            if user is None:
                continue
            playlist = get_or_create_favorites_playlist(db, user)
            listed = set(
                db.execute(
                    select(PlaylistItem.media_item_id).where(PlaylistItem.playlist_id == playlist.id)
                ).scalars()
            )
            position = db.execute(
                select(func.coalesce(func.max(PlaylistItem.position), -1)).where(
                    PlaylistItem.playlist_id == playlist.id
                )
            ).scalar_one()
            liked = db.execute(
                select(Favorite.media_item_id)
                .where(Favorite.user_id == user_id)
                .order_by(Favorite.created_at, Favorite.media_item_id)
            ).scalars()
            for media_item_id in liked:
                if media_item_id not in listed:
                    position += 1
                    db.add(PlaylistItem(playlist_id=playlist.id, media_item_id=media_item_id, position=position))
        db.commit()
    finally:
        db.close()


def backfill_mix_preview_ids() -> None:
    """Fill the Home collage snapshot of mix playlists from before the column
    existed. Cheap - at most a
    handful of mix rows, four ids each - and a playlist that fails is skipped
    rather than stopping the server from starting."""
    db = SessionLocal()
    try:
        mixes = db.scalars(
            select(Playlist).where(
                Playlist.auto_kind == "mix",
                Playlist.preview_media_item_ids_json.is_(None),
            )
        ).all()
        for playlist in mixes:
            if not playlist.items:
                continue
            try:
                preview_ids = collage_media_item_ids(playlist.items)
            except Exception:  # pragma: no cover - defensive, never fatal
                logger.exception(
                    "Backfilling collage preview ids for playlist %s failed",
                    playlist.id,
                )
                continue
            playlist.preview_media_item_ids_json = preview_ids
        db.commit()
    finally:
        db.close()


# The defaults added after the old one-time batch marker
# ("default_radio_stations_seeded") shipped; an install with that marker was
# meant to receive these and no other missing default.
_RADIO_DEFAULTS_ADDED_AFTER_BATCH_MARKER = frozenset({
    "https://kexp-mp3-128.streamguys1.com/kexp128.mp3",
    "https://direct.fipradio.fr/live/fip-midfi.mp3",
    "https://stream.srg-ssr.ch/m/rsj/aacp_96",
    # RTÉ Radio 1 and 2FM, by their current (HTTPS) URLs: the conversion only
    # looks up today's defaults (their old URLs are in DEFAULT_HTTPS_MOVES).
    "https://www.rte.ie/manifests/radio1.m3u",
    "https://www.rte.ie/manifests/2fm.m3u",
    "https://25633.live.streamtheworld.com/RTE_LYRIC_FM.mp3?tdsdk=rte",
    "https://edgex.audioxi.com/NT",
    "https://edgex.audioxi.com/TD",
    "https://stream0.wfmu.org/freeform-128k",
    "https://stream0.wfmu.org/rocknsoul",
    "https://stream0.wfmu.org/drummer",
    "https://stream0.wfmu.org/sheena",
    "https://livestreaming-node-4.srg-ssr.ch/srgssr/rsc_de/mp3/128",
    "https://livestreaming-node-3.srg-ssr.ch/srgssr/rsp/mp3/128",
})


def convert_legacy_radio_seed_marker() -> None:
    """Replace the old batch radio marker with per-station markers, so the
    seed needs only the latter. On an install that has the batch marker,
    each default station from before it that is not in the table was
    deleted by a user: it gets its marker, so it is never seeded again -
    exactly what the batch marker did. Fresh installs have no batch marker
    and nothing changes."""
    with engine.connect() as conn:
        legacy = conn.exec_driver_sql(
            "SELECT 1 FROM system_settings WHERE key = 'default_radio_stations_seeded'"
        ).scalar()
        if legacy:
            present = {row[0] for row in conn.exec_driver_sql("SELECT stream_url FROM radio_stations")}
            for _name, stream_url, _description, _provider in DEFAULT_RADIO_STATIONS:
                if stream_url in present or stream_url in _RADIO_DEFAULTS_ADDED_AFTER_BATCH_MARKER:
                    continue
                conn.exec_driver_sql(
                    "INSERT OR IGNORE INTO system_settings (key, value) VALUES (?, '1')",
                    (f"default_radio_station:{stream_url}",),
                )
            conn.exec_driver_sql(
                "DELETE FROM system_settings WHERE key = 'default_radio_stations_seeded'"
            )
        conn.commit()


# The SomaFM defaults were seeded as plain-HTTP ice1.somafm.com streams until
# they moved to the HTTPS address SomaFM's own playlist files list
# (https://somafm.com/<channel>.pls, first entry). Kept as a fixed list rather
# than derived from DEFAULT_RADIO_STATIONS, so a SomaFM default added later is
# never mistaken for one that moved.
_SOMAFM_HTTP_CHANNELS = (
    "7soul", "beatblender", "bootliquor", "brfm", "cliqhop", "covers",
    "deepspaceone", "defcon", "digitalis", "doomed", "dronezone", "dz2",
    "dubstep", "fluid", "folkfwd", "groovesalad", "groovesalad2", "gsclassic",
    "illstreet", "indiepop", "live", "lush", "missioncontrol", "poptron",
    "secretagent", "seventies", "sf1033", "sonicuniverse", "spacestation",
    "suburbsofgoa", "thetrip", "thistle", "u80s", "metal", "reggae", "scanner",
    "vaporwaves", "specials", "n5md", "synphaera", "darkzone", "sfinsf",
    "tikitime", "bossa", "insound", "chillits",
)
SOMAFM_HTTPS_MOVES: dict[str, str] = {
    f"http://ice1.somafm.com/{channel}-128-mp3": f"https://ice6.somafm.com/{channel}-128-mp3"
    for channel in _SOMAFM_HTTP_CHANNELS
}


def _move_default_station_urls(moves: Iterable[tuple[str, str, str]]) -> None:
    """Move shared default stations from an old stream URL to a new one, for
    each (old URL, new URL, provider).

    A shared default row (no owner; that provider, or none for a row from
    before providers were recorded - the seed treats such a row as the
    default too) with the old URL gets the new URL in place, so its id,
    favourites and name stay. Its per-station marker moves with it. A
    default the user deleted has only the old marker: it gets the new one,
    so the seed does not bring it back. A private station that happens to
    use the old URL is left alone, and so is a shared one whose provider was
    changed, and so is the old row when a row with the new URL already
    exists (no duplicate is made). Running it twice changes nothing."""
    with engine.connect() as conn:
        for old_url, new_url, provider in moves:
            old_marker = f"default_radio_station:{old_url}"
            new_marker = f"default_radio_station:{new_url}"
            had_marker = conn.exec_driver_sql(
                "SELECT 1 FROM system_settings WHERE key = ?", (old_marker,)
            ).scalar()
            new_exists = conn.exec_driver_sql(
                "SELECT 1 FROM radio_stations WHERE stream_url = ?", (new_url,)
            ).scalar()
            moved = 0
            if not new_exists:
                moved = conn.exec_driver_sql(
                    "UPDATE radio_stations SET stream_url = ? "
                    "WHERE stream_url = ? AND is_shared = 1 AND owner_user_id IS NULL "
                    "AND (provider = ? OR provider IS NULL)",
                    (new_url, old_url, provider),
                ).rowcount
            if had_marker or moved:
                conn.exec_driver_sql(
                    "INSERT OR IGNORE INTO system_settings (key, value) VALUES (?, '1')",
                    (new_marker,),
                )
            conn.exec_driver_sql("DELETE FROM system_settings WHERE key = ?", (old_marker,))
        conn.commit()


def move_somafm_defaults_to_https() -> None:
    """Existing installs keep their SomaFM default rows, now on HTTPS (see
    `_move_default_station_urls`)."""
    _move_default_station_urls(
        (old_url, new_url, "SomaFM") for old_url, new_url in SOMAFM_HTTPS_MOVES.items()
    )


# The last defaults seeded on plain HTTP, and the HTTPS address
# each moved to: Radio Paradise's own host answers the same path over HTTPS
# (its published links still say http://), and the RTÉ stations use the
# live links RTÉ publishes (https://www.rte.ie/manifests/<station>.m3u, which
# redirect to the station's HTTPS stream). A fixed list, like the SomaFM one.
DEFAULT_HTTPS_MOVES: tuple[tuple[str, str, str], ...] = (
    (
        "http://stream.radioparadise.com/mp3-192",
        "https://stream.radioparadise.com/mp3-192",
        "Radio Paradise",
    ),
    (
        "http://25533.live.streamtheworld.com/RTE_1_INT.mp3?tdsdk=rte",
        "https://www.rte.ie/manifests/radio1.m3u",
        "RTÉ",
    ),
    ("http://icecast1.rte.ie/2fm", "https://www.rte.ie/manifests/2fm.m3u", "RTÉ"),
)


def move_http_defaults_to_https() -> None:
    """Existing installs keep their Radio Paradise Main Mix, RTÉ Radio 1 and
    RTÉ 2FM default rows, now on HTTPS (see `_move_default_station_urls`)."""
    _move_default_station_urls(DEFAULT_HTTPS_MOVES)


def retry_unmeasured_loudness() -> None:
    """Tracks marked as loudness-scanned but with no gain are measured again.
    A measurement that used an ffmpeg option the image's ffmpeg 5.1 does not
    have failed while the track was still marked as done. Clearing the mark
    lets the background
    loudness pass pick them up; a track that truly cannot be measured is
    simply marked again."""
    with engine.connect() as conn:
        conn.exec_driver_sql(
            "UPDATE media_items SET loudness_scanned_at = NULL "
            "WHERE kind = 'track' AND replaygain_track_gain IS NULL "
            "AND loudness_scanned_at IS NOT NULL"
        )
        conn.commit()


def restore_musicbrainz_tags_after_lastfm() -> None:
    """Remove every Last.fm tag and genre row. A Last.fm tag could take over
    a tag MusicBrainz also had (the row became Last.fm's), so for each track
    that had Last.fm tags the recording's own MusicBrainz tags are put back
    from its stored MusicBrainz response."""
    from sqlalchemy import delete

    from app.metadata_taxonomy import (
        replace_item_musicbrainz_tags,
        stored_musicbrainz_tags,
    )
    from app.models import (
        Genre,
        MediaItemGenre,
        MediaItemMusicBrainzTag,
        MusicBrainzTag,
    )

    with SessionLocal() as db:
        ids = sorted(
            db.execute(
                select(MediaItemMusicBrainzTag.media_item_id)
                .where(MediaItemMusicBrainzTag.source == "lastfm")
                .distinct()
            ).scalars()
        )
        db.execute(delete(MediaItemMusicBrainzTag).where(MediaItemMusicBrainzTag.source == "lastfm"))
        db.execute(delete(MediaItemGenre).where(MediaItemGenre.source == "lastfm"))
        db.commit()
        restored = ids
        for start in range(0, len(ids), 500):
            items = db.execute(
                select(MediaItem)
                .where(MediaItem.id.in_(ids[start : start + 500]))
                .options(undefer(MediaItem.musicbrainz_metadata_json))
            ).scalars()
            for item in items:
                tags = stored_musicbrainz_tags(item)
                if tags:
                    replace_item_musicbrainz_tags(db, item, tags)
            db.commit()
        # Tag and genre names no track carries any more (most were Last.fm's
        # own) are dropped, so nothing of Last.fm's is left to list.
        db.execute(delete(MusicBrainzTag).where(~MusicBrainzTag.id.in_(select(MediaItemMusicBrainzTag.musicbrainz_tag_id))))
        db.execute(delete(Genre).where(~Genre.id.in_(select(MediaItemGenre.genre_id))))
        db.commit()
        logger.info("Removed Last.fm tags; restored MusicBrainz tags on %d tracks", len(restored))


def drop_deezer_catalog_art() -> None:
    """Want-list art that may have come from Deezer, whose terms allow no
    local copy: every artist picture (Deezer was their only source) and
    every album cover (Deezer was the fallback, and which came from where
    was not recorded). They are fetched again from the Cover Art Archive
    and Wikimedia Commons; their remembered misses go too. Art people chose
    for their own library is not touched."""
    from sqlalchemy import delete

    from app.custom_art import purge_custom_art
    from app.models import CustomArt, ExternalCache

    with SessionLocal() as db:
        for entity_type in ("catalog_artist", "catalog_album"):
            keys = list(db.execute(select(CustomArt.entity_key).where(CustomArt.entity_type == entity_type)).scalars())
            purge_custom_art(db, entity_type, keys)
        db.execute(delete(ExternalCache).where(ExternalCache.provider == "catalogart"))
        db.commit()



# What the Last.fm/Deezer clean-up removes from an older copy of the
# database (a pre-upgrade backup), table by table.
_BACKUP_SCRUB = (
    ("external_cache", "DELETE FROM external_cache WHERE provider IN ('lastfm', 'deezer')"),
    ("artist_metadata_cache", "DELETE FROM artist_metadata_cache"),
    ("user_integrations", "DELETE FROM user_integrations WHERE provider = 'lastfm'"),
    ("system_settings", "DELETE FROM system_settings WHERE key = 'lastfm_server_api_key'"),
    ("discovery_state", "UPDATE discovery_state SET explore_results_json = '[]'"),
)


def _scrub_backup(path: Path) -> None:
    """Remove stored Last.fm and Deezer data from one backup file and
    compact it, so no copy of Deezer's content outlives the upgrade (its
    terms allow none). The rest of the backup is left as it was."""
    conn = sqlite3.connect(path)
    try:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        for table, sql in _BACKUP_SCRUB:
            if table in tables:
                conn.execute(sql)
        conn.commit()
        conn.execute("VACUUM")
    finally:
        conn.close()


def compact_after_lastfm_and_deezer_purge() -> None:
    """Deleted rows stay readable in SQLite's free pages until the file is
    rewritten: VACUUM the database, and scrub then compact the older
    backups (each may still hold Last.fm/Deezer rows)."""
    database = engine.url.database
    if not database or database == ":memory:":
        return
    for path in sorted((Path(database).parent / "backups").glob("atriolyric-v*.db")):
        try:
            _scrub_backup(path)
        except sqlite3.Error:
            logger.warning("Could not clean the backup %s of old Last.fm/Deezer data", path.name, exc_info=True)
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.exec_driver_sql("VACUUM")



def recheck_art_with_folder_covers() -> None:
    """Tracks were marked "no art" (art_checked_mtime) when their file had
    no picture. A cover image in the track's folder counts too,
    so the marks are cleared and the startup art pass looks at those tracks
    once more."""
    with engine.connect() as conn:
        conn.exec_driver_sql("UPDATE media_items SET art_checked_mtime = NULL WHERE art_checked_mtime IS NOT NULL")
        conn.commit()

DATA_BACKFILLS: tuple[tuple[str, Callable[[], None]], ...] = (
    ("backfill_artist_credits", backfill_artist_credits),
    ("backfill_albums", backfill_albums),
    ("backfill_artist_comma_display", backfill_artist_comma_display),
    ("backfill_blank_tag_strings", backfill_blank_tag_strings),
    ("backfill_favorites_playlists", backfill_favorites_playlists),
    ("backfill_mix_preview_ids", backfill_mix_preview_ids),
    ("convert_legacy_radio_seed_marker", convert_legacy_radio_seed_marker),
    ("retry_unmeasured_loudness", retry_unmeasured_loudness),
    ("move_somafm_defaults_to_https", move_somafm_defaults_to_https),
    ("move_http_defaults_to_https", move_http_defaults_to_https),
    ("restore_musicbrainz_tags_after_lastfm", restore_musicbrainz_tags_after_lastfm),
    ("drop_deezer_catalog_art", drop_deezer_catalog_art),
    ("compact_after_lastfm_and_deezer_purge", compact_after_lastfm_and_deezer_purge),
    ("recheck_art_with_folder_covers", recheck_art_with_folder_covers),
)


def _run_data_backfills() -> int:
    ran = 0
    for key, step in DATA_BACKFILLS:
        with engine.connect() as conn:
            done = conn.exec_driver_sql(
                "SELECT value FROM system_settings WHERE key = ?", (key,)
            ).scalar()
        if done:
            continue
        logger.info("Running data backfill: %s", key)
        step()
        with engine.connect() as conn:
            conn.exec_driver_sql(
                "INSERT OR REPLACE INTO system_settings (key, value) VALUES (?, ?)", (key, "1")
            )
            conn.commit()
        ran += 1
    return ran


def _existing_tables() -> set[str]:
    with engine.connect() as conn:
        return {
            row[0]
            for row in conn.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        }


def backup_database(version: int) -> Path | None:
    """Copy the database to <data dir>/backups before it is upgraded from
    `version`, keeping the newest BACKUPS_KEPT copies. SQLite's online
    backup copies a consistent snapshot, including what is still in the WAL
    file, which a plain file copy would miss."""
    database = engine.url.database
    if not database or database == ":memory:":
        return None
    backups = Path(database).parent / "backups"
    backups.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    target = backups / f"atriolyric-v{version}-{stamp}.db"
    source = engine.raw_connection()
    try:
        live = source.driver_connection
        assert isinstance(live, sqlite3.Connection)  # the engine is always SQLite
        copy = sqlite3.connect(target)
        try:
            live.backup(copy)
        finally:
            copy.close()
    finally:
        source.close()
    copies = sorted(backups.glob("atriolyric-v*.db"), key=lambda path: (path.stat().st_mtime, path.name))
    for old in copies[:-BACKUPS_KEPT]:
        old.unlink(missing_ok=True)
    logger.warning(
        "Copied the database to %s before upgrading it from schema version %d", target, version
    )
    return target


def _read_schema_version() -> int:
    with engine.connect() as conn:
        value = conn.exec_driver_sql(
            "SELECT value FROM system_settings WHERE key = ?", (SCHEMA_VERSION_KEY,)
        ).scalar()
    try:
        return int(value) if value is not None else 0
    except ValueError:
        return 0


def _write_schema_version(version: int) -> None:
    with engine.connect() as conn:
        conn.exec_driver_sql(
            "INSERT OR REPLACE INTO system_settings (key, value) VALUES (?, ?)",
            (SCHEMA_VERSION_KEY, str(version)),
        )
        conn.commit()


def latest_schema_version() -> int:
    return len(MIGRATIONS)


def run_migrations() -> int:
    """Create missing tables, then run every step this database hasn't had,
    recording the version after each one so an interrupted upgrade resumes
    where it stopped. Returns how many steps ran.

    An existing database is copied first when any step is pending (see
    backup_database). One upgraded by a newer release is refused: this
    release's code would run against columns and meanings it does not know."""
    tables = _existing_tables()
    version = _read_schema_version() if "system_settings" in tables else 0
    if version > len(MIGRATIONS):
        if os.environ.get(ALLOW_NEWER_SCHEMA_ENV, "").strip().lower() not in ("1", "true", "yes"):
            raise NewerSchemaError(
                f"The database's schema version is {version}, but this release only knows "
                f"versions up to {len(MIGRATIONS)}: it was upgraded by a newer Atriolyric. "
                f"Run that release again, or restore a copy from the backups folder; "
                f"{ALLOW_NEWER_SCHEMA_ENV}=true starts anyway, at your own risk."
            )
        Base.metadata.create_all(bind=engine)
        logger.warning(
            "Database schema version %d is newer than this release knows (%d); "
            "leaving it untouched because %s is set",
            version,
            len(MIGRATIONS),
            ALLOW_NEWER_SCHEMA_ENV,
        )
        return 0
    if tables and version < len(MIGRATIONS):
        # Never for a fresh database (nothing to lose), nor when up to date.
        backup_database(version)
    Base.metadata.create_all(bind=engine)
    for number, step in enumerate(MIGRATIONS[version:], start=version + 1):
        logger.info("Applying schema migration %d: %s", number, step.__name__)
        step()
        _write_schema_version(number)
    _run_data_backfills()
    return len(MIGRATIONS) - version
