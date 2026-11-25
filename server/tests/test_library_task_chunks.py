"""The bulk provider tasks list ids and load rows a chunk at a time: same
outcome on a small library, a row deleted mid-task is
skipped, the deferred recording JSON is loaded with the chunk rather than
once per row, and an album is re-derived once per batch rather than once
per enriched track."""

from __future__ import annotations

from unittest.mock import patch

from app import library_tasks
from app.albums import refresh_album_details as real_refresh_album_details
from app.artist_credits import ROLE_MAIN
from app.cleanup import delete_media_records
from app.enrichment import enrich_media_item
from app.metadata_providers.musicbrainz import LookupResult
from app.metadata_providers.result import Found
from app.models import (
    Album,
    Artist,
    LibraryRoot,
    LibraryTask,
    MediaItem,
    MediaItemMusicBrainzTag,
    MediaKind,
    TrackArtist,
)
from tests.test_large_columns import recorded_statements

RECORDING = {
    "releases": [
        {
            "id": "release-1",
            "title": "LP",
            "status": "Official",
            "release-group": {"id": "group-1", "primary-type": "Album"},
        }
    ],
    "tags": [{"name": "dreamy", "count": 3}],
}


def _root_with_tracks(db_session, count: int, kind: str, **fields) -> tuple[int, int, list[int]]:
    root = LibraryRoot(path=f"/{kind}", display_name=kind)
    db_session.add(root)
    db_session.commit()
    items = [
        MediaItem(
            library_root_id=root.id,
            kind=MediaKind.track,
            relative_path=f"{n}.mp3",
            title=f"Song {n}",
            artist="Band",
            album="LP",
            **fields,
        )
        for n in range(count)
    ]
    task = LibraryTask(library_root_id=root.id, kind=kind, status="running")
    db_session.add_all([*items, task])
    db_session.commit()
    return root.id, task.id, [item.id for item in items]


def _per_row_loads(statements: list[str]) -> list[str]:
    """Lazy loads of the deferred recording column, one row at a time."""
    return [
        s
        for s in statements
        if s.lstrip().startswith("SELECT media_items.musicbrainz_metadata_json")
        and "media_items.title" not in s
    ]


def test_enrichment_task_skips_a_row_deleted_mid_task(db_session, session_factory, monkeypatch):
    monkeypatch.setattr(library_tasks, "SessionLocal", session_factory)
    monkeypatch.setattr(library_tasks, "ITEM_CHUNK", 2)
    root_id, task_id, ids = _root_with_tracks(db_session, 5, "enrich")
    deleted = ids[3]  # in the second chunk, not loaded yet
    seen: list[int] = []

    def enrich(db, item, *, commit, fetch_cover_art):
        if not seen:
            delete_media_records(db_session, [deleted])
            db_session.commit()
        seen.append(item.id)
        return {"matched": True, "metadata_saved": True, "status": "complete"}

    monkeypatch.setattr(library_tasks, "enrich_media_item", enrich)
    library_tasks._run_enrichment_task(root_id, task_id)

    db_session.expire_all()
    task = db_session.get(LibraryTask, task_id)
    assert seen == [i for i in ids if i != deleted]
    assert (task.status, task.total, task.processed, task.matched) == ("completed", 5, 5, 4)


def test_enrichment_task_refreshes_each_album_once_per_batch(db_session, session_factory, monkeypatch):
    monkeypatch.setattr(library_tasks, "SessionLocal", session_factory)
    root_id, task_id, ids = _root_with_tracks(db_session, 3, "enrich_all", musicbrainz_recording_mbid="rec")
    with (
        patch(
            "app.enrichment.musicbrainz.fetch_recording_result",
            return_value=LookupResult(value=RECORDING),
        ),
        patch("app.enrichment.refresh_album_details", wraps=real_refresh_album_details) as refresh,
    ):
        library_tasks._run_enrichment_task(root_id, task_id, refresh_all=True)

    assert refresh.call_count == 1  # three tracks, one album, one batch
    db_session.expire_all()
    task = db_session.get(LibraryTask, task_id)
    assert (task.status, task.processed, task.matched) == ("completed", 3, 3)
    album = db_session.query(Album).one()
    assert (album.release_group_mbid, album.release_mbid) == ("group-1", "release-1")
    assert {i.album_id for i in db_session.query(MediaItem).filter(MediaItem.id.in_(ids))} == {album.id}


def test_a_single_enrichment_still_refreshes_its_album_at_once(db_session):
    _root_id, _task_id, (item_id,) = _root_with_tracks(db_session, 1, "single", musicbrainz_recording_mbid="rec")
    item = db_session.get(MediaItem, item_id)
    with (
        patch(
            "app.enrichment.musicbrainz.fetch_recording_result",
            return_value=LookupResult(value=RECORDING),
        ),
        patch("app.enrichment.refresh_album_details", wraps=real_refresh_album_details) as refresh,
    ):
        enrich_media_item(db_session, item)
    assert refresh.call_count == 1
    assert db_session.query(Album).one().release_group_mbid == "group-1"


def test_cover_art_task_loads_recordings_with_the_chunk_and_skips_a_deleted_row(
    db_session, session_factory, monkeypatch
):
    monkeypatch.setattr(library_tasks, "SessionLocal", session_factory)
    monkeypatch.setattr(library_tasks, "ITEM_CHUNK", 2)
    root_id, task_id, ids = _root_with_tracks(
        db_session, 5, "cover_art", musicbrainz_metadata_json=RECORDING
    )
    deleted = ids[4]
    asked: list[str] = []

    def front_image(release_mbid, release_group_mbid=None):
        if not asked:
            delete_media_records(db_session, [deleted])
            db_session.commit()
        asked.append(release_mbid)
        return Found(f"https://art/{len(asked)}")

    with (
        patch("app.enrichment.coverart.front_image", side_effect=front_image),
        recorded_statements(session_factory.kw["bind"]) as statements,
    ):
        library_tasks._run_cover_art_task(root_id, task_id)

    assert _per_row_loads(statements) == []
    db_session.expire_all()
    task = db_session.get(LibraryTask, task_id)
    assert (task.status, task.total, task.processed, task.matched) == ("completed", 5, 5, 4)
    assert asked == ["release-1"] * 4
    art = dict(db_session.query(MediaItem.id, MediaItem.cover_art_url).filter(MediaItem.id.in_(ids)))
    assert deleted not in art
    assert all(url and url.startswith("https://art/") for url in art.values())


def test_listenbrainz_tags_task_asks_once_per_batch_of_artists(
    db_session, session_factory, monkeypatch
):
    monkeypatch.setattr(library_tasks, "SessionLocal", session_factory)
    monkeypatch.setattr(library_tasks, "_TAG_BATCH", 2)
    root_id, task_id, ids = _root_with_tracks(
        db_session, 3, "lb_tags", musicbrainz_metadata_json=RECORDING, musicbrainz_artist_mbid="00000007-0000-4000-8000-000000000007"
    )
    with (
        patch("app.library_tasks.listenbrainz.artist_tags", return_value={"00000007-0000-4000-8000-000000000007": [("shoegaze", 10)]}) as ask,
        recorded_statements(session_factory.kw["bind"]) as statements,
    ):
        library_tasks._run_listenbrainz_tags_task(root_id, task_id)

    assert _per_row_loads(statements) == []
    assert [call.args[0] for call in ask.call_args_list] == [["00000007-0000-4000-8000-000000000007"], ["00000007-0000-4000-8000-000000000007"]]
    db_session.expire_all()
    task = db_session.get(LibraryTask, task_id)
    assert (task.status, task.total, task.processed, task.matched) == ("completed", 3, 3, 3)
    sources = {
        (row.media_item_id, row.source)
        for row in db_session.query(MediaItemMusicBrainzTag).filter(MediaItemMusicBrainzTag.media_item_id.in_(ids))
    }
    assert sources == {(i, "listenbrainz") for i in ids}
    assert all(item.online_tags_fetched_at for item in db_session.query(MediaItem).filter(MediaItem.id.in_(ids)))


def _recording_sessions(monkeypatch, session_factory) -> list:
    sessions: list = []

    def factory():
        session = session_factory()
        sessions.append(session)
        return session

    monkeypatch.setattr(library_tasks, "SessionLocal", factory)
    return sessions


def test_bulk_provider_tasks_hold_no_open_transaction_during_a_provider_call(
    db_session, session_factory, monkeypatch
):
    """A provider call can take seconds (or a rate-limit pause). Made while
    the previous item's writes are still uncommitted, it would hold SQLite's
    single write lock for that whole time and stall every other writer, so
    each task commits before it asks a provider about the next item."""
    sessions = _recording_sessions(monkeypatch, session_factory)
    in_transaction: list[bool] = []

    def provider(value):
        def call(*_args, **_kwargs):
            in_transaction.append(sessions[-1].in_transaction())
            return value

        return call

    root_id, task_id, _ids = _root_with_tracks(
        db_session, 3, "lb_tags", musicbrainz_metadata_json=RECORDING, musicbrainz_artist_mbid="00000007-0000-4000-8000-000000000007"
    )
    monkeypatch.setattr(library_tasks, "_TAG_BATCH", 1)
    with patch("app.library_tasks.listenbrainz.artist_tags", side_effect=provider({"00000007-0000-4000-8000-000000000007": [("shoegaze", 10)]})):
        library_tasks._run_listenbrainz_tags_task(root_id, task_id)

    root_id, task_id, _ids = _root_with_tracks(db_session, 3, "mbid")
    with patch(
        "app.enrichment.musicbrainz.search_recording_result",
        side_effect=provider(LookupResult(value={"recording_mbid": "rec-1"})),
    ):
        library_tasks._run_mbid_lookup_task(root_id, task_id)

    root_id, task_id, _ids = _root_with_tracks(
        db_session, 3, "enrich", musicbrainz_recording_mbid="rec-1"
    )
    with patch(
        "app.enrichment.musicbrainz.fetch_recording_result",
        side_effect=provider(LookupResult(value=RECORDING)),
    ):
        library_tasks._run_enrichment_task(root_id, task_id)

    root_id, task_id, _ids = _root_with_tracks(
        db_session, 3, "cover_art", musicbrainz_metadata_json=RECORDING
    )
    with patch("app.enrichment.coverart.front_image", side_effect=provider(Found("https://art/1"))):
        library_tasks._run_cover_art_task(root_id, task_id)

    root_id, task_id, ids = _root_with_tracks(db_session, 2, "artist_info")
    for n, item_id in enumerate(ids):
        artist = Artist(name=f"Artist {n}", normalized_name=f"artist {n}")
        db_session.add(artist)
        db_session.flush()
        db_session.add(TrackArtist(media_item_id=item_id, artist_id=artist.id, role=ROLE_MAIN))
    db_session.commit()
    with (
        patch.object(library_tasks, "artist_info", side_effect=provider({"bio": "b"})),
        patch.object(library_tasks, "artist_top_tracks", side_effect=provider([])),
    ):
        library_tasks._run_artist_info_task(root_id, task_id, 1)

    assert len(in_transaction) == 3 * 4 + 2 * 2
    assert not any(in_transaction)
