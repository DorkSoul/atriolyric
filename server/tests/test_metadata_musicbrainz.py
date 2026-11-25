from __future__ import annotations

from unittest.mock import Mock, patch

import httpx
import pytest

from app.metadata_providers import musicbrainz
from app.metadata_providers._throttle import Throttle


@pytest.fixture(autouse=True)
def _fresh_throttle(monkeypatch):
    """A 503 here pauses the throttle (Retry-After); keep that to this test."""
    monkeypatch.setattr(musicbrainz, "_throttle", Throttle(min_interval=1.0))


@pytest.mark.network
def test_search_recording_real_api_finds_bohemian_rhapsody():
    result = musicbrainz.search_recording_result("Queen", "Bohemian Rhapsody").value
    assert result is not None
    assert result["title"] is not None
    assert "queen" in result["artist"].lower()
    assert result["recording_mbid"]


@pytest.mark.network
def test_search_recording_real_api_no_match_for_nonsense():
    result = musicbrainz.search_recording_result("asdkfjhaslkdfjh", "qwoeiruqwoeiru9999").value
    assert result is None


def test_search_recording_handles_error_body_gracefully():
    fake_response = Mock()
    fake_response.raise_for_status = Mock()
    fake_response.json.return_value = {"error": "The MusicBrainz web server is currently busy."}
    with patch("app.metadata_providers.musicbrainz._client.get", return_value=fake_response):
        assert musicbrainz.search_recording_result("Queen", "Bohemian Rhapsody").value is None


def test_search_recording_handles_network_failure():
    with patch("app.metadata_providers.musicbrainz._client.get", side_effect=OSError("no network")):
        assert musicbrainz.search_recording_result("Queen", "Bohemian Rhapsody").value is None


def test_search_recording_returns_none_for_empty_input():
    assert musicbrainz.search_recording_result("", "").value is None


def test_search_recording_returns_none_for_empty_result():
    fake_response = Mock()
    fake_response.raise_for_status = Mock()
    fake_response.json.return_value = {"recordings": []}
    with patch("app.metadata_providers.musicbrainz._throttle.wait"), patch(
        "app.metadata_providers.musicbrainz._client.get", return_value=fake_response
    ):
        assert musicbrainz.search_recording_result("Artist", "Track").value is None


def test_recording_summary_rejects_missing_recording_id():
    assert musicbrainz._recording_summary({"title": "Track"}) is None


def test_isrc_lookup_normalizes_the_identifier_and_maps_a_recording():
    fake_response = Mock()
    fake_response.raise_for_status = Mock()
    fake_response.json.return_value = {"recordings": [{"id": "rec-isrc", "title": "Track"}]}
    with patch("app.metadata_providers.musicbrainz._throttle.wait"), patch(
        "app.metadata_providers.musicbrainz._client.get", return_value=fake_response
    ) as get:
        result = musicbrainz.lookup_recording_by_isrc("gb-abc-1234567")

    assert result == musicbrainz.LookupResult(
        value={
            "recording_mbid": "rec-isrc",
            "title": "Track",
            "artist": None,
            "artist_mbid": None,
            "album": None,
            "release_mbid": None,
            "length_seconds": None,
        }
    )
    assert get.call_args.args[0].endswith("/isrc/GBABC1234567")
    assert get.call_args.kwargs["params"] == {"fmt": "json"}










def test_recording_result_retains_errors_and_escapes_query_quotes():
    with patch("app.metadata_providers.musicbrainz._throttle.wait"), patch(
        "app.metadata_providers.musicbrainz._client.get", side_effect=OSError("offline")
    ):
        result = musicbrainz.search_recording_result('Artist "Quoted"', "Track")
    assert result.error == "MusicBrainz request failed: OSError"

    fake_response = Mock()
    fake_response.raise_for_status = Mock()
    fake_response.json.return_value = {"recordings": []}
    with patch("app.metadata_providers.musicbrainz._throttle.wait"), patch(
        "app.metadata_providers.musicbrainz._client.get", return_value=fake_response
    ) as get:
        musicbrainz.search_recording_result('Artist "Quoted"', "Track")
    assert '\\"Quoted\\"' in get.call_args.kwargs["params"]["query"]


@pytest.mark.parametrize(
    ("status_code", "expected"),
    [(404, musicbrainz.LookupResult()), (503, musicbrainz.LookupResult(error="MusicBrainz HTTP 503"))],
)
def test_request_json_distinguishes_catalogue_misses_from_http_failures(
    status_code, expected
):
    request = httpx.Request("GET", "https://musicbrainz.org/ws/2/recording")
    response = httpx.Response(status_code, request=request)
    error = httpx.HTTPStatusError("status error", request=request, response=response)
    fake_response = Mock()
    fake_response.raise_for_status.side_effect = error
    with patch("app.metadata_providers.musicbrainz._throttle.wait"), patch(
        "app.metadata_providers.musicbrainz._client.get", return_value=fake_response
    ):
        assert musicbrainz._request_json("/recording", {}, 10) == expected


def test_request_json_logs_and_returns_bounded_http_error_body(caplog):
    request = httpx.Request("GET", "https://musicbrainz.org/ws/2/isrc/USSR20700627")
    response = httpx.Response(
        400, request=request, json={"error": "Invalid ISRC", "detail": "bad value"}
    )
    error = httpx.HTTPStatusError("status error", request=request, response=response)
    fake_response = Mock()
    fake_response.raise_for_status.side_effect = error
    caplog.set_level("WARNING", logger="app.metadata_providers.musicbrainz")

    with patch("app.metadata_providers.musicbrainz._throttle.wait"), patch(
        "app.metadata_providers.musicbrainz._client.get", return_value=fake_response
    ):
        result = musicbrainz._request_json("/isrc/USSR20700627", {}, 10)

    assert result.error == 'MusicBrainz HTTP 400: {"error":"Invalid ISRC","detail":"bad value"}'
    assert "status=400 path=/ws/2/isrc/USSR20700627" in caplog.text
    assert '"error":"Invalid ISRC"' in caplog.text


def test_request_json_rejects_non_object_payloads():
    fake_response = Mock()
    fake_response.raise_for_status = Mock()
    fake_response.json.return_value = []
    with patch("app.metadata_providers.musicbrainz._throttle.wait"), patch(
        "app.metadata_providers.musicbrainz._client.get", return_value=fake_response
    ):
        assert musicbrainz._request_json("/recording", {}, 10) == musicbrainz.LookupResult(
            error="MusicBrainz returned an invalid response"
        )


def test_isrc_lookup_handles_blank_and_retryable_request_errors():
    assert musicbrainz.lookup_recording_by_isrc("---") == musicbrainz.LookupResult()
    with patch(
        "app.metadata_providers.musicbrainz._request_json",
        return_value=musicbrainz.LookupResult(error="MusicBrainz HTTP 503"),
    ):
        assert musicbrainz.lookup_recording_by_isrc("GBABC1234567") == musicbrainz.LookupResult(
            error="MusicBrainz HTTP 503"
        )


def test_fetch_recording_handles_empty_ids_network_failures_and_bad_payloads():
    assert musicbrainz.fetch_recording_result("").value is None

    with patch("app.metadata_providers.musicbrainz._throttle.wait"), patch(
        "app.metadata_providers.musicbrainz._client.get", side_effect=OSError("offline")
    ):
        assert musicbrainz.fetch_recording_result("recording-id").value is None

    fake_response = Mock()
    fake_response.raise_for_status = Mock()
    fake_response.json.return_value = {"title": "No recording id"}
    with patch("app.metadata_providers.musicbrainz._throttle.wait"), patch(
        "app.metadata_providers.musicbrainz._client.get", return_value=fake_response
    ):
        assert musicbrainz.fetch_recording_result("recording-id").value is None


def test_a_merged_artist_redirect_is_followed(monkeypatch):
    """MusicBrainz answers a merged artist's old MBID with 301 to the artist
    it was merged into; that is the artist's data, not a failure."""
    old = "00000001-0000-4000-8000-000000000001"
    new = "00000002-0000-4000-8000-000000000002"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/ws/2/artist/{old}":
            return httpx.Response(301, headers={"Location": f"https://musicbrainz.org/ws/2/artist/{new}?fmt=json"})
        assert request.url.path == f"/ws/2/artist/{new}"
        return httpx.Response(200, json={"id": new, "name": "Merged Band"})

    monkeypatch.setattr(musicbrainz._client, "_transport", httpx.MockTransport(handler))
    monkeypatch.setattr(musicbrainz._throttle, "wait", lambda: True)
    result = musicbrainz.lookup_artist(old)
    assert result.error is None and result.value == {"id": new, "name": "Merged Band"}
