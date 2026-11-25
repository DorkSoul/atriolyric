from __future__ import annotations

import os
from unittest.mock import patch

from app.cleanup import delete_media_records
from app.library_tasks import _run_scan_task
from app.models import (
    LibraryRoot,
    LibraryTask,
    MediaItem,
    MediaKind,
    User,
    UserLibraryAccess,
    WantListItem,
)
from app.owned_match import match_owned
from app.security import hash_password
from app.want_list import match_want_list, wait_for_rematches
from tests.conftest import login_as, make_audio_fixture


def _root(db_session, path="/lib", shared=True) -> LibraryRoot:
    root = LibraryRoot(path=path, display_name=path, is_shared=shared)
    db_session.add(root)
    db_session.commit()
    return root


def _media(db_session, root, artist="Kalabi", title="Inside The Sun", kind=MediaKind.track) -> MediaItem:
    item = MediaItem(
        library_root_id=root.id, kind=kind, relative_path=f"{artist}-{title}-{kind.value}",
        title=title, artist=artist,
    )
    db_session.add(item)
    db_session.commit()
    return item


def _plain_user(db_session, session_factory, client, username="listener"):
    user = User(username=username, password_hash=hash_password("listener-pass"))
    db_session.add(user)
    db_session.commit()
    return user, login_as(session_factory, client, username, "listener-pass")


def test_adding_a_song_already_in_the_library_matches_ignoring_case(client, db_session):
    root = _root(db_session)
    _media(db_session, root, artist="Kalabi", title="Inside The Sun", kind=MediaKind.music_video)
    track = _media(db_session, root)
    resp = client.post(
        "/api/radio/want-list",
        json={"artist": " KALABI ", "title": "inside the sun", "station_name": "Groove Salad"},
    )
    assert resp.status_code == 200
    assert resp.json()["media_item_id"] == track.id

    other = client.post("/api/radio/want-list", json={"artist": "Kalabi", "title": "Other Song"}).json()
    assert other["media_item_id"] is None


def test_no_match_in_a_root_the_user_cannot_access(session_factory, client, db_session):
    private = _root(db_session, "/private", shared=False)
    track = _media(db_session, private)
    user, listener = _plain_user(db_session, session_factory, client)
    resp = listener.post("/api/radio/want-list", json={"artist": "Kalabi", "title": "Inside The Sun"})
    assert resp.json()["media_item_id"] is None

    # Granting access makes a read queue a re-match, which links it...
    db_session.add(UserLibraryAccess(user_id=user.id, library_root_id=private.id))
    db_session.commit()
    listener.get("/api/want-list/entries")
    wait_for_rematches()
    assert listener.get("/api/want-list/entries").json()[0]["media_item_id"] == track.id

    # ...and revoking it unlinks it again.
    db_session.query(UserLibraryAccess).delete()
    db_session.commit()
    listener.get("/api/want-list/entries")
    wait_for_rematches()
    assert listener.get("/api/want-list/entries").json()[0]["media_item_id"] is None


def test_deleting_the_media_clears_the_match(client, db_session):
    root = _root(db_session)
    track = _media(db_session, root)
    entry_id = client.post(
        "/api/radio/want-list", json={"artist": "Kalabi", "title": "Inside The Sun"}
    ).json()["id"]
    delete_media_records(db_session, [track.id])
    db_session.commit()
    entry = db_session.get(WantListItem, entry_id)
    db_session.refresh(entry)
    assert entry.media_item_id is None


def test_a_scan_that_finds_the_song_matches_it_again(client, db_session, library_dir, session_factory):
    entry = WantListItem(user_id=1, artist="Kalabi", title="Inside The Sun")
    db_session.add(entry)
    db_session.commit()
    root = _root(db_session, str(library_dir))

    def scan(mode: str = "normal") -> None:
        task = LibraryTask(library_root_id=root.id, kind="scan", status="running")
        db_session.add(task)
        db_session.commit()
        with patch("app.library_tasks.start_loudness_backfill"):
            _run_scan_task(root.id, task.id, mode)
        db_session.expire_all()

    song = library_dir / "sun.wav"
    make_audio_fixture(song, title="Inside The Sun", artist="Kalabi", album=None, genre=None)
    scan()
    first_match = db_session.get(WantListItem, entry.id).media_item_id
    assert first_match is not None

    song.unlink()
    # The file is gone: its row is removed and the match cleared. Confirmed,
    # because a scan that finds no files at all otherwise removes nothing.
    scan("confirm_removals")
    assert db_session.get(WantListItem, entry.id).media_item_id is None

    make_audio_fixture(song, title="Inside The Sun", artist="Kalabi", album=None, genre=None)
    os.utime(song, (song.stat().st_atime, song.stat().st_mtime + 5))
    scan()
    rematch = db_session.get(WantListItem, entry.id).media_item_id
    # SQLite may hand the new row the old id; what matters is that it's
    # linked to the row the latest scan created.
    assert rematch == db_session.query(MediaItem.id).filter_by(relative_path="sun.wav").scalar()


def test_match_all_users_and_report_changes(db_session):
    root = _root(db_session)
    _media(db_session, root, artist="A", title="T")
    db_session.add_all([
        WantListItem(user_id=1, artist="a", title="t"),
        WantListItem(user_id=1, artist="nobody", title="nothing"),
    ])
    db_session.commit()
    assert match_want_list(db_session) == 1
    assert match_want_list(db_session) == 0  # already linked, nothing to do


def test_user_without_any_accessible_root_matches_nothing(session_factory, client, db_session):
    root = _root(db_session, "/private", shared=False)
    _media(db_session, root)
    user, _ = _plain_user(db_session, session_factory, client)
    db_session.add(WantListItem(user_id=user.id, artist="Kalabi", title="Inside The Sun"))
    db_session.commit()
    assert match_want_list(db_session, [user.id]) == 0


def test_csv_export_defuses_spreadsheet_formulas(client):
    import csv
    import io

    for artist, title in [("=HYPERLINK(\"http://x\")", "+1"), ("-cmd", "@sum"), ("Safe", "a-b")]:
        assert client.post("/api/want-list", json={"kind": "track", "artist": artist, "title": title}).status_code == 200
    body = client.get("/api/want-list/export", params={"format": "csv"}).text
    cells = {cell for row in csv.DictReader(io.StringIO(body)) for key in ("artist", "title") for cell in [row[key]]}
    assert cells == {
        "'=HYPERLINK(\"http://x\")", "'+1", "'-cmd", "'@sum", "Safe", "a-b",
    }
    # Names are stripped on the way in, so tab and CR are checked directly.
    from app.want_list import _csv_safe

    assert [_csv_safe(v) for v in ("\tTab", "\rCR", "plain", 5, None)] == ["'\tTab", "'\rCR", "plain", 5, None]
    # JSON is data, not something a spreadsheet opens: left as is.
    rows = client.get("/api/want-list/export", params={"format": "json"}).json()
    assert "=HYPERLINK(\"http://x\")" in {r["artist"] for r in rows}


# --- performance --------------------------------------------------------------


def test_every_unmatched_entry_and_album_is_matched_in_one_library_lookup(db_session):
    from app.external_cache import store
    from tests.test_want_catalog import _release

    root = _root(db_session)
    owned = _media(db_session, root, artist="A", title="Song 3")
    _media(db_session, root, artist="A", title="Opener")
    group = "00000000-0000-4000-8000-0000000000aa"
    store(db_session, "musicbrainz", "release-group", group, {"id": group, "releases": [{"id": "rel-a", "status": "Official", "date": "2004"}]})
    store(db_session, "musicbrainz", "release", "rel-a", _release("rel-a", ["Opener", "Missing"]))
    db_session.add_all([WantListItem(user_id=1, artist="A", title=f"Song {n}") for n in range(20)])
    db_session.add(WantListItem(user_id=1, kind="album", artist="A", title="Album", release_group_mbid=group))
    db_session.commit()

    with patch("app.want_list.match_owned", wraps=match_owned) as lookup:
        assert match_want_list(db_session) == 2
    assert lookup.call_count == 1
    assert len(lookup.call_args.args[2]) == 22  # 20 songs + the album's 2 tracks
    by_title = {e.title: e for e in db_session.query(WantListItem)}
    assert by_title["Song 3"].media_item_id == owned.id
    assert (by_title["Album"].owned_count, by_title["Album"].total_count) == (1, 2)


def test_reads_rematch_only_after_the_library_or_access_changes(session_factory, client, db_session):
    from app.library_cache import bump_library_version

    root = _root(db_session)
    client.post("/api/radio/want-list", json={"artist": "Kalabi", "title": "Inside The Sun"})
    with patch("app.want_list.match_owned", return_value=[None]) as lookup:
        client.get("/api/want-list/entries")
        client.get("/api/want-list/artists")
        wait_for_rematches()
        assert lookup.call_count == 0  # nothing changed since the add matched

    # A track added behind the cache's back is found once the library
    # version moves, as every scan, tag edit and enrichment run does.
    track = _media(db_session, root)
    client.get("/api/want-list/entries")
    wait_for_rematches()
    assert client.get("/api/want-list/entries").json()[0]["media_item_id"] is None
    bump_library_version()
    assert client.get("/api/want-list/entries").json()[0]["media_item_id"] is None  # as last matched
    wait_for_rematches()
    assert client.get("/api/want-list/entries").json()[0]["media_item_id"] == track.id


def test_a_read_after_a_library_change_does_not_match_on_the_request(client, db_session):
    """Matching scans the user's whole want list against the library; a GET
    answers with the stored state and leaves the re-match to a background
    thread."""
    import threading

    from app.library_cache import bump_library_version

    root = _root(db_session)
    client.post("/api/radio/want-list", json={"artist": "Kalabi", "title": "Inside The Sun"})
    _media(db_session, root)
    bump_library_version()
    threads: list[str] = []

    def record_thread(*args, **kwargs):
        threads.append(threading.current_thread().name)
        return match_owned(*args, **kwargs)

    with patch("app.want_list.match_owned", side_effect=record_thread):
        assert client.get("/api/want-list/entries").json()[0]["media_item_id"] is None
        wait_for_rematches()
        # Two reads while nothing changed queue no second re-match.
        client.get("/api/want-list/entries")
        wait_for_rematches()
    assert threads == ["want-list-match"]


def test_a_tracklist_fetched_later_recounts_the_album_on_the_next_read(client, db_session):
    from app.external_cache import store
    from tests.test_want_catalog import _release

    root = _root(db_session)
    _media(db_session, root, artist="A", title="Opener")
    group = "00000000-0000-4000-8000-0000000000bb"
    with patch("app.want_list.tracklist", return_value=None):
        entry = client.post(
            "/api/want-list", json={"kind": "album", "artist": "A", "title": "Album", "release_group_mbid": group}
        ).json()
    assert entry["total_count"] is None
    store(db_session, "musicbrainz", "release-group", group, {"id": group, "releases": [{"id": "rel-b", "status": "Official", "date": "2004"}]})
    store(db_session, "musicbrainz", "release", "rel-b", _release("rel-b", ["Opener", "Missing"]))
    client.get("/api/want-list/entries")
    wait_for_rematches()
    row = next(r for r in client.get("/api/want-list/entries").json() if r["id"] == entry["id"])
    assert (row["owned_count"], row["total_count"]) == (1, 2)


def test_want_list_lookups_use_an_index(db_session):
    """match_owned's queries must be index searches, not scans of the
    library - including the lower(artist) fallback."""
    from sqlalchemy import event

    root = _root(db_session)
    _media(db_session, root)
    engine = db_session.get_bind()
    statements: list[tuple[str, object]] = []

    def capture(_conn, _cursor, statement, parameters, _context, _many):
        if statement.lstrip().upper().startswith("SELECT") and "media_items" in statement:
            statements.append((statement, parameters))

    event.listen(engine, "before_cursor_execute", capture)
    try:
        match_owned(db_session, [root.id], [{"artist": "Kalabi", "title": "Inside The Sun", "mbid": "rec-1"}])
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert len(statements) == 3
    with engine.connect() as conn:
        for statement, parameters in statements:
            plan = " ".join(
                row[3] for row in conn.exec_driver_sql(f"EXPLAIN QUERY PLAN {statement}", parameters)
            )
            assert "SCAN media_items" not in plan, plan
            assert "idx_media_items_root_kind" not in plan, plan
    # The fallback searches the lower(artist) expression index itself.
    with engine.connect() as conn:
        fallback_plan = " ".join(
            row[3]
            for row in conn.exec_driver_sql(f"EXPLAIN QUERY PLAN {statements[2][0]}", statements[2][1])
        )
    assert "idx_media_items_lower_artist_root_kind" in fallback_plan, fallback_plan


def test_the_artist_string_fallback_matches_a_non_ascii_capitalised_artist(db_session):
    """SQLite's lower() only folds ASCII, so the values the fallback looks up
    in the lower(artist) index must be folded the same way - Python's
    str.lower() turns "ÓLAFUR" into "ólafur", which the index (holding
    "Ólafur") never contains. The track is not linked to an Artist row, as
    for a scan that has not linked it yet, so only the fallback can match."""
    root = _root(db_session)
    track = _media(db_session, root, artist="ÓLAFUR ARNALDS", title="Saman")
    assert match_owned(db_session, [root.id], [{"artist": "Ólafur Arnalds", "title": "saman"}]) == [track.id]
