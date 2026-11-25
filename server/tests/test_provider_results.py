"""Every provider tells "found", "definitively nothing" and
"failed" apart, callers cache the first two and never the third, a
Retry-After pauses the provider, and bulk tasks stop on an outage."""

from __future__ import annotations

import json
from unittest.mock import patch

import httpx
import pytest

from app import library_tasks
from app.external_cache import cached
from app.metadata_providers import (
    coverart,
    deezer,
    listenbrainz,
    lyrics,
    musicbrainz,
    wikipedia,
)
from app.metadata_providers._throttle import Throttle
from app.metadata_providers.result import (
    MAX_RETRY_AFTER,
    NOT_FOUND,
    Failed,
    Found,
    cache_value,
    failed,
    recording_failures,
    retry_after_seconds,
)
from app.models import (
    ExternalCache,
    LibraryRoot,
    LibraryTask,
    MediaItem,
    MediaKind,
    User,
    UserIntegration,
)


@pytest.fixture(autouse=True)
def _fresh_throttles(monkeypatch):
    """Each test's pauses stay in that test."""
    for module, interval in (
        (listenbrainz, 0.0), (deezer, 0.0), (coverart, 0.0), (lyrics, 0.0), (wikipedia, 0.0), (musicbrainz, 0.0)
    ):
        monkeypatch.setattr(module, "_throttle", Throttle(interval))
    monkeypatch.setattr(coverart, "_front_image_cache", {})


def _json(status: int, payload, headers: dict | None = None) -> httpx.Response:
    return httpx.Response(status, json=payload, headers=headers, request=httpx.Request("GET", "https://x"))


def _paused(module) -> float:
    return module._throttle.paused_for()


# --- the shared type ---------------------------------------------------------


def test_cache_value_and_retry_after_parsing():
    assert cache_value(Found([1]), []) == [1]
    assert cache_value(NOT_FOUND, []) == []
    assert cache_value(Failed("x"), []) is None
    assert retry_after_seconds(_json(429, {}, {"Retry-After": "7"})) == 7
    assert retry_after_seconds(_json(429, {}, {"Retry-After": "99999"})) == MAX_RETRY_AFTER
    assert retry_after_seconds(_json(429, {}, {"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"})) == 0
    assert retry_after_seconds(_json(429, {}, {"Retry-After": "soon"})) is None
    assert retry_after_seconds(_json(429, {})) is None


def test_failures_are_counted_per_context():
    with recording_failures() as outer:
        failed("one")
        with recording_failures() as inner:
            failed("two")
        assert inner == ["two"]
    assert outer == ["one"]
    failed("nobody listening")  # no context: just a Failed


def test_a_pause_makes_page_requests_give_up_and_waits_in_bulk():
    throttle = Throttle(0.0)
    throttle.pause(60)
    assert throttle.paused_for() > 59
    assert throttle.wait() is False  # a page request: more than INTERACTIVE_MAX_WAIT away


# --- ListenBrainz ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (httpx.ConnectTimeout("slow"), None),
        (_json(429, {"code": 429}, {"X-RateLimit-Reset-In": "7"}), None),
        (_json(503, {}), None),
        (_json(200, []), {}),
        (_json(200, [{"artist_mbid": "m", "total_listen_count": 9, "total_user_count": 3}]), {"playcount": 9, "listeners": 3}),
    ],
)
def test_listenbrainz_results_and_caching(db_session, response, expected):
    from app.listenbrainz_data import artist_popularity

    with patch(
        "app.metadata_providers.listenbrainz._client.request",
        **({"side_effect": response} if isinstance(response, Exception) else {"return_value": response}),
    ):
        got = artist_popularity(db_session, "m")
    row = db_session.get(ExternalCache, ("listenbrainz", "artist-popularity", "m"))
    if expected is None:
        assert got is None and row is None  # a failure is never cached
    elif expected == {}:
        assert got == {} and json.loads(row.payload_json) == {}  # a definitive miss is
    else:
        assert got == expected and row is not None


def test_listenbrainz_rate_limit_pauses_the_provider():
    with patch(
        "app.metadata_providers.listenbrainz._client.request",
        return_value=_json(429, {}, {"X-RateLimit-Reset-In": "7"}),
    ):
        with recording_failures() as failures:
            assert listenbrainz.similar_artists("m") is None
    assert 6 < _paused(listenbrainz) <= 7 and failures


def test_listenbrainz_waits_out_a_used_up_window():
    with patch(
        "app.metadata_providers.listenbrainz._client.request",
        return_value=_json(200, [], {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset-In": "5"}),
    ):
        assert listenbrainz.similar_recordings("m") == []
    assert 4 < _paused(listenbrainz) <= 5


def test_listenbrainz_other_failures_and_misses():
    with patch("app.metadata_providers.listenbrainz._client.request", return_value=httpx.Response(502, text="<html>")):
        assert listenbrainz.top_recordings_for_artist("m", "tok") is None
    with patch("app.metadata_providers.listenbrainz._client.request", return_value=httpx.Response(200, text="<html>")):
        assert listenbrainz.artist_tags(["m"]) is None
    with patch("app.metadata_providers.listenbrainz._client.request", return_value=_json(404, {})):
        assert listenbrainz.top_recordings_for_artist("m", "tok") == []
    with patch("app.metadata_providers.listenbrainz._client.request", return_value=_json(400, {"error": "bad"})):
        assert listenbrainz.similar_artists("not-an-mbid") == []


def test_unknown_track_similar_list_is_cached_not_asked_every_play(db_session):
    from app.listenbrainz_data import similar_tracks

    root = LibraryRoot(path="/m", display_name="m")
    db_session.add(root)
    db_session.commit()
    item = MediaItem(
        library_root_id=root.id, relative_path="a.mp3", title="Song", artist="A",
        musicbrainz_recording_mbid="00000001-0000-4000-8000-000000000001",
    )
    db_session.add(item)
    db_session.commit()
    with patch("app.metadata_providers.listenbrainz._client.request", return_value=_json(200, [])) as get:
        assert similar_tracks(db_session, item) == []
        assert similar_tracks(db_session, item) == []
    assert get.call_count == 1


def test_artist_tags_mark_tracks_asked_even_when_the_artist_has_none(db_session):
    from app.enrichment import apply_listenbrainz_tags

    root = LibraryRoot(path="/m", display_name="m")
    db_session.add(root)
    db_session.commit()
    item = MediaItem(library_root_id=root.id, relative_path="a.mp3", title="Song", artist="A", kind=MediaKind.track)
    db_session.add(item)
    db_session.commit()
    apply_listenbrainz_tags(db_session, item, [])
    assert item.online_tags_fetched_at is not None


# --- Deezer --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (httpx.ConnectTimeout("slow"), "failed"),
        (_json(429, {}, {"Retry-After": "3"}), "failed"),
        # Quota errors arrive as 200 with an error body.
        (_json(200, {"error": {"type": "Exception", "message": "Quota limit exceeded", "code": 4}}), "failed"),
        (_json(200, {"data": [], "total": 0}), None),
        (
            _json(200, {"data": [{"title": "Song", "preview": "https://cdnt-preview.dzcdn.net/p.mp3", "artist": {"name": "Queen"}}]}),
            "Song",
        ),
    ],
)
def test_deezer_previews_are_kept_in_memory_only(db_session, response, expected):
    with patch(
        "app.metadata_providers.deezer.httpx.get",
        **({"side_effect": response} if isinstance(response, Exception) else {"return_value": response}),
    ) as get, recording_failures() as failures:
        got = deezer.artist_preview("Queen")
        again = deezer.artist_preview("Queen")
    # Nothing Deezer says is ever written to the database (its terms).
    assert db_session.query(ExternalCache).filter_by(provider="deezer").count() == 0
    if expected == "failed":
        # A failure is remembered briefly: the second ask is not sent, and
        # still counts as a failure for the caller.
        assert got is None and again is None and len(failures) == 2
        assert get.call_count == 1
        assert deezer.recently_failed("Queen") and deezer.peek_preview("Queen") == (False, None)
    else:
        assert get.call_count == 1  # a found preview and "no preview" are remembered (in memory)
        assert (got or {}).get("title") == expected and again == got


def test_deezer_quota_error_pauses_the_provider():
    quota = _json(200, {"error": {"message": "Quota limit exceeded", "code": 4}})
    with patch("app.metadata_providers.deezer.httpx.get", return_value=quota):
        assert deezer.search_artist_track("A") is None
        assert _paused(deezer) > 4


def test_deezer_other_failures():
    for response in (httpx.Response(500), httpx.Response(200, text="<html>"), _json(200, {"nodata": 1})):
        with patch("app.metadata_providers.deezer.httpx.get", return_value=response), recording_failures() as failures:
            assert deezer.search_artist_track("A") is None
        assert failures


def test_deezer_memory_stays_bounded(monkeypatch):
    monkeypatch.setattr(deezer, "MEMORY_MAX_ENTRIES", 4)
    monkeypatch.setattr(deezer, "search_artist_track", lambda name: None)
    for index in range(10):
        deezer.artist_preview(f"Artist {index}")
    assert len(deezer._memory) <= 4


# --- Cover Art Archive ---------------------------------------------------------


@pytest.mark.parametrize(
    ("response", "kind"),
    [
        (httpx.ConnectTimeout("slow"), Failed),
        (_json(503, {}, {"Retry-After": "4"}), Failed),
        (httpx.Response(404, text="<html>Not Found</html>"), type(NOT_FOUND)),
        (_json(200, {"images": [{"front": True, "image": "https://coverartarchive.org/f.jpg"}]}), Found),
    ],
)
def test_cover_art_archive_results(response, kind):
    with patch(
        "app.metadata_providers.coverart._client.get",
        **({"side_effect": response} if isinstance(response, Exception) else {"return_value": response}),
    ):
        result = coverart.front_image("rel-1")
    assert isinstance(result, kind)
    # Only answers are remembered for the process, never a failure.
    assert ("rel-1" in coverart._front_image_cache) is (kind is not Failed)


def test_cover_art_falls_back_to_the_release_group_only_on_a_definite_miss():
    missing = httpx.Response(404, text="<html>Not Found</html>")
    group = _json(200, {"images": [{"front": True, "image": "https://coverartarchive.org/rg.jpg"}]})
    with patch.object(coverart._client, "get", side_effect=[missing, group]) as get:
        result = coverart.front_image("rel-x", "rg-x")
    assert isinstance(result, Found) and result.data == "https://coverartarchive.org/rg.jpg"
    assert [call.args[0] for call in get.call_args_list] == [
        "https://coverartarchive.org/release/rel-x",
        "https://coverartarchive.org/release-group/rg-x",
    ]
    # A failed release lookup is not a miss: the group is not asked.
    with patch.object(coverart._client, "get", side_effect=[httpx.ConnectTimeout("slow")]) as get:
        assert isinstance(coverart.front_image("rel-y", "rg-y"), Failed)
    assert get.call_count == 1
    # Without a release group a miss stays a miss.
    with patch.object(coverart._client, "get", return_value=missing):
        assert coverart.front_image("rel-z") is NOT_FOUND


def test_cover_art_archive_rate_limit_pauses_and_lists_stay_three_way():
    with patch("app.metadata_providers.coverart._client.get", return_value=_json(429, {}, {"Retry-After": "4"})):
        assert coverart.list_release_images("rel-2") is None
    assert _paused(coverart) > 3
    coverart._throttle = Throttle(0.0)
    with patch("app.metadata_providers.coverart._client.get", return_value=httpx.Response(404)):
        assert coverart.list_release_images("rel-2") == []
    with patch("app.metadata_providers.coverart._client.get", return_value=httpx.Response(500)):
        assert coverart.list_release_images("rel-2") is None
    with patch("app.metadata_providers.coverart._client.get", return_value=httpx.Response(200, text="x")):
        assert isinstance(coverart.front_image("rel-3"), Failed)


def test_cover_art_task_item_failure_is_not_marked_as_no_art(db_session):
    from app.enrichment import fetch_cover_art_for_item

    root = LibraryRoot(path="/m", display_name="m")
    db_session.add(root)
    db_session.commit()
    item = MediaItem(
        library_root_id=root.id, relative_path="a.mp3", title="S",
        musicbrainz_metadata_json={"releases": [{"id": "rel-9"}]}, cover_art_url="https://old",
    )
    db_session.add(item)
    db_session.commit()
    with patch("app.enrichment.coverart.front_image", return_value=Failed("Cover Art Archive HTTP 503")):
        result = fetch_cover_art_for_item(db_session, item)
    assert result["failed"] is True and item.cover_art_url == "https://old"


# --- lrclib ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (httpx.ConnectTimeout("slow"), None),
        (_json(429, {}, {"Retry-After": "5"}), None),
        (_json(200, []), {}),
        (_json(200, [{"plainLyrics": "words", "syncedLyrics": None}]), {"plain": "words", "synced": None}),
    ],
)
def test_lrclib_results(response, expected):
    with patch(
        "app.metadata_providers.lyrics.httpx.get",
        **({"side_effect": response} if isinstance(response, Exception) else {"return_value": response}),
    ):
        assert lyrics.fetch_lrclib_lyrics("Artist", "Title") == expected


def test_lrclib_rate_limit_pauses_it():
    with patch("app.metadata_providers.lyrics.httpx.get", return_value=_json(429, {}, {"Retry-After": "60"})):
        lyrics.fetch_lrclib_lyrics("Artist", "Title")
    assert _paused(lyrics) > 59
    with patch("app.metadata_providers.lyrics.httpx.get") as get:
        assert lyrics.fetch_lrclib_lyrics("Artist", "Title") is None  # still paused: not asked
    get.assert_not_called()
    with patch("app.metadata_providers.lyrics.httpx.get", return_value=_json(200, {"weird": 1})):
        lyrics._throttle = Throttle(0.0)
        assert lyrics.fetch_lrclib_lyrics("Artist", "Title") is None


# --- Wikipedia -------------------------------------------------------------------

LINKS = [{"type": "wikipedia", "url": "https://en.wikipedia.org/wiki/Queen_(band)"}]


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (httpx.ConnectTimeout("slow"), None),
        (_json(429, {}, {"Retry-After": "2"}), None),
        (httpx.Response(404), {}),
        (_json(200, {"extract": "A band.", "title": "Queen"}), "A band."),
    ],
)
def test_wikipedia_results_and_caching(db_session, response, expected):
    with patch(
        "app.metadata_providers.wikipedia.httpx.get",
        **({"side_effect": response} if isinstance(response, Exception) else {"return_value": response}),
    ):
        got = cached(db_session, "wikipedia", "summary", "queen", lambda: wikipedia.summary_from_links(LINKS))
    row = db_session.get(ExternalCache, ("wikipedia", "summary", "queen"))
    if expected is None:
        assert got is None and row is None
    elif expected == {}:
        assert got == {} and row is not None
    else:
        assert got["text"] == expected and row is not None


def test_wikidata_route_and_its_failures():
    links = [{"type": "wikidata", "url": "https://www.wikidata.org/wiki/Q15862"}]
    entity = _json(200, {"entities": {"Q15862": {"sitelinks": {"enwiki": {"title": "Queen (band)"}}}}})
    with patch(
        "app.metadata_providers.wikipedia.httpx.get",
        side_effect=[entity, _json(200, {"extract": "Bio", "title": "Queen"})],
    ):
        assert wikipedia.summary_from_links(links)["text"] == "Bio"
    with patch("app.metadata_providers.wikipedia.httpx.get", side_effect=httpx.ConnectError("down")):
        assert wikipedia.summary_from_links(links) is None
    with patch("app.metadata_providers.wikipedia.httpx.get", return_value=_json(200, {"entities": {}})):
        assert wikipedia.summary_from_links(links) == {}
    with patch(
        "app.metadata_providers.wikipedia.httpx.get",
        side_effect=[entity, httpx.ReadTimeout("slow")],
    ):
        assert wikipedia.summary_from_links(links) is None
    with patch("app.metadata_providers.wikipedia.httpx.get", return_value=httpx.Response(500)):
        assert wikipedia.summary_from_links(LINKS) is None
    with patch("app.metadata_providers.wikipedia.httpx.get", return_value=_json(200, ["not", "a", "dict"])):
        assert wikipedia.summary_from_links(LINKS) is None


# --- MusicBrainz -----------------------------------------------------------------


def test_musicbrainz_503_pauses_for_its_retry_after():
    request = httpx.Request("GET", "https://musicbrainz.org/ws/2/recording")
    response = httpx.Response(503, headers={"Retry-After": "9"}, request=request)
    with patch("app.metadata_providers.musicbrainz._client.get", return_value=response):
        with recording_failures() as failures:
            result = musicbrainz._request_json("/recording", {}, 10)
    assert result.error == "MusicBrainz HTTP 503" and failures == ["MusicBrainz HTTP 503"]
    assert 8 < _paused(musicbrainz) <= 9


# --- bulk tasks stop on an outage --------------------------------------------------


def _root_with_tracks(db_session, count=8):
    root = LibraryRoot(path="/m", display_name="m")
    db_session.add(root)
    db_session.commit()
    for index in range(count):
        db_session.add(
            MediaItem(
                library_root_id=root.id, kind=MediaKind.track, relative_path=f"{index}.mp3",
                title=f"T{index}", artist="A",
            )
        )
    db_session.commit()
    task = LibraryTask(library_root_id=root.id, kind="mbid_lookup", status="running")
    db_session.add(task)
    db_session.commit()
    return root, task


def test_a_bulk_task_rides_out_a_blip_and_stops_on_a_long_outage(db_session, monkeypatch):
    root, task = _root_with_tracks(db_session, 14)
    pauses: list[float] = []
    monkeypatch.setattr(library_tasks, "_failure_pause", lambda task_id, seconds: pauses.append(seconds))
    calls = []

    def outage(item):
        calls.append(item.id)
        return musicbrainz.LookupResult(error="MusicBrainz HTTP 503")

    monkeypatch.setattr(library_tasks, "_lookup_recording", outage)
    library_tasks._run_mbid_lookup_task(root.id, task.id)
    db_session.expire_all()
    finished = db_session.get(LibraryTask, task.id)
    assert finished.status == "failed"
    assert "11 provider failures in a row over about 18 minutes" in finished.error
    assert "MusicBrainz HTTP 503" in finished.error
    assert len(calls) == 11  # stopped instead of marching through all 14
    # Growing pauses, capped at 5 minutes; stopped once they passed 15 minutes.
    assert pauses == [2, 4, 8, 16, 32, 64, 128, 256, 300, 300]


def test_a_minute_long_blip_does_not_stop_a_task(db_session, monkeypatch):
    """A provider sending error pages for about 90 s does not stop a bulk
    task: six failures in a row (about two minutes of pauses) pass."""
    root, task = _root_with_tracks(db_session, 10)
    monkeypatch.setattr(library_tasks, "_failure_pause", lambda task_id, seconds: None)
    outcomes = iter(
        [musicbrainz.LookupResult(error="x")] * 6 + [musicbrainz.LookupResult(value={"recording_mbid": "r"})] * 4
    )
    monkeypatch.setattr(library_tasks, "_lookup_recording", lambda item: next(outcomes))
    library_tasks._run_mbid_lookup_task(root.id, task.id)
    db_session.expire_all()
    assert db_session.get(LibraryTask, task.id).status == "completed"


def test_a_success_resets_the_failure_streak(db_session, monkeypatch):
    root, task = _root_with_tracks(db_session)
    monkeypatch.setattr(library_tasks, "_failure_pause", lambda task_id, seconds: None)
    outcomes = iter(
        [musicbrainz.LookupResult(error="x")] * 4
        + [musicbrainz.LookupResult(value={"recording_mbid": "r"})]
        + [musicbrainz.LookupResult(error="x")] * 3
    )
    monkeypatch.setattr(library_tasks, "_lookup_recording", lambda item: next(outcomes))
    library_tasks._run_mbid_lookup_task(root.id, task.id)
    db_session.expire_all()
    assert db_session.get(LibraryTask, task.id).status == "completed"


@pytest.mark.parametrize("kind", ["enrich", "listenbrainz", "cover_art", "artist_info"])
def test_every_bulk_provider_task_stops_on_an_outage(db_session, monkeypatch, kind):
    root, task = _root_with_tracks(db_session, 12)
    monkeypatch.setattr(library_tasks, "_failure_pause", lambda task_id, seconds: None)
    if kind == "enrich":
        def fail(db, item, **_kw):
            item.musicbrainz_last_error = "MusicBrainz HTTP 503"
            return {"matched": False, "status": "retry"}
        monkeypatch.setattr(library_tasks, "enrich_media_item", fail)
        library_tasks._run_enrichment_task(root.id, task.id)
    elif kind == "listenbrainz":
        for index, item in enumerate(db_session.query(MediaItem)):
            item.musicbrainz_artist_mbid = f"mb-{index}"
        db_session.commit()
        monkeypatch.setattr(library_tasks, "_TAG_BATCH", 1)
        monkeypatch.setattr(library_tasks.listenbrainz, "artist_tags", lambda mbids: None)
        library_tasks._run_listenbrainz_tags_task(root.id, task.id)
    elif kind == "cover_art":
        for item in db_session.query(MediaItem):
            item.musicbrainz_metadata_json = {"releases": [{"id": "r"}]}
        db_session.commit()
        monkeypatch.setattr(
            library_tasks, "fetch_cover_art_for_item",
            lambda db, item, commit=False: {"matched": True, "fetched": False, "failed": True, "error": "CAA down"},
        )
        library_tasks._run_cover_art_task(root.id, task.id)
    else:
        from app.artist_credits import rebuild_all_credits

        for index, item in enumerate(db_session.query(MediaItem)):
            item.artist = f"Artist {index}"
        db_session.commit()
        rebuild_all_credits(db_session)

        def info(db, user, name, **_kw):
            failed("MusicBrainz HTTP 503")
            return {}

        monkeypatch.setattr(library_tasks, "artist_info", info)
        monkeypatch.setattr(library_tasks, "artist_top_tracks", lambda db, user, name: {})
        admin = db_session.query(User).first()
        library_tasks._run_artist_info_task(root.id, task.id, admin.id)
    db_session.expire_all()
    finished = db_session.get(LibraryTask, task.id)
    assert finished.status == "failed" and "provider failures in a row" in finished.error


def test_failure_pause_wakes_when_the_task_is_stopped(monkeypatch):
    import threading
    import time

    event = threading.Event()
    event.set()
    monkeypatch.setitem(library_tasks._task_stop_events, 999, event)
    started = time.monotonic()
    library_tasks._failure_pause(999, 30)
    assert time.monotonic() - started < 1
    slept = []
    monkeypatch.setattr(library_tasks.time, "sleep", slept.append)
    library_tasks._failure_pause(1000, 3)
    assert slept == [3]


# --- scrobbling credentials ----------------------------------------------------------


def test_an_unreadable_credential_needs_reconnecting_and_is_logged_once(client, db_session, caplog):
    from app import scrobbling

    user = db_session.query(User).first()
    db_session.add(UserIntegration(user_id=user.id, provider="listenbrainz", credential="not-encrypted", external_username="me"))
    db_session.commit()
    scrobbling._unreadable_logged.clear()
    with caplog.at_level("WARNING", logger="app.scrobbling"):
        body = client.get("/api/integrations").json()
        client.get("/api/integrations")
    assert body == [
        {"provider": "listenbrainz", "linked": False, "needs_reconnect": True, "external_username": "me", "available": True}
    ]
    assert caplog.text.count("cannot be read") == 1


def test_a_readable_credential_is_linked(db_session):
    """The submission path itself (and its logging) is tested with the
    scrobble queue; here only the integration status."""
    from app import scrobbling
    from app.secrets import encrypt_credential

    user = db_session.query(User).first()
    db_session.add(
        UserIntegration(
            user_id=user.id, provider="listenbrainz", external_username="me",
            credential=encrypt_credential(json.dumps({"token": "tok"})),
        )
    )
    db_session.commit()
    assert scrobbling.list_integrations(db_session, user)[0] == {
        "provider": "listenbrainz", "linked": True, "needs_reconnect": False,
        "external_username": "me", "available": True,
    }
