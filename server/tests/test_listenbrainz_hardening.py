"""Explore previews never block a page, IDs sent to
ListenBrainz are well-formed, a revoked token asks for relinking, mixes
time-box their live look-ups, and no Deezer data survives in backups."""

from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from unittest.mock import patch

import httpx

from app import scrobbling
from app.discovery import explore
from app.metadata_providers import deezer, listenbrainz
from app.metadata_providers._throttle import Throttle
from app.models import (
    CustomArt,
    DiscoveryState,
    LibraryRoot,
    MediaItem,
    MediaKind,
    ScrobbleQueueItem,
    User,
    UserIntegration,
    utcnow,
)
from app.secrets import decrypt_credential, encrypt_credential

A1 = "00000004-0000-4000-8000-000000000004"
A2 = "00000005-0000-4000-8000-000000000005"
REC = "00000001-0000-4000-8000-000000000001"


def _json(status: int, payload, headers: dict | None = None) -> httpx.Response:
    return httpx.Response(status, json=payload, headers=headers, request=httpx.Request("GET", "https://x"))


def _user(db_session) -> User:
    return db_session.query(User).one()


# --- Explore: the Home page answers from memory -------------------------------------


def test_discovery_state_never_asks_deezer_and_warms_missing_previews_in_the_background(client, db_session):
    user = _user(db_session)
    db_session.add(DiscoveryState(user_id=user.id, explore_results_json=[{"artist": "Known"}, {"artist": "Missing"}]))
    db_session.commit()
    deezer._remember("Known", {"artist": "Known (Deezer)", "title": "Hit", "artwork_url": None,
                                "preview_url": "https://cdnt-preview.dzcdn.net/k", "view_url": None})
    jobs: list = []
    with patch("app.discovery.explore.provider_queue.submit", side_effect=lambda key, job, **_: jobs.append(job) or True), \
         patch("app.metadata_providers.deezer.search_artist_track") as search:
        body = client.get("/api/library/discovery/state").json()
        again = client.get("/api/library/discovery/state").json()
    search.assert_not_called()  # the page itself never waits on Deezer
    # The name shown is the stored (ListenBrainz) one, not Deezer's spelling.
    assert [r["artist"] for r in body["explore_results"]] == ["Known"]
    assert body["explore_pending"] is True and again["explore_pending"] is True
    assert len(jobs) == 1  # one job, however often the page asks meanwhile
    track = {"artist": "Missing", "title": "Song", "artwork_url": None,
             "preview_url": "https://cdnt-preview.dzcdn.net/m", "view_url": None}
    with patch("app.metadata_providers.deezer.search_artist_track", return_value=track):
        jobs[0]()
    body = client.get("/api/library/discovery/state").json()
    assert [r["artist"] for r in body["explore_results"]] == ["Known", "Missing"]
    assert body["explore_pending"] is False


def test_warming_previews_stops_after_failures_and_remembers_them(monkeypatch):
    monkeypatch.setattr(deezer, "_throttle", Throttle(0.0))
    with patch("app.metadata_providers.deezer.httpx.get", side_effect=httpx.ConnectError("down")) as get:
        explore.warm_previews([f"Artist {n}" for n in range(10)])
    assert get.call_count == explore.EXPLORE_FAILURE_LIMIT
    assert deezer.recently_failed("Artist 0")
    # A recently failed artist is not looked up again by the page either.
    assert explore.explore_previews([{"artist": "Artist 0"}], user_id=1) == ([], False)


def test_explore_stores_only_listenbrainz_names(db_session):
    from app import discovery

    user = _user(db_session)
    with (
        patch("app.discovery.explore._top_played_artists", return_value=["Seed"]),
        patch("app.discovery.explore._similar_artist_names", return_value={"Sigur Ros"}),
        patch(
            "app.discovery.explore.deezer.search_artist_track",
            return_value={"artist": "Sigur Rós", "title": "Hoppípolla", "preview_url": "https://p", "view_url": None,
                          "artwork_url": None},
        ),
    ):
        result = discovery.explore_new_artists(db_session, user)
    assert result["results"][0]["artist"] == "Sigur Ros"
    assert db_session.get(DiscoveryState, user.id).explore_results_json == [{"artist": "Sigur Ros"}]


# --- MusicBrainz IDs sent to ListenBrainz ----------------------------------------------


def test_only_well_formed_mbids_are_sent():
    assert listenbrainz.valid_mbids(f"{A1}/{A2}") == [A1, A2]
    assert listenbrainz.valid_mbids(f"{A1.upper()}; {A1}") == [A1]
    assert listenbrainz.valid_mbids("not an id") == [] and listenbrainz.valid_mbids(None) == []
    assert listenbrainz.first_mbid(f" {A2} / {A1}") == A2


def test_a_batch_refused_with_400_is_a_failure_not_a_miss(monkeypatch):
    monkeypatch.setattr(listenbrainz, "_throttle", Throttle(0.0))
    with patch("app.metadata_providers.listenbrainz._client.request", return_value=_json(400, {"error": "bad"})):
        assert listenbrainz.artist_tags([A1, A2]) is None
        assert listenbrainz.artist_popularity([A1]) is None
        # A single-ID look-up: ListenBrainz does not know it.
        assert listenbrainz.similar_artists(A1) == []


def test_the_tags_task_sends_the_first_artist_of_a_multi_value_tag(db_session, session_factory, monkeypatch):
    from app import library_tasks
    from app.models import LibraryTask

    monkeypatch.setattr(library_tasks, "SessionLocal", session_factory)
    root = LibraryRoot(path="/mv", display_name="mv")
    db_session.add(root)
    db_session.commit()
    good = MediaItem(library_root_id=root.id, kind=MediaKind.track, relative_path="a.mp3", title="A",
                     artist="X", musicbrainz_artist_mbid=f"{A1}/{A2}")
    junk = MediaItem(library_root_id=root.id, kind=MediaKind.track, relative_path="b.mp3", title="B",
                     artist="Y", musicbrainz_artist_mbid="garbage")
    task = LibraryTask(library_root_id=root.id, kind="lb_tags", status="running")
    db_session.add_all([good, junk, task])
    db_session.commit()
    with patch("app.library_tasks.listenbrainz.artist_tags", return_value={A1: [("shoegaze", 4)]}) as ask:
        library_tasks._run_listenbrainz_tags_task(root.id, task.id)
    ask.assert_called_once_with([A1])
    db_session.expire_all()
    assert db_session.get(LibraryTask, task.id).matched == 1
    assert {t.name for t in db_session.get(MediaItem, good.id).musicbrainz_tags} == {"shoegaze"}


# --- a token ListenBrainz stopped accepting ---------------------------------------------


def _track(db_session) -> MediaItem:
    root = LibraryRoot(path="/s", display_name="s")
    db_session.add(root)
    db_session.commit()
    item = MediaItem(library_root_id=root.id, kind=MediaKind.track, relative_path="t.mp3", title="T", artist="A",
                     musicbrainz_recording_mbid=REC, musicbrainz_artist_mbid=A1)
    db_session.add(item)
    db_session.commit()
    return item


def _link(db_session, user: User) -> None:
    db_session.add(UserIntegration(user_id=user.id, provider="listenbrainz", external_username="me",
                                   credential=encrypt_credential(json.dumps({"token": "T"}))))
    db_session.commit()


def test_a_revoked_token_asks_for_relinking_and_keeps_the_listen(db_session):
    user = _user(db_session)
    item = _track(db_session)
    _link(db_session, user)
    with patch("app.scrobbling.listenbrainz.submit_listen", return_value="unauthorized"):
        scrobbling.scrobble_track(db_session, user, item, utcnow())
    status = scrobbling.list_integrations(db_session, user)[0]
    assert (status["linked"], status["needs_reconnect"]) == (False, True)
    queued = db_session.query(ScrobbleQueueItem).one()
    assert (queued.recording_mbid, queued.artist_mbids) == (REC, A1)
    # Nothing more is sent until the user links again...
    with patch("app.scrobbling.listenbrainz.submit_listen") as submit:
        scrobbling.scrobble_track(db_session, user, item, utcnow())
        scrobbling.retry_queued_scrobbles(db_session, now=utcnow() + timedelta(hours=1))
    submit.assert_not_called()
    assert db_session.query(ScrobbleQueueItem).count() == 1
    # ...which clears the mark; the queued listen then goes out with its IDs.
    with patch("app.scrobbling.listenbrainz.validate_token", return_value="me"):
        assert scrobbling.link_listenbrainz(db_session, user, "NEW") is None
    row = db_session.get(UserIntegration, (user.id, "listenbrainz"))
    assert json.loads(decrypt_credential(row.credential)) == {"token": "NEW"}
    with patch("app.scrobbling.listenbrainz.submit_listen", return_value="accepted") as submit:
        assert scrobbling.retry_queued_scrobbles(db_session, now=utcnow() + timedelta(hours=1)) == 1
    assert submit.call_args.kwargs["recording_mbid"] == REC
    assert submit.call_args.kwargs["artist_mbids"] == [A1]


def test_a_queued_entry_refused_for_the_token_waits_for_relinking(db_session):
    user = _user(db_session)
    _link(db_session, user)
    now = utcnow()
    db_session.add(ScrobbleQueueItem(user_id=user.id, artist="A", title="T", started_at=now, next_attempt_at=now))
    db_session.commit()
    with patch("app.scrobbling.listenbrainz.submit_listen", return_value="unauthorized"):
        assert scrobbling.retry_queued_scrobbles(db_session, now=now) == 0
    assert db_session.query(ScrobbleQueueItem).count() == 1
    assert scrobbling.list_integrations(db_session, user)[0]["needs_reconnect"] is True


def test_validate_token_checks_the_status_before_the_body(monkeypatch):
    monkeypatch.setattr(listenbrainz, "_throttle", Throttle(0.0))
    with patch("app.metadata_providers.listenbrainz._client.get",
               return_value=httpx.Response(401, text="<html>no</html>", request=httpx.Request("GET", "https://x"))):
        assert listenbrainz.validate_token("t") is False
    with patch("app.metadata_providers.listenbrainz._client.get", return_value=_json(429, {}, {"X-RateLimit-Reset-In": "4"})):
        assert listenbrainz.validate_token("t") is None
    assert listenbrainz._throttle.paused_for() > 3


# --- mixes time-box their live look-ups ----------------------------------------------


def test_similar_tracks_stop_asking_live_at_the_budget(db_session, monkeypatch):
    from app import listenbrainz_data

    root = LibraryRoot(path="/b", display_name="b")
    db_session.add(root)
    db_session.commit()
    seeds = [
        MediaItem(library_root_id=root.id, kind=MediaKind.track, relative_path=f"{n}.mp3", title=f"S{n}", artist="A",
                  musicbrainz_recording_mbid=f"0000000{n}-0000-4000-8000-00000000000{n}")
        for n in range(1, 4)
    ]
    db_session.add_all(seeds)
    db_session.commit()
    clock = iter([0.0, 1.0, 100.0, 100.0, 100.0])  # the budget runs out after the first seed
    monkeypatch.setattr(listenbrainz_data.time, "monotonic", lambda: next(clock))
    with patch("app.listenbrainz_data.listenbrainz.similar_recordings", return_value=[]) as ask, \
         patch("app.listenbrainz_data.prefetch_similar_in_background") as warm:
        listenbrainz_data.similar_owned_weights(db_session, [root.id], seeds, True)
    assert ask.call_count == 1
    assert [call.args[0] for call in warm.call_args_list] == [seeds[1].id, seeds[2].id]


# --- photo credit, licences and links -------------------------------------------------


def test_a_musicbrainz_outage_is_not_cached_as_no_photo(db_session):
    from app.metadata_info import artist_image
    from app.models import ExternalCache

    with patch("app.metadata_info.musicbrainz.lookup_artist", return_value=type("R", (), {"error": "down", "value": None})()):
        assert artist_image(db_session, A1) is None
    assert db_session.get(ExternalCache, ("wikimedia", "artist-image", A1)) is None


def test_only_free_licences_and_https_links_are_kept(monkeypatch):
    from app.metadata_providers import wikipedia

    monkeypatch.setattr(wikipedia, "_throttle", Throttle(0.0))

    def answer(licence: str, page: str = "https://commons.wikimedia.org/wiki/File:X.jpg"):
        return _json(200, {"query": {"pages": {"1": {"imageinfo": [{
            "url": "https://upload.wikimedia.org/x.jpg", "descriptionurl": page,
            "extmetadata": {"LicenseShortName": {"value": licence}, "LicenseUrl": {"value": "intent://evil"}},
        }]}}}})

    for licence, kept in (("CC BY 4.0", True), ("CC BY-SA 3.0", True), ("CC0", True), ("Public domain", True),
                          ("CC BY-NC 4.0", False), ("CC BY-ND 2.0", False), ("CC BY-NC-SA 4.0", False)):
        with patch("app.metadata_providers.wikipedia.httpx.get", return_value=answer(licence)):
            found = wikipedia.commons_image("X.jpg")
        assert (getattr(found, "data", None) is not None) is kept, licence
    with patch("app.metadata_providers.wikipedia.httpx.get", return_value=answer("CC BY 4.0", "javascript:alert(1)")):
        data = wikipedia.commons_image("X.jpg").data  # type: ignore[union-attr]
    assert data["page_url"] is None and data["license_url"] is None


def test_listenbrainz_outages_are_logged_but_not_on_every_request(monkeypatch, caplog):
    monkeypatch.setattr(listenbrainz, "_throttle", Throttle(0.0))
    monkeypatch.setattr(listenbrainz, "_last_failure_log", [0.0])
    with caplog.at_level("WARNING", logger="app.metadata_providers.listenbrainz"), \
         patch("app.metadata_providers.listenbrainz._client.request", return_value=_json(502, {})):
        for _ in range(5):
            assert listenbrainz.similar_artists(A1) is None
    assert caplog.text.count("ListenBrainz did not answer") == 1


# --- Deezer data in backups, art saved before sources were recorded ---------------------


def test_backups_are_scrubbed_of_lastfm_and_deezer_data(tmp_path, monkeypatch):
    from app import migrations

    backups = tmp_path / "backups"
    backups.mkdir()
    old = backups / "atriolyric-v40-20261124T000000.000000Z.db"
    conn = sqlite3.connect(old)
    conn.execute("CREATE TABLE external_cache (provider TEXT, kind TEXT, key TEXT, payload_json TEXT)")
    conn.executemany("INSERT INTO external_cache VALUES (?, ?, ?, ?)",
                     [("deezer", "artist-pictures", "x", "[]"), ("lastfm", "artist.getInfo", "x", "{}"),
                      ("musicbrainz", "artist", "m", "{}")])
    conn.execute("CREATE TABLE discovery_state (user_id INTEGER, explore_results_json TEXT)")
    conn.execute("INSERT INTO discovery_state VALUES (1, '[{\"preview_url\": \"https://dzcdn\"}]')")
    conn.commit()
    conn.close()
    database = tmp_path / "atriolyric.db"
    sqlite3.connect(database).close()
    engine = type("E", (), {"url": type("U", (), {"database": str(database)})(), "connect": lambda self: _NoVacuum()})()
    monkeypatch.setattr(migrations, "engine", engine)
    migrations.compact_after_lastfm_and_deezer_purge()
    conn = sqlite3.connect(old)
    assert [r[0] for r in conn.execute("SELECT provider FROM external_cache")] == ["musicbrainz"]
    assert conn.execute("SELECT explore_results_json FROM discovery_state").fetchone()[0] == "[]"
    conn.close()


class _NoVacuum:
    """Stands in for the engine connection the live database is vacuumed on."""

    def execution_options(self, **_kw):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def exec_driver_sql(self, sql):
        assert sql == "VACUUM"


def test_admin_can_list_and_remove_art_saved_before_sources_were_recorded(client, db_session, tmp_path, monkeypatch):
    from app.config import settings
    from app.custom_art import store_custom_art
    from tests.test_custom_art import _png

    monkeypatch.setattr(settings, "database_path", tmp_path / "art.db")
    old_pick = store_custom_art(db_session, "artist", "Old", _png())  # source unknown
    upload = store_custom_art(db_session, "album", "A\x1fB", _png(), source="upload")
    playlist = store_custom_art(db_session, "playlist", "1", _png())  # playlists only ever had uploads
    old_file = settings.custom_art_dir / old_pick.path
    assert client.get("/api/admin/custom-art/unrecorded").json() == {"count": 1}
    assert client.post("/api/admin/custom-art/unrecorded/remove").json() == {"removed": 1}
    remaining = {(row.entity_type, row.entity_key) for row in db_session.query(CustomArt)}
    assert remaining == {(upload.entity_type, upload.entity_key), (playlist.entity_type, playlist.entity_key)}
    assert not old_file.exists()
    assert client.get("/api/admin/custom-art/unrecorded").json() == {"count": 0}
