"""The ListenBrainz provider: response parsing, scores and listen submission."""

from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest

from app.metadata_providers import listenbrainz
from app.metadata_providers._throttle import Throttle


@pytest.fixture(autouse=True)
def _no_pacing(monkeypatch):
    monkeypatch.setattr(listenbrainz, "_throttle", Throttle(0.0))


def _json(status: int, payload, headers: dict | None = None) -> httpx.Response:
    return httpx.Response(status, json=payload, headers=headers, request=httpx.Request("GET", "https://x"))


def test_similar_artists_scale_scores_to_the_best_and_sort():
    rows = [
        {"artist_mbid": "b", "name": "Second", "score": 50},
        {"artist_mbid": "a", "name": "First", "score": 200},
        {"artist_mbid": "c", "name": "", "score": 10},
        {"artist_mbid": "d", "name": "Zero", "score": 0},
        "junk",
    ]
    with patch("app.metadata_providers.listenbrainz._client.request", return_value=_json(200, rows)) as get:
        got = listenbrainz.similar_artists("seed")
    assert got == [
        {"name": "First", "mbid": "a", "match": 1.0},
        {"name": "Second", "mbid": "b", "match": 0.25},
    ]
    params = get.call_args.kwargs["params"]
    assert params["artist_mbids"] == "seed" and params["algorithm"] == listenbrainz.SIMILAR_ARTISTS_ALGORITHM
    assert get.call_args.kwargs["headers"]["User-Agent"]


def test_similar_recordings_parse_artist_title_and_match():
    rows = [
        {"recording_mbid": "r1", "recording_name": "Song", "artist_credit_name": "Band", "score": 100},
        {"recording_mbid": "r2", "recording_name": "Other", "artist_credit_name": "Act", "score": 40},
    ]
    with patch("app.metadata_providers.listenbrainz._client.request", return_value=_json(200, rows)):
        assert listenbrainz.similar_recordings("seed") == [
            {"artist": "Band", "title": "Song", "mbid": "r1", "match": 1.0},
            {"artist": "Act", "title": "Other", "mbid": "r2", "match": 0.4},
        ]
    with patch("app.metadata_providers.listenbrainz._client.request", return_value=_json(200, [{"score": 3}])):
        assert listenbrainz.similar_recordings("seed") == []


def test_top_recordings_keep_the_most_played_copy_of_a_song():
    rows = [
        {"recording_mbid": "r1", "recording_name": "Hit", "total_listen_count": 900, "total_user_count": 90},
        {"recording_mbid": "r1b", "recording_name": "hit", "total_listen_count": 50, "total_user_count": 5},
        {"recording_mbid": "r2", "recording_name": "B-side", "total_listen_count": "40", "total_user_count": None},
    ]
    with patch("app.metadata_providers.listenbrainz._client.request", return_value=_json(200, rows)):
        got = listenbrainz.top_recordings_for_artist("a", "tok")
    assert got == [
        {"title": "Hit", "mbid": "r1", "playcount": 900, "listeners": 90, "rank": 1},
        {"title": "B-side", "mbid": "r2", "playcount": 40, "listeners": None, "rank": 2},
    ]
    with patch("app.metadata_providers.listenbrainz._client.request", return_value=_json(200, rows)):
        assert len(listenbrainz.top_recordings_for_artist("a", "tok", limit=1)) == 1


def test_top_recordings_send_the_token_and_say_why_a_refusal_failed():
    with patch("app.metadata_providers.listenbrainz._client.request", return_value=_json(200, [])) as request:
        assert listenbrainz.top_recordings_for_artist("a", "tok") == []
    assert request.call_args.kwargs["headers"]["Authorization"] == "Token tok"
    refusal = _json(401, {"code": 401, "error": "You need to provide an Auth token for this endpoint."})
    with patch("app.metadata_providers.listenbrainz._client.request", return_value=refusal):
        result = listenbrainz._request("GET", f"{listenbrainz.API_URL}/1/popularity/top-recordings-for-artist/a")
    assert result.reason == "ListenBrainz HTTP 401: You need to provide an Auth token for this endpoint."
    long = _json(401, {"error": "word " * 100})
    with patch("app.metadata_providers.listenbrainz._client.request", return_value=long):
        result = listenbrainz._request("GET", f"{listenbrainz.API_URL}/1/popularity/top-recordings-for-artist/a")
    assert len(result.reason) == len("ListenBrainz HTTP 401: ") + 160 and result.reason.endswith("...")


def test_a_page_that_is_not_json_says_what_came_back():
    page = httpx.Response(200, text="<html><body>  Down for\n maintenance </body></html>", headers={"Content-Type": "text/html; charset=utf-8"})
    with patch("app.metadata_providers.listenbrainz._client.request", return_value=page):
        result = listenbrainz._request("GET", f"{listenbrainz.API_URL}/1/popularity/top-recordings-for-artist/a")
    assert result.reason == (
        "ListenBrainz returned an invalid response (text/html: <html><body> Down for maintenance </body></html>)"
    )
    with patch("app.metadata_providers.listenbrainz._client.request", return_value=httpx.Response(200)):
        result = listenbrainz._request("GET", f"{listenbrainz.API_URL}/1/popularity/top-recordings-for-artist/a")
    assert result.reason == "ListenBrainz returned an invalid response (no content type, empty)"


def test_artist_tags_drop_single_votes_and_batch(monkeypatch):
    monkeypatch.setattr(listenbrainz, "MBID_BATCH", 1)
    answers = [
        _json(200, [{"artist_mbid": "a", "tag": {"artist": [
            {"tag": "shoegaze", "count": 5}, {"tag": "seen live", "count": 1}, {"tag": "dream pop", "count": 2},
        ]}}]),
        _json(200, [{"artist_mbid": "b", "tag": {}}]),
    ]
    with patch("app.metadata_providers.listenbrainz._client.request", side_effect=answers) as get:
        got = listenbrainz.artist_tags(["a", "b"])
    assert got == {"a": [("shoegaze", 5), ("dream pop", 2)], "b": []}
    assert get.call_count == 2


def test_popularity_reads_counts_per_kind():
    with patch(
        "app.metadata_providers.listenbrainz._client.request",
        return_value=_json(200, [{"release_group_mbid": "g", "total_listen_count": None, "total_user_count": 4}]),
    ) as post:
        assert listenbrainz.release_group_popularity(["g"]) == {"g": {"playcount": None, "listeners": 4}}
    assert post.call_args.args[:2] == ("POST", "https://api.listenbrainz.org/1/popularity/release-group")
    assert post.call_args.kwargs["json"] == {"release_group_mbids": ["g"]}


def test_validate_token_tells_valid_invalid_and_unreachable_apart():
    with patch("app.metadata_providers.listenbrainz._client.get", return_value=_json(200, {"valid": True, "user_name": "me"})) as get:
        assert listenbrainz.validate_token("tok") == "me"
    assert get.call_args.kwargs["headers"]["Authorization"] == "Token tok"
    with patch("app.metadata_providers.listenbrainz._client.get", return_value=_json(200, {"valid": False})):
        assert listenbrainz.validate_token("tok") is False
    with patch("app.metadata_providers.listenbrainz._client.get", return_value=_json(401, {"code": 401})):
        assert listenbrainz.validate_token("tok") is False
    with patch("app.metadata_providers.listenbrainz._client.get", side_effect=httpx.ConnectError("down")):
        assert listenbrainz.validate_token("tok") is None
    with patch("app.metadata_providers.listenbrainz._client.get", return_value=_json(500, {})):
        assert listenbrainz.validate_token("tok") is None


def test_submit_listen_sends_the_listen_and_reports_the_outcome():
    with patch("app.metadata_providers.listenbrainz._client.post", return_value=_json(200, {"status": "ok"})) as post:
        outcome = listenbrainz.submit_listen(
            "tok", listen_type="single", artist="Band", title="Song", album="LP",
            listened_at=1700000000, duration=200.4, recording_mbid="rec", artist_mbids=["art"],
        )
    assert outcome == "accepted"
    body = post.call_args.kwargs["json"]
    assert body["listen_type"] == "single"
    listen = body["payload"][0]
    assert listen["listened_at"] == 1700000000
    meta = listen["track_metadata"]
    assert (meta["artist_name"], meta["track_name"], meta["release_name"]) == ("Band", "Song", "LP")
    assert meta["additional_info"] == {
        "submission_client": "Atriolyric", "duration_ms": 200400, "recording_mbid": "rec", "artist_mbids": ["art"],
    }
    assert post.call_args.kwargs["headers"]["Authorization"] == "Token tok"
    for response, expected in (
        (_json(401, {}), "unauthorized"),
        (_json(403, {}), "unauthorized"),
        (_json(400, {}), "rejected"),
        (_json(429, {}), "transient"),
        (_json(503, {}), "transient"),
    ):
        with patch("app.metadata_providers.listenbrainz._client.post", return_value=response):
            assert listenbrainz.submit_listen(
                "tok", listen_type="single", artist="A", title="T", album=None, listened_at=1, duration=None
            ) == expected
    with patch("app.metadata_providers.listenbrainz._client.post", side_effect=httpx.ReadTimeout("slow")):
        assert listenbrainz.submit_listen(
            "tok", listen_type="playing_now", artist="A", title="T", album=None, listened_at=None, duration=None
        ) == "transient"


# --- Wikimedia Commons artist photos (app.metadata_providers.wikipedia) -------------


def _commons(licence: str | None, artist: str = "<a href='x'>Pho &amp; Tographer</a>"):
    meta = {"Artist": {"value": artist}, "LicenseUrl": {"value": "https://creativecommons.org/licenses/by/4.0"}}
    if licence is not None:
        meta["LicenseShortName"] = {"value": licence}
    return _json(200, {"query": {"pages": {"1": {"imageinfo": [{
        "url": "https://upload.wikimedia.org/full.jpg",
        "thumburl": "https://thumb.wikimedia.org/w/1000px-full.jpg?utm_source=x",
        "descriptionurl": "https://commons.wikimedia.org/wiki/File:Full.jpg",
        "extmetadata": meta,
    }]}}}})


def test_commons_image_keeps_the_credit_and_skips_unfree_files(monkeypatch):
    from app.metadata_providers import wikipedia
    from app.metadata_providers.result import Found, NotFound

    monkeypatch.setattr(wikipedia, "_throttle", Throttle(0.0))
    with patch("app.metadata_providers.wikipedia.httpx.get", return_value=_commons("CC BY-SA 4.0")):
        found = wikipedia.commons_image("Full.jpg")
    assert isinstance(found, Found)
    assert found.data == {
        "url": "https://thumb.wikimedia.org/w/1000px-full.jpg",
        "thumb": "https://thumb.wikimedia.org/w/1000px-full.jpg",
        "author": "Pho & Tographer",
        "license": "CC BY-SA 4.0",
        "license_url": "https://creativecommons.org/licenses/by/4.0",
        "page_url": "https://commons.wikimedia.org/wiki/File:Full.jpg",
    }
    for licence in ("Fair use", None):
        with patch("app.metadata_providers.wikipedia.httpx.get", return_value=_commons(licence)):
            assert isinstance(wikipedia.commons_image("Full.jpg"), NotFound)
    with patch("app.metadata_providers.wikipedia.httpx.get", return_value=_json(200, {"query": {"pages": {}}})):
        assert isinstance(wikipedia.commons_image("Gone.jpg"), NotFound)


def test_artist_photo_from_the_wikidata_link(monkeypatch):
    from app.metadata_providers import wikipedia

    monkeypatch.setattr(wikipedia, "_throttle", Throttle(0.0))
    claims = _json(200, {"claims": {"P18": [{"mainsnak": {"datavalue": {"value": "Full.jpg"}}}]}})
    links = [
        {"type": "official homepage", "url": "https://band.example"},
        {"type": "wikidata", "url": "https://www.wikidata.org/wiki/Q1"},
    ]
    with patch("app.metadata_providers.wikipedia.httpx.get", side_effect=[claims, _commons("CC0")]) as get:
        image = wikipedia.image_from_links(links)
    assert image["license"] == "CC0"
    assert get.call_args_list[0].kwargs["params"]["entity"] == "Q1"
    assert get.call_args_list[1].kwargs["params"]["titles"] == "File:Full.jpg"
    # No image named, no Wikidata link, or a failed request.
    with patch("app.metadata_providers.wikipedia.httpx.get", return_value=_json(200, {"claims": {}})):
        assert wikipedia.image_from_links(links) == {}
    assert wikipedia.image_from_links([{"type": "official homepage", "url": "https://x"}]) == {}
    with patch("app.metadata_providers.wikipedia.httpx.get", side_effect=httpx.ConnectError("down")):
        assert wikipedia.image_from_links(links) is None
