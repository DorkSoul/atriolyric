from __future__ import annotations

import threading
import time
from unittest.mock import patch

import httpx
import pytest

from app.models import CustomArt, ExternalCache

MBID = "11111111-2222-3333-4444-555555555555"


PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32


def _fake_resize(src, out_path):
    out_path.write_bytes(b"thumb")
    return True


RESOLVED = ("The Band", "The Album")


@pytest.fixture()
def queued(monkeypatch, session_factory):
    """Catalog art is fetched on the provider queue: capture its jobs (the
    real queue dedupes by key the same way) and run them on demand against
    the test database."""
    jobs: dict[str, object] = {}

    def submit(key, job, **_):
        if key in jobs:
            return False
        jobs[key] = job
        return True

    monkeypatch.setattr("app.provider_queue.submit", submit)
    monkeypatch.setattr("app.want_metadata.SessionLocal", session_factory)
    return jobs


def _run(jobs) -> None:
    for key in list(jobs):
        jobs.pop(key)()


def test_catalog_album_art_is_downloaded_once_and_served(client, db_session, queued):
    with (
        patch("app.want_metadata._resolved_names", return_value=RESOLVED),
        patch("app.want_metadata._download_remote_art", return_value=(PNG, "image/png")) as dl,
        patch("app.custom_art._resize_art", side_effect=_fake_resize),
    ):
        first = client.get(f"/api/catalog/art/album/{MBID}")
        again = client.get(f"/api/catalog/art/album/{MBID}")
        # Off the request path: nothing downloaded yet, one job queued.
        assert dl.call_count == 0 and list(queued) == [f"catalog-art:album:{MBID}"]
        _run(queued)
        second = client.get(f"/api/catalog/art/album/{MBID}")
    assert first.status_code == again.status_code == 204
    assert first.headers["cache-control"] == "private, max-age=30"
    assert second.status_code == 200 and second.headers["content-type"].startswith("image/")
    assert dl.call_count == 1
    assert db_session.query(CustomArt).filter_by(entity_type="catalog_album", entity_key=MBID).count() == 1


def test_catalog_art_miss_is_204_and_remembered(client, db_session, queued):
    with (
        patch("app.want_metadata._resolved_names", return_value=RESOLVED),
        patch("app.want_metadata._download_remote_art", return_value=None) as dl,
        patch("app.want_metadata._sources", return_value=[("https://coverartarchive.org/x", "coverartarchive", None)]),
    ):
        assert client.get(f"/api/catalog/art/artist/{MBID}").status_code == 204
        _run(queued)
        known = client.get(f"/api/catalog/art/artist/{MBID}")
    assert known.status_code == 204 and known.headers["cache-control"] == "private, max-age=3600"
    assert queued == {}  # a remembered miss queues nothing
    assert dl.call_count == 1
    assert db_session.get(ExternalCache, ("catalogart", "artist", MBID)) is not None


def test_a_failed_download_is_not_remembered_as_missing(client, db_session, queued):
    """An exception is not stored as "missing" for 7 days."""
    request = httpx.Request("GET", "https://coverartarchive.org/x")
    outage = httpx.HTTPStatusError("503", request=request, response=httpx.Response(503, request=request))
    for error in (httpx.ConnectTimeout("slow"), outage):
        with (
            patch("app.want_metadata._resolved_names", return_value=RESOLVED),
            patch("app.want_metadata._download_remote_art", side_effect=error),
            patch("app.want_metadata._sources", return_value=[("https://coverartarchive.org/x", "coverartarchive", None)]),
        ):
            assert client.get(f"/api/catalog/art/album/{MBID}").status_code == 204
            _run(queued)
        assert db_session.get(ExternalCache, ("catalogart", "album", MBID)) is None
        # Not known-missing: the next request queues the fetch again.
        assert client.get(f"/api/catalog/art/album/{MBID}").headers["cache-control"] == "private, max-age=30"
        assert list(queued) == [f"catalog-art:album:{MBID}"]
        queued.clear()


def test_a_404_from_every_source_is_a_definitive_miss(db_session):
    from app.want_metadata import catalog_art

    request = httpx.Request("GET", "https://coverartarchive.org/x")
    gone = httpx.HTTPStatusError("404", request=request, response=httpx.Response(404, request=request))
    with (
        patch("app.want_metadata._download_remote_art", side_effect=gone),
        patch("app.want_metadata._sources", return_value=[("https://coverartarchive.org/x", "coverartarchive", None), ("https://coverartarchive.org/y", "coverartarchive", None)]),
    ):
        assert catalog_art(db_session, "album", MBID) is None
    assert db_session.get(ExternalCache, ("catalogart", "album", MBID)) is not None


def test_a_failed_name_lookup_is_not_remembered_as_missing(db_session):
    """A Deezer quota error while finding sources is a failure too."""
    from app.metadata_providers.result import failed
    from app.want_metadata import catalog_art

    def sources_with_a_failure(*_a):
        failed("Deezer error 4: Quota limit exceeded")
        return []

    with patch("app.want_metadata._sources", side_effect=sources_with_a_failure):
        assert catalog_art(db_session, "artist", MBID) is None
    assert db_session.get(ExternalCache, ("catalogart", "artist", MBID)) is None


def test_two_concurrent_fetches_download_once(session_factory):
    from app.want_metadata import catalog_art

    calls = []

    def slow_download(url):
        calls.append(url)
        time.sleep(0.3)
        return (PNG, "image/png")

    results = []

    def fetch():
        db = session_factory()
        try:
            row = catalog_art(db, "album", MBID)
            results.append(row.entity_key if row else None)
        finally:
            db.close()

    with (
        patch("app.want_metadata._download_remote_art", side_effect=slow_download),
        patch("app.want_metadata._sources", return_value=[("https://coverartarchive.org/x", "coverartarchive", None)]),
        patch("app.custom_art._resize_art", side_effect=_fake_resize),
    ):
        threads = [threading.Thread(target=fetch) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
    assert len(calls) == 1
    assert results == [MBID, MBID]


def test_art_downloader_allows_archive_and_wikimedia_but_not_deezer():
    from app import art

    with patch("app.art.socket.getaddrinfo", return_value=[(None, None, None, None, ("1.1.1.1", 443))]):
        art._validate_remote_art_url("https://ia800000.us.archive.org/a.jpg")
        art._validate_remote_art_url("https://upload.wikimedia.org/wikipedia/commons/a.jpg")
        # Deezer's terms allow no local copy, so its images are never downloaded.
        with pytest.raises(ValueError):
            art._validate_remote_art_url("https://e-cdns-images.dzcdn.net/a.jpg")


def test_serve_stale_keeps_saved_answers_without_refetching(db_session):
    from datetime import timedelta

    from app.external_cache import cached, serve_stale
    from app.models import ExternalCache

    cached(db_session, "musicbrainz", "artist", "x", lambda: {"name": "A"})
    row = db_session.get(ExternalCache, ("musicbrainz", "artist", "x"))
    row.fetched_at = row.fetched_at - timedelta(days=400)
    db_session.commit()

    def boom():
        raise AssertionError("refetched")

    with serve_stale():
        assert cached(db_session, "musicbrainz", "artist", "x", boom) == {"name": "A"}
        # An explicit refresh still goes to the provider.
        assert cached(db_session, "musicbrainz", "artist", "x", lambda: {"name": "B"}, refresh=True) == {"name": "B"}
    # Outside the want list, an expired answer is renewed.
    row = db_session.get(ExternalCache, ("musicbrainz", "artist", "x"))
    row.fetched_at = row.fetched_at - timedelta(days=400)
    db_session.commit()
    assert cached(db_session, "musicbrainz", "artist", "x", lambda: {"name": "C"}) == {"name": "C"}


def test_backfill_checks_each_entry_once_a_week(db_session):
    from app.models import User, WantListItem
    from app.security import hash_password
    from app.want_metadata import backfill_wanted_metadata

    user = User(username="w", password_hash=hash_password("pw-pw-pw-pw"))
    db_session.add(user)
    db_session.commit()
    db_session.add(WantListItem(user_id=user.id, kind="track", artist="A", title="T"))
    db_session.commit()
    with patch("app.want_metadata._fetch_entry") as fetch:
        assert backfill_wanted_metadata(db_session) == 1
        assert backfill_wanted_metadata(db_session) == 0
    assert fetch.call_count == 1


def test_catalog_art_searches_with_musicbrainz_names_not_the_callers(client, db_session, queued):
    """The stored image is shared by everyone, so ?name=&album= must not pick
    it: album covers come from the Cover Art Archive by MBID, artist photos
    from Wikimedia Commons by MBID."""
    group = {"title": "Real Album", "artist-credit": [{"name": "Real Band"}]}
    downloaded = []
    image = {"url": "https://upload.wikimedia.org/a.jpg", "thumb": "https://upload.wikimedia.org/a.jpg"}
    with (
        patch("app.want_metadata.mb_release_group", return_value=group),
        patch("app.want_metadata.mb_artist", return_value={"name": "Real Band"}),
        patch("app.want_metadata.artist_image", return_value=image) as photo,
        patch("app.want_metadata._download_remote_art", side_effect=lambda url: downloaded.append(url)),
    ):
        for kind in ("album", "artist"):
            resp = client.get(f"/api/catalog/art/{kind}/{MBID}", params={"name": "Evil", "album": "Chosen"})
            assert resp.status_code == 204
            _run(queued)
    assert downloaded == [
        f"https://coverartarchive.org/release-group/{MBID}/front-500",
        "https://upload.wikimedia.org/a.jpg",
    ]
    photo.assert_called_once()
    assert photo.call_args.args[1] == MBID


def test_catalog_art_stores_nothing_when_musicbrainz_cannot_resolve(client, db_session, queued):
    with (
        patch("app.want_metadata.mb_release_group", return_value={}),
        patch("app.want_metadata.mb_artist", return_value={}),
        patch("app.want_metadata._download_remote_art") as dl,
    ):
        for kind in ("album", "artist"):
            resp = client.get(f"/api/catalog/art/{kind}/{MBID}", params={"name": "Evil"})
            assert resp.status_code == 204
            _run(queued)
    assert dl.call_count == 0
    assert db_session.query(CustomArt).count() == 0


def test_catalog_art_edge_paths(db_session, queued, caplog):
    """A remembered miss returns at once; an undecodable source is skipped;
    a row stored meanwhile (unique key) is returned; a crashing job is logged."""
    from sqlalchemy.exc import IntegrityError

    from app.external_cache import store
    from app.want_metadata import catalog_art, queue_catalog_art

    store(db_session, "catalogart", "artist", MBID, {"missing": True})
    with patch("app.want_metadata._sources") as sources:
        assert catalog_art(db_session, "artist", MBID) is None
    sources.assert_not_called()

    other = "22222222-2222-3333-4444-555555555555"
    with (
        patch("app.want_metadata._sources", return_value=[("https://a", "coverartarchive", None), ("https://b", "coverartarchive", None)]),
        patch("app.want_metadata._download_remote_art", return_value=(PNG, "image/png")),
        patch("app.want_metadata.store_custom_art", side_effect=[ValueError("bad"), IntegrityError("x", {}, Exception())]),
        patch("app.want_metadata.get_custom_art", return_value=None) as get,
    ):
        assert catalog_art(db_session, "album", other) is None
    assert get.call_count == 3  # before, under the lock, after the collision

    with patch("app.want_metadata.catalog_art", side_effect=RuntimeError("boom")):
        queue_catalog_art("album", other)
        with caplog.at_level("ERROR", logger="app.want_metadata"):
            _run(queued)
    assert "Catalog art fetch failed" in caplog.text


def test_a_refused_art_job_is_the_same_short_lived_204(client, db_session, monkeypatch):
    """The provider queue is full (or the user has too many covers waiting):
    the tile gets the usual "ask again shortly" 204, and the job carries the
    asking user for the per-user limit."""
    from app.models import User

    asked: list[tuple[str, int | None]] = []

    def refuse(key, job, *, user_id=None, scheduled=False):
        asked.append((key, user_id))
        return False

    monkeypatch.setattr("app.provider_queue.submit", refuse)
    response = client.get(f"/api/catalog/art/album/{MBID}")
    assert response.status_code == 204
    assert response.headers["cache-control"] == "private, max-age=30"
    assert asked == [(f"catalog-art:album:{MBID}", db_session.query(User).one().id)]
    assert db_session.get(ExternalCache, ("catalogart", "album", MBID)) is None  # nothing remembered


def test_the_scheduled_fill_yields_between_entries_and_release_groups(db_session):
    from app.models import User, WantListItem
    from app.security import hash_password
    from app.want_metadata import _fetch_entry, backfill_wanted_metadata

    user = User(username="w", password_hash=hash_password("pw-pw-pw-pw"))
    db_session.add(user)
    db_session.commit()
    for title in ("T1", "T2", "T3"):
        db_session.add(WantListItem(user_id=user.id, kind="track", artist="A", title=title))
    artist = WantListItem(user_id=user.id, kind="artist", artist="A", title="A", artist_mbid=MBID)
    db_session.add(artist)
    db_session.commit()
    with (
        patch("app.want_metadata._fetch_entry"),
        patch("app.want_metadata.provider_queue.yield_to_user_jobs") as between_entries,
    ):
        assert backfill_wanted_metadata(db_session, limit=3) == 3
    assert between_entries.call_count == 2  # between entries, not after the last
    groups = [{"mbid": f"g{n}", "title": f"Album {n}"} for n in range(3)]
    with (
        patch("app.want_metadata.catalog_art"),
        patch("app.want_metadata.tracklist"),
        patch("app.want_metadata.release_groups", return_value=groups),
        patch("app.want_metadata.provider_queue.yield_to_user_jobs") as between_groups,
    ):
        _fetch_entry(db_session, artist, None)
    assert between_groups.call_count == 3
