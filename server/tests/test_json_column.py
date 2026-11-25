"""The `*_json` columns decode and encode themselves (app.json_column) while
storing exactly the text a plain json.dumps call with each column's options
stores, so existing rows load, and rewrite to the same bytes."""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.json_column import JSONText
from app.models import (
    Album,
    Artist,
    ArtistMetadataCache,
    Base,
    CustomArt,
    DiscoveryState,
    LibraryRoot,
    MediaItem,
    PlaybackState,
    Playlist,
    PlaySourceHistory,
    User,
)

# (model, column, declared empty value, the json.dumps options a value is
# stored with, a value as stored). The non-ASCII names check ensure_ascii;
# the unsorted dicts check sort_keys.
OLD_WRITERS: list[tuple[type, str, Any, dict, Any]] = [
    (User, "dismissed_notices_json", [], {}, ["admin_lastfm_key", "z_notice"]),
    (User, "eq_gains_json", None, {}, [1.5, -3.0, 0.0, 12.0, -12.0, 0.25, 0.0, 0.0, 0.0, 2.0]),
    (MediaItem, "artist_tags_json", None, {"ensure_ascii": False},
     {"artists": ["Björk", "Sigur Rós"], "album_artists": ["Ásgeir", "Björk"]}),
    (MediaItem, "musicbrainz_metadata_json", None,
     {"ensure_ascii": False, "sort_keys": True, "separators": (",", ":")},
     {"title": "Jóga", "id": "rec-1", "releases": [{"title": "Homogénic", "id": "r"}], "isrcs": ["GB1"]}),
    (Artist, "info_json", None, {}, {"bio": "x"}),
    (Album, "secondary_types_json", [], {}, ["Live", "Compilation"]),
    (Album, "info_json", None, {}, {"wiki": "y"}),
    (Playlist, "preview_media_item_ids_json", [], {}, [4, 2, 9]),
    (PlaySourceHistory, "payload_json", None, {"sort_keys": True},
     {"filters": {"q": "Sigur Rós", "genre": None}, "artist": "Mø"}),
    (PlaySourceHistory, "preview_media_item_ids_json", [], {}, [1, 2, 3]),
    (PlaybackState, "queue_context_json", None, {},
     {"kind": "library", "filters": {"q": "Ólafur"}, "exclude_ids": [3, 1]}),
    # Encoded like queue_context_json.
    (PlaybackState, "queue_origin_json", None, {},
     {"kind": "album", "payload": {"artist": "Sigur Rós", "album": "Ágætis byrjun"}}),
    (ArtistMetadataCache, "similar_artists_json", None, {}, sorted(["Múm", "Amiina"])),
    (ArtistMetadataCache, "similar_scores_json", None, {"sort_keys": True}, {"Múm": 0.9, "Amiina": 1.0}),
    (DiscoveryState, "discovery_seed_artists_json", [], {}, ["Röyksopp", "Air"]),
    (DiscoveryState, "discovery_similar_artists_json", [], {}, sorted(["Air", "Zero 7"])),
    (DiscoveryState, "explore_seed_artists_json", [], {}, ["Air"]),
    (DiscoveryState, "explore_results_json", [], {}, [{"title": "Ñ", "artist": "Café Tacvba", "id": 3}]),
    (CustomArt, "credit_json", None, {}, {"author": "Fotógrafo", "license": "CC BY 4.0"}),
]


def test_every_json_column_is_covered_and_external_cache_is_left_alone():
    converted = {
        (table.name, column.name)
        for table in Base.metadata.sorted_tables
        for column in table.columns
        if isinstance(column.type, JSONText)
    }
    assert converted == {(model.__tablename__, column) for model, column, *_ in OLD_WRITERS}
    # Every other *_json column is a deliberate exception.
    left = {
        (table.name, column.name)
        for table in Base.metadata.sorted_tables
        for column in table.columns
        if column.name.endswith("_json") and not isinstance(column.type, JSONText)
    }
    assert left == {("external_cache", "payload_json")}


@pytest.fixture()
def rows(db_session: Session) -> dict[type, Any]:
    """One row of every table with a JSON column, its primary key by model."""
    user = db_session.scalars(select(User)).one()
    root = LibraryRoot(path="/m", display_name="m")
    db_session.add(root)
    db_session.flush()
    item = MediaItem(library_root_id=root.id, relative_path="a.mp3", title="A")
    artist = Artist(name="A", normalized_name="a")
    album = Album(title="T", title_key="t")
    playlist = Playlist(user_id=user.id, name="P")
    history = PlaySourceHistory(user_id=user.id, kind="album", label="L", payload_json={"album": "T"})
    state = PlaybackState(user_id=user.id)
    cache = ArtistMetadataCache(artist="A")
    discovery = DiscoveryState(user_id=user.id)
    art = CustomArt(entity_type="artist", entity_key="A", path="a.jpg", thumb_path="a.thumb.jpg")
    db_session.add_all([item, artist, album, playlist, history, state, cache, discovery, art])
    db_session.commit()
    return {
        User: user.id, MediaItem: item.id, Artist: artist.id, Album: album.id, Playlist: playlist.id,
        PlaySourceHistory: history.id, PlaybackState: user.id, ArtistMetadataCache: "A",
        DiscoveryState: user.id, CustomArt: art.id,
    }


def _key_column(model: type) -> str:
    return next(iter(model.__table__.primary_key.columns)).name


def _store_raw(db: Session, model: type, pk: Any, column: str, value: str | None) -> None:
    db.execute(
        # Table and column names come from the models above, never from input.
        text(f"UPDATE {model.__tablename__} SET {column} = :v WHERE {_key_column(model)} = :pk"),  # noqa: S608
        {"v": value, "pk": pk},
    )
    db.commit()
    db.expire_all()


def _raw(db: Session, model: type, pk: Any, column: str) -> str | None:
    return db.execute(
        text(f"SELECT {column} FROM {model.__tablename__} WHERE {_key_column(model)} = :pk"),  # noqa: S608
        {"pk": pk},
    ).scalar_one()


@pytest.mark.parametrize(("model", "column", "empty", "_opts", "_value"), OLD_WRITERS)
@pytest.mark.parametrize("stored", [None, "", "not json", "null"])
def test_old_unreadable_rows_load_as_the_empty_value(db_session, rows, model, column, empty, _opts, _value, stored):
    if stored is None and not model.__table__.c[column].nullable:
        pytest.skip("NOT NULL column")
    _store_raw(db_session, model, rows[model], column, stored)
    row = db_session.get(model, rows[model])
    assert getattr(row, column) == empty
    # Loading writes nothing back, and re-assigning the loaded value writes
    # nothing either (it is equal), so the stored text stays as it was.
    setattr(row, column, getattr(row, column))
    db_session.commit()
    assert _raw(db_session, model, rows[model], column) == stored


@pytest.mark.parametrize(("model", "column", "_empty", "opts", "value"), OLD_WRITERS)
def test_rows_written_by_the_old_code_round_trip_byte_identical(db_session, rows, model, column, _empty, opts, value):
    old_text = json.dumps(value, **opts)
    _store_raw(db_session, model, rows[model], column, old_text)
    row = db_session.get(model, rows[model])
    assert getattr(row, column) == value
    flag_modified(row, column)  # force the UPDATE although nothing changed
    db_session.commit()
    assert _raw(db_session, model, rows[model], column) == old_text
    # And a fresh assignment of the same value is stored the same way.
    setattr(row, column, json.loads(old_text))
    flag_modified(row, column)
    db_session.commit()
    assert _raw(db_session, model, rows[model], column) == old_text


def test_a_json_string_is_refused_instead_of_being_encoded_twice(db_session, rows):
    playlist = db_session.get(Playlist, rows[Playlist])
    playlist.preview_media_item_ids_json = "[1, 2]"  # type: ignore[assignment]
    with pytest.raises(Exception, match="not JSON text"):
        db_session.commit()
    db_session.rollback()
    assert _raw(db_session, Playlist, rows[Playlist], "preview_media_item_ids_json") is None


def test_changes_are_saved_by_assignment_not_in_place(db_session, rows):
    """The rule every call site follows: a loaded list/dict is read-only and
    a change is a new value assigned to the attribute."""
    _store_raw(db_session, DiscoveryState, rows[DiscoveryState], "explore_seed_artists_json", '["Air"]')
    state = db_session.get(DiscoveryState, rows[DiscoveryState])
    state.explore_seed_artists_json = [*state.explore_seed_artists_json, "Moby"]
    db_session.commit()
    assert _raw(db_session, DiscoveryState, rows[DiscoveryState], "explore_seed_artists_json") == '["Air", "Moby"]'
    # In place, SQLAlchemy sees no change (why the rule exists): nothing is written.
    state = db_session.get(DiscoveryState, rows[DiscoveryState])
    state.explore_seed_artists_json.append("Not saved")
    db_session.commit()
    assert _raw(db_session, DiscoveryState, rows[DiscoveryState], "explore_seed_artists_json") == '["Air", "Moby"]'


def test_new_rows_store_null_and_list_defaults_as_before(db_session, rows):
    # The discovery lists default to the text "[]"; nullable columns to NULL.
    for column in ("discovery_seed_artists_json", "discovery_similar_artists_json",
                   "explore_seed_artists_json", "explore_results_json"):
        assert _raw(db_session, DiscoveryState, rows[DiscoveryState], column) == "[]"
    assert _raw(db_session, Playlist, rows[Playlist], "preview_media_item_ids_json") is None
    assert _raw(db_session, User, rows[User], "dismissed_notices_json") is None


def test_play_source_written_before_the_change_is_found_by_its_payload(client, db_session):
    """payload_json is a lookup key: a row stored with sorted keys and
    ASCII escapes is matched and updated, not duplicated."""
    payload = {"filters": {"q": "Sigur Rós", "genre": "Post-rock"}}
    db_session.execute(
        text(
            "INSERT INTO play_source_history (user_id, kind, label, payload_json, updated_at) "
            "VALUES (1, 'library', 'Old', :p, '2026-01-01 00:00:00')"
        ),
        {"p": json.dumps(payload, sort_keys=True)},
    )
    db_session.commit()
    resp = client.post("/api/library/play-source", json={"kind": "library", "label": "New", "payload": payload})
    assert resp.status_code == 204
    db_session.expire_all()
    rows = db_session.execute(text("SELECT label, payload_json FROM play_source_history")).all()
    assert rows == [("New", json.dumps(payload, sort_keys=True))]
    assert db_session.scalars(
        select(PlaySourceHistory).where(PlaySourceHistory.payload_json == payload)
    ).one().label == "New"


def test_declared_column_types_are_unchanged():
    # The DDL of a plain String/Text column (VARCHAR where it was inferred).
    assert str(User.__table__.c.dismissed_notices_json.type.compile()) == "VARCHAR"
    assert str(MediaItem.__table__.c.musicbrainz_metadata_json.type.compile()) == "TEXT"
    assert str(PlaySourceHistory.__table__.c.payload_json.type.compile()) == "VARCHAR"
    assert str(Playlist.__table__.c.preview_media_item_ids_json.type.compile()) == "TEXT"
