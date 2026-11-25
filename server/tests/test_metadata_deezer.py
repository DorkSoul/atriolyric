from __future__ import annotations

from unittest.mock import Mock, patch

import pytest

from app.metadata_providers import deezer


@pytest.mark.network
def test_search_artist_track_real_api_finds_a_preview():
    result = deezer.search_artist_track("Queen")
    assert result is not None
    assert result["artist"]
    assert result["preview_url"].startswith("http")
    # Deezer's terms require preview content to stay next to a link back to
    # the real listing - confirm the real API actually gives us one.
    assert result["view_url"] and result["view_url"].startswith("http")


def test_search_artist_track_empty_name_short_circuits():
    assert deezer.search_artist_track("") is None


def test_search_artist_track_returns_none_on_network_failure():
    with patch("app.metadata_providers.deezer.httpx.get", side_effect=OSError("down")):
        assert deezer.search_artist_track("Queen") is None


def test_search_artist_track_prefers_exact_artist_match():
    fake_response = Mock(status_code=200)
    fake_response.raise_for_status = Mock()
    fake_response.json.return_value = {
        "data": [
            {
                "title": "Unrelated",
                "preview": "https://example.com/other.mp3",
                "artist": {"name": "Some Other Band", "link": "https://deezer.com/artist/other"},
                "album": {"cover_medium": "https://example.com/other-medium.jpg"},
            },
            {
                "title": "Bohemian Rhapsody",
                "preview": "https://example.com/queen.mp3",
                "artist": {"name": "Queen", "link": "https://deezer.com/artist/queen"},
                "album": {"cover_medium": "https://example.com/queen-medium.jpg"},
                "link": "https://deezer.com/track/queen-bohemian-rhapsody",
            },
        ]
    }
    with patch("app.metadata_providers.deezer.httpx.get", return_value=fake_response):
        result = deezer.search_artist_track("Queen")
    assert result == {
        "artist": "Queen",
        "title": "Bohemian Rhapsody",
        "artwork_url": "https://example.com/queen-medium.jpg",
        "preview_url": "https://example.com/queen.mp3",
        "view_url": "https://deezer.com/track/queen-bohemian-rhapsody",
    }


def test_search_artist_track_falls_back_to_artist_link():
    fake_response = Mock(status_code=200)
    fake_response.raise_for_status = Mock()
    fake_response.json.return_value = {
        "data": [
            {
                "title": "Bohemian Rhapsody",
                "preview": "https://example.com/queen.mp3",
                "artist": {"name": "Queen", "link": "https://deezer.com/artist/queen"},
                # No track-level "link" this time.
            },
        ]
    }
    with patch("app.metadata_providers.deezer.httpx.get", return_value=fake_response):
        result = deezer.search_artist_track("Queen")
    assert result["view_url"] == "https://deezer.com/artist/queen"


def test_search_artist_track_never_falls_back_to_another_artist():
    fake_response = Mock(status_code=200)
    fake_response.raise_for_status = Mock()
    fake_response.json.return_value = {
        "data": [
            {"title": "No Preview", "artist": {"name": "Someone Else"}},
            {
                "title": "Has A Preview",
                "preview": "https://example.com/fallback.mp3",
                "artist": {"name": "Close Enough Name"},
                "album": {},
            },
        ]
    }
    with patch("app.metadata_providers.deezer.httpx.get", return_value=fake_response):
        # The search is fuzzy: a near-miss would preview, and link to, someone else.
        assert deezer.search_artist_track("Queen") is None
        # The same artist spelled differently still matches.
        assert deezer.search_artist_track("close enough name")["preview_url"] == "https://example.com/fallback.mp3"


def test_search_artist_track_returns_none_when_no_results_have_previews():
    fake_response = Mock(status_code=200)
    fake_response.raise_for_status = Mock()
    fake_response.json.return_value = {"data": [{"title": "No Preview", "artist": {"name": "Queen"}}]}
    with patch("app.metadata_providers.deezer.httpx.get", return_value=fake_response):
        assert deezer.search_artist_track("Queen") is None
