from __future__ import annotations

import json
from datetime import timedelta
from unittest.mock import patch

from app.albums import rebuild_albums
from app.artist_credits import rebuild_all_credits
from app.external_cache import store
from app.models import (
    Album,
    CustomArt,
    ExternalCache,
    LibraryRoot,
    MediaItem,
    User,
    WantListItem,
    utcnow,
)
from app.security import hash_password
from app.want_list import refresh_wanted_catalogs
from tests.conftest import DEFAULT_USERNAME, login_as

ARTIST = "00000000-0000-4000-8000-000000000001"
GROUPS = [
    {"mbid": "00000000-0000-4000-8000-000000000002", "title": "Debut", "primary_type": "Album", "secondary_types": [], "section": "Album", "first_release_date": "2001-01-01", "disambiguation": None},
    {"mbid": "00000000-0000-4000-8000-000000000003", "title": "Second", "primary_type": "Album", "secondary_types": [], "section": "Album", "first_release_date": "2004-01-01", "disambiguation": None},
    {"mbid": "00000000-0000-4000-8000-000000000004", "title": "Hit Song", "primary_type": "Single", "secondary_types": [], "section": "Single", "first_release_date": "2001-02-01", "disambiguation": None},
    {"mbid": "00000000-0000-4000-8000-000000000005", "title": "Live Somewhere", "primary_type": "Album", "secondary_types": ["Live"], "section": "Live", "first_release_date": "2006-01-01", "disambiguation": None},
]


def _user(db_session) -> User:
    return db_session.query(User).filter_by(username=DEFAULT_USERNAME).one()


def _release(release_id: str, titles: list[str]) -> dict:
    return {
        "id": release_id,
        "title": "x",
        "media": [
            {
                "position": 1,
                "tracks": [
                    {"position": i + 1, "title": t, "recording": {"id": f"rec-{t}", "title": t, "length": 200000}}
                    for i, t in enumerate(titles)
                ],
            }
        ],
    }


def _library(db_session):
    root = LibraryRoot(path="/m", display_name="m")
    db_session.add(root)
    db_session.commit()
    recording = (
        {
            "id": "rec-Hit Song",
            "artist-credit": [{"name": "The Band", "artist": {"id": ARTIST, "name": "The Band"}}],
            "releases": [
                {"id": "rel-debut", "title": "Debut", "status": "Official", "release-group": {"id": "00000000-0000-4000-8000-000000000002", "primary-type": "Album"}}
            ],
        }
    )
    for index, title in enumerate(["Hit Song", "Opener"]):
        db_session.add(
            MediaItem(
                library_root_id=root.id,
                relative_path=f"{index}.mp3",
                title=title,
                artist="The Band",
                album="Debut",
                musicbrainz_recording_mbid=f"rec-{title}" if index == 0 else None,
                musicbrainz_metadata_json=recording if index == 0 else None,
            )
        )
    db_session.commit()
    rebuild_all_credits(db_session)
    rebuild_albums(db_session)
    store(db_session, "musicbrainz", "artist-release-groups", ARTIST, GROUPS)
    return root


def _cache_tracklist(db_session, group_mbid: str, release_id: str, titles: list[str]):
    store(db_session, "musicbrainz", "release-group", group_mbid, {"id": group_mbid, "title": group_mbid, "releases": [{"id": release_id, "status": "Official", "date": "2004"}]})
    store(db_session, "musicbrainz", "release", release_id, _release(release_id, titles))


def test_catalog_marks_owned_partial_missing_and_singles_by_song(client, db_session):
    _library(db_session)
    assert db_session.query(Album).one().release_group_mbid == "00000000-0000-4000-8000-000000000002"
    _cache_tracklist(db_session, "00000000-0000-4000-8000-000000000003", "rel-second", ["Opener", "New One"])
    body = client.get("/api/catalog/artist", params={"mbid": ARTIST}).json()
    assert body["in_library"] is True and body["artist"] == "The Band"
    status = {g["title"]: (g["status"], g["owned_count"], g["total_count"]) for g in body["release_groups"]}
    assert status["Debut"][0] == "owned"
    assert status["Hit Song"][0] == "owned"  # the single's song is on the owned album
    assert status["Second"] == ("partial", 1, 2)
    assert status["Live Somewhere"][0] == "missing"
    assert [g["title"] for g in body["release_groups"]][0] == "Live Somewhere"  # newest first
    assert (body["tracklists_known"], body["tracklists_total"]) == (1, 4)


def test_catalog_by_library_name_resolves_the_mbid(client, db_session):
    _library(db_session)
    body = client.get("/api/catalog/artist", params={"name": "The Band"}).json()
    assert body["artist_mbid"] == ARTIST


def test_release_group_page_lists_owned_and_missing_songs(client, db_session):
    _library(db_session)
    _cache_tracklist(db_session, "00000000-0000-4000-8000-000000000003", "rel-second", ["Opener", "New One"])
    with patch("app.metadata_info.musicbrainz.lookup_release_group") as lookup:
        lookup.side_effect = AssertionError("should be cached")
        body = client.get("/api/catalog/release-group/00000000-0000-4000-8000-000000000003", params={"artist": "The Band"}).json()
    rows = [(t["title"], t["media_item_id"] is not None) for t in body["tracks"]]
    assert rows == [("Opener", True), ("New One", False)]
    assert body["owned_count"] == 1


def test_want_an_album_counts_its_owned_songs(client, db_session):
    _library(db_session)
    _cache_tracklist(db_session, "00000000-0000-4000-8000-000000000003", "rel-second", ["Opener", "New One"])
    entry = client.post(
        "/api/want-list",
        json={"kind": "album", "artist": "The Band", "title": "Second", "release_group_mbid": "00000000-0000-4000-8000-000000000003", "artist_mbid": ARTIST},
    ).json()
    assert (entry["owned_count"], entry["total_count"], entry["owned"]) == (1, 2, False)
    again = client.post(
        "/api/want-list",
        json={"kind": "album", "artist": "the band", "title": "second", "release_group_mbid": "00000000-0000-4000-8000-000000000003"},
    ).json()
    assert again["id"] == entry["id"]


def test_want_a_song_matches_the_library(client, db_session):
    _library(db_session)
    entry = client.post(
        "/api/want-list", json={"kind": "track", "artist": "The Band", "title": "Opener (2011 Remaster)"}
    ).json()
    assert entry["owned"] is True and entry["media_item_id"] is not None
    assert client.post("/api/want-list/clear-owned").json() == {"removed": 1}
    assert db_session.query(WantListItem).count() == 0


def test_whole_artist_want_with_an_exclusion(client, db_session):
    _library(db_session)
    with patch("app.want_list.prefetch_tracklists_in_background") as prefetch:
        client.post("/api/want-list", json={"kind": "artist", "artist": "The Band", "artist_mbid": ARTIST})
    prefetch.assert_called_once_with(ARTIST)
    client.post(
        "/api/want-list",
        json={"kind": "album", "artist": "The Band", "title": "Live Somewhere", "release_group_mbid": "00000000-0000-4000-8000-000000000005", "excluded": True},
    )
    catalog = client.get("/api/catalog/artist", params={"mbid": ARTIST}).json()
    wanted = {g["title"]: g["wanted"] for g in catalog["release_groups"]}
    assert wanted == {"Debut": False, "Second": True, "Hit Song": False, "Live Somewhere": False}
    assert catalog["artist_wanted"] is True

    detail = client.get("/api/want-list/artist", params={"name": "The Band"}).json()
    assert detail["artist_entry"]["kind"] == "artist"
    assert [g["title"] for g in detail["missing_release_groups"]] == ["Second"]

    summaries = client.get("/api/want-list/artists").json()
    assert len(summaries) == 1
    assert summaries[0]["wants_artist"] is True and summaries[0]["in_library"] is True
    assert summaries[0]["representative_media_item_id"] is not None


def test_admins_see_every_users_wants(client, db_session, session_factory):
    _library(db_session)
    other = User(username="friend", password_hash=hash_password("pw-friend-1"), is_admin=False)
    db_session.add(other)
    db_session.commit()
    db_session.add(WantListItem(user_id=other.id, kind="artist", artist="Stranger", title="Stranger"))
    db_session.commit()
    assert client.get("/api/want-list/artists").json() == []
    everyone = client.get("/api/want-list/artists", params={"all_users": True}).json()
    assert [(row["artist"], row["users"]) for row in everyone] == [("Stranger", ["friend"])]

    friend = login_as(session_factory, client, "friend", "pw-friend-1")
    assert friend.get("/api/want-list/artists", params={"all_users": True}).json()[0]["users"] is None


def test_export_csv_and_json(client, db_session):
    _library(db_session)
    client.post("/api/want-list", json={"kind": "track", "artist": "Someone", "title": "Song", "recording_mbid": "00000000-0000-4000-8000-000000000007"})
    csv_body = client.get("/api/want-list/export", params={"format": "csv"})
    assert csv_body.headers["content-type"].startswith("text/csv")
    assert "attachment" in csv_body.headers["content-disposition"]
    header, row = csv_body.text.strip().splitlines()
    assert header.startswith("kind,artist,title")
    assert "00000000-0000-4000-8000-000000000007" in row
    data = client.get("/api/want-list/export", params={"format": "json"}).json()
    assert data[0]["recording_mbid"] == "00000000-0000-4000-8000-000000000007"


def test_search_marks_library_and_wanted(client, db_session):
    _library(db_session)
    client.post("/api/want-list", json={"kind": "album", "artist": "The Band", "title": "Second", "release_group_mbid": "00000000-0000-4000-8000-000000000003"})
    ok = lambda value: type("R", (), {"error": None, "value": value})()
    with patch("app.want_list.musicbrainz.search_artists", return_value=ok({"artists": [{"id": ARTIST, "name": "The Band", "score": 100}]})), patch(
        "app.want_list.musicbrainz.search_release_groups",
        return_value=ok({"release-groups": [
            {"id": "00000000-0000-4000-8000-000000000002", "title": "Debut", "primary-type": "Album", "artist-credit": [{"name": "The Band", "artist": {"id": ARTIST}}]},
            {"id": "00000000-0000-4000-8000-000000000003", "title": "Second", "primary-type": "Album", "first-release-date": "2004", "artist-credit": [{"name": "The Band"}]},
        ]}),
    ), patch(
        "app.want_list.musicbrainz.search_recordings",
        return_value=ok({"recordings": [
            {"id": "rec-Hit Song", "title": "Hit Song", "artist-credit": [{"name": "The Band"}]},
            {"id": "rec-live", "title": "Hit Song", "artist-credit": [{"name": "The Band"}]},
            {"id": "00000000-0000-4000-8000-000000000008", "title": "Brand New", "artist-credit": [{"name": "The Band"}], "releases": [{"title": "Second", "release-group": {"id": "00000000-0000-4000-8000-000000000003"}}]},
        ]}),
    ):
        body = client.get("/api/want-list/search", params={"q": "band"}).json()
    assert body["artists"][0]["in_library"] is True
    assert {a["title"]: (a["in_library"], a["wanted"]) for a in body["albums"]} == {
        "Debut": (True, False),
        "Second": (False, True),
    }
    assert body["albums"][1]["year"] == "2004"
    assert [(t["title"], t["in_library"]) for t in body["tracks"]] == [("Hit Song", True), ("Brand New", False)]
    assert body["tracks"][1]["release_group_mbid"] == "00000000-0000-4000-8000-000000000003"


def test_radio_bookmarks_are_song_entries(client, db_session):
    _library(db_session)
    entry = client.post(
        "/api/radio/want-list", json={"artist": "Somebody", "title": "On Air", "station_name": "KEXP"}
    ).json()
    row = db_session.get(WantListItem, entry["id"])
    assert (row.kind, row.station_name) == ("track", "KEXP")
    listed = client.get("/api/want-list/entries").json()
    assert listed[0]["station_name"] == "KEXP"


def test_wanted_artist_catalogs_refresh_weekly(db_session):
    _library(db_session)
    db_session.add(WantListItem(user_id=_user(db_session).id, kind="artist", artist="The Band", title="The Band", artist_mbid=ARTIST))
    db_session.commit()
    with patch("app.catalog.musicbrainz.browse_release_groups") as browse:
        assert refresh_wanted_catalogs(db_session) == 0
        browse.assert_not_called()
    row = db_session.get(ExternalCache, ("musicbrainz", "artist-release-groups", ARTIST))
    row.fetched_at = utcnow() - timedelta(days=8)
    db_session.commit()
    ok = type("R", (), {"error": None, "value": {"release-groups": [{"id": "00000000-0000-4000-8000-000000000006", "title": "New Album", "primary-type": "Album"}]}})()
    with patch("app.catalog.musicbrainz.browse_release_groups", return_value=ok):
        assert refresh_wanted_catalogs(db_session) == 1
    groups = json.loads(db_session.get(ExternalCache, ("musicbrainz", "artist-release-groups", ARTIST)).payload_json)
    assert [g["title"] for g in groups] == ["New Album"]


def test_an_artist_wanted_by_name_gets_their_mbid(client, db_session):
    search = type("R", (), {"error": None, "value": {"artists": [{"id": "mb-sim", "name": "Similar Act", "score": 100}]}})()
    with patch("app.metadata_info.musicbrainz.search_artists", return_value=search), patch(
        "app.want_list.prefetch_tracklists_in_background"
    ):
        entry = client.post("/api/want-list", json={"kind": "artist", "artist": "Similar Act"}).json()
    assert entry["artist_mbid"] == "mb-sim"


def test_stale_catalog_is_served_at_once_and_rechecked_in_the_background(client, db_session):
    _library(db_session)
    row = db_session.get(ExternalCache, ("musicbrainz", "artist-release-groups", ARTIST))
    row.fetched_at = row.fetched_at - timedelta(days=3)
    db_session.commit()

    queued = []
    with (
        patch("app.metadata_providers.musicbrainz.browse_release_groups", side_effect=AssertionError("fetched while serving")),
        patch("app.provider_queue.submit", side_effect=lambda key, job, **_: queued.append(key) or True),
    ):
        resp = client.get("/api/catalog/artist", params={"mbid": ARTIST, "name": "The Band"})
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["release_groups"]) == 4  # the saved list, not blocked on MusicBrainz
    assert body["refreshing"] is True
    assert queued == [f"refresh-artist:{ARTIST}"]


def test_fresh_catalog_is_not_rechecked(client, db_session):
    _library(db_session)  # just stored, so fresh
    with patch("app.provider_queue.submit", side_effect=AssertionError("queued")):
        resp = client.get("/api/catalog/artist", params={"mbid": ARTIST, "name": "The Band"})
    assert resp.status_code == 200
    assert resp.json()["refreshing"] is False


def test_refresh_button_rechecks_in_the_background_when_data_is_saved(client, db_session):
    _library(db_session)
    queued = []
    with (
        patch("app.metadata_providers.musicbrainz.browse_release_groups", side_effect=AssertionError("fetched while serving")),
        patch("app.provider_queue.submit", side_effect=lambda key, job, **_: queued.append(key) or True),
    ):
        resp = client.get("/api/catalog/artist", params={"mbid": ARTIST, "name": "The Band", "refresh": "true"})
    assert resp.status_code == 200
    assert resp.json()["refreshing"] is True
    assert queued == [f"refresh-artist:{ARTIST}"]


def test_refresh_status_reports_the_running_job(client):
    from app import provider_queue

    with patch.object(provider_queue, "_keys", {f"refresh-artist:{ARTIST}"}):
        assert client.get("/api/catalog/artist/refresh-status", params={"mbid": ARTIST}).json() == {"refreshing": True}
    assert client.get("/api/catalog/artist/refresh-status", params={"mbid": ARTIST}).json() == {"refreshing": False}


def test_saved_tracklist_is_reused_without_decoding_the_musicbrainz_payloads(db_session):
    from app.catalog import cached_tracklist, tracklist

    store(db_session, "musicbrainz", "release-group", "00000000-0000-4000-8000-000000000002", {"id": "00000000-0000-4000-8000-000000000002", "title": "Debut", "releases": [{"id": "rel-debut", "status": "Official", "date": "2001"}]})
    store(db_session, "musicbrainz", "release", "rel-debut", _release("rel-debut", ["Opener", "Hit Song"]))
    built = tracklist(db_session, "00000000-0000-4000-8000-000000000002")
    assert [t["title"] for t in built["tracks"]] == ["Opener", "Hit Song"]
    # The big payloads can vanish; the compact saved listing still answers.
    db_session.query(ExternalCache).filter(ExternalCache.provider == "musicbrainz").delete()
    db_session.commit()
    again = cached_tracklist(db_session, "00000000-0000-4000-8000-000000000002")
    assert [t["title"] for t in again["tracks"]] == ["Opener", "Hit Song"]


def test_a_refused_recheck_shows_the_saved_catalog_and_is_not_refreshing(client, db_session):
    """The provider queue has no room: the saved list is served and the page
    is not told to poll for a re-check that is not coming."""
    _library(db_session)
    row = db_session.get(ExternalCache, ("musicbrainz", "artist-release-groups", ARTIST))
    row.fetched_at = row.fetched_at - timedelta(days=3)
    db_session.commit()
    asked = []
    with patch(
        "app.provider_queue.submit",
        side_effect=lambda key, job, *, user_id=None: asked.append((key, user_id)) or False,
    ):
        resp = client.get("/api/catalog/artist", params={"mbid": ARTIST, "name": "The Band"})
    assert resp.status_code == 200
    assert len(resp.json()["release_groups"]) == 4
    assert resp.json()["refreshing"] is False
    assert asked == [(f"refresh-artist:{ARTIST}", _user(db_session).id)]


def test_the_scheduled_catalog_refresh_yields_between_artists(db_session):
    _library(db_session)
    other = "00000000-0000-4000-8000-0000000000aa"
    for mbid in (ARTIST, other):
        db_session.add(WantListItem(user_id=_user(db_session).id, kind="artist", artist=mbid, title=mbid, artist_mbid=mbid))
    db_session.commit()
    row = db_session.get(ExternalCache, ("musicbrainz", "artist-release-groups", ARTIST))
    row.fetched_at = utcnow() - timedelta(days=8)
    db_session.commit()
    ok = type("R", (), {"error": None, "value": {"release-groups": []}})()
    with (
        patch("app.catalog.musicbrainz.browse_release_groups", return_value=ok),
        patch("app.want_list.yield_to_user_jobs") as yielded,
    ):
        assert refresh_wanted_catalogs(db_session) == 2
    assert yielded.call_count == 2


def _add_tracks(db_session, root, titles: list[str]) -> None:
    for index, title in enumerate(titles):
        db_session.add(
            MediaItem(library_root_id=root.id, relative_path=f"extra-{index}.mp3", title=title, artist="The Band", album="Extras")
        )
    db_session.commit()
    rebuild_all_credits(db_session)
    rebuild_albums(db_session)


def test_a_live_remaster_in_the_library_does_not_own_the_studio_song(client, db_session):
    """"Anthem (Live at Wembley; 2011 Remaster)" does not count as "Anthem",
    so Clear owned does not delete the entry."""
    root = _library(db_session)
    _add_tracks(db_session, root, ["Anthem (Live at Wembley; 2011 Remaster)", "Ballad - Live at Wembley / 2011 Remaster", "Chant (Clean Bandit Remix)"])
    for title in ("Anthem", "Ballad", "Chant"):
        entry = client.post("/api/want-list", json={"kind": "track", "artist": "The Band", "title": title}).json()
        assert entry["owned"] is False and entry["media_item_id"] is None, title
    assert client.post("/api/want-list/clear-owned").json() == {"removed": 0}
    assert db_session.query(WantListItem).count() == 3
    # An album's tracklist counts the same way.
    _cache_tracklist(db_session, "00000000-0000-4000-8000-000000000003", "rel-second", ["Anthem", "Opener (2011 Remaster)"])
    album = client.post(
        "/api/want-list",
        json={"kind": "album", "artist": "The Band", "title": "Second", "release_group_mbid": "00000000-0000-4000-8000-000000000003"},
    ).json()
    assert (album["owned_count"], album["total_count"]) == (1, 2)
    # The live take itself is still found under its plain live name.
    live = client.post(
        "/api/want-list", json={"kind": "track", "artist": "The Band", "title": "Anthem (Live at Wembley)"}
    ).json()
    assert live["owned"] is False  # a different key: "2011 Remaster" stays with the live note


def test_a_punctuation_title_only_matches_the_same_title(client, db_session):
    """"÷" and "+" do not both normalise to "", so owning one does not own
    the other."""
    root = _library(db_session)
    _add_tracks(db_session, root, ["+", "!!!"])
    wanted = client.post("/api/want-list", json={"kind": "track", "artist": "The Band", "title": "÷"}).json()
    assert wanted["owned"] is False and wanted["media_item_id"] is None
    owned = client.post("/api/want-list", json={"kind": "track", "artist": "The Band", "title": "+"}).json()
    assert owned["id"] != wanted["id"]  # not folded into the "÷" entry either
    assert owned["owned"] is True
    assert db_session.get(MediaItem, owned["media_item_id"]).title == "+"
    assert client.post("/api/want-list/clear-owned").json() == {"removed": 1}
    assert [e.title for e in db_session.query(WantListItem)] == ["÷"]


def _art(db_session, entity_type: str, key: str) -> None:
    db_session.add(CustomArt(entity_type=entity_type, entity_key=key, path=f"{key}.jpg", thumb_path=f"{key}-t.jpg"))
    db_session.commit()


def test_an_artist_with_no_photo_shows_one_of_their_covers(client, db_session):
    # Not in the library, so no track's art: the newest album with a stored
    # cover stands in for the missing photo (singles only after albums).
    store(db_session, "musicbrainz", "artist-release-groups", ARTIST, GROUPS)
    _art(db_session, "catalog_album", GROUPS[0]["mbid"])
    _art(db_session, "catalog_album", GROUPS[2]["mbid"])
    with patch("app.want_list.prefetch_tracklists_in_background"):
        client.post("/api/want-list", json={"kind": "artist", "artist": "The Band", "artist_mbid": ARTIST})
    summary = client.get("/api/want-list/artists").json()[0]
    assert summary["representative_media_item_id"] is None
    assert summary["cover_release_group_mbid"] == GROUPS[0]["mbid"]

    # A wanted album's own cover comes first.
    client.post(
        "/api/want-list",
        json={"kind": "album", "artist": "The Band", "title": "Hit Song", "artist_mbid": ARTIST, "release_group_mbid": GROUPS[2]["mbid"]},
    )
    assert client.get("/api/want-list/artists").json()[0]["cover_release_group_mbid"] == GROUPS[2]["mbid"]
    detail = client.get("/api/want-list/artist", params={"name": "The Band", "mbid": ARTIST}).json()
    assert detail["cover_release_group_mbid"] == GROUPS[2]["mbid"]

    # Once the artist has a photo, no stand-in.
    _art(db_session, "catalog_artist", ARTIST)
    assert client.get("/api/want-list/artists").json()[0]["cover_release_group_mbid"] is None
    catalog = client.get("/api/catalog/artist", params={"mbid": ARTIST}).json()
    assert catalog["cover_release_group_mbid"] is None
