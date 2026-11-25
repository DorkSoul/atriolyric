from __future__ import annotations

from unittest.mock import Mock, patch

import pytest

from app.metadata_providers import coverart


@pytest.mark.network
def test_no_art_returns_none_for_real_release_confirmed_to_have_none():
    # Verified live against the real API: Nevermind's cover-art-archive
    # flags all report false for this release id.
    assert coverart.get_front_image_url("f922ec87-4758-421d-a839-3193455345ff") is None


def test_returns_front_image_when_present():
    fake_response = Mock()
    fake_response.status_code = 200
    fake_response.raise_for_status = Mock()
    fake_response.json.return_value = {
        "images": [
            {"front": False, "image": "https://coverartarchive.org/.../back.jpg"},
            {"front": True, "image": "https://coverartarchive.org/.../front.jpg"},
        ]
    }
    with patch("app.metadata_providers.coverart._client.get", return_value=fake_response):
        url = coverart.get_front_image_url("some-mbid-front")
    assert url == "https://coverartarchive.org/.../front.jpg"


def test_returns_none_when_no_front_flagged():
    fake_response = Mock()
    fake_response.status_code = 200
    fake_response.raise_for_status = Mock()
    fake_response.json.return_value = {"images": [{"front": False, "image": "x"}]}
    with patch("app.metadata_providers.coverart._client.get", return_value=fake_response):
        assert coverart.get_front_image_url("some-mbid-no-front") is None


def test_returns_none_for_missing_release():
    fake_response = Mock(status_code=404)
    with patch("app.metadata_providers.coverart._client.get", return_value=fake_response):
        assert coverart.get_front_image_url("missing-mbid") is None


def test_reuses_cached_front_image():
    fake_response = Mock(status_code=200)
    fake_response.raise_for_status = Mock()
    fake_response.json.return_value = {"images": [{"front": True, "image": "art"}]}
    with patch(
        "app.metadata_providers.coverart._client.get", return_value=fake_response
    ) as get:
        assert coverart.get_front_image_url("cached-mbid") == "art"
        assert coverart.get_front_image_url("cached-mbid") == "art"
    get.assert_called_once()


def test_empty_mbid_short_circuits():
    assert coverart.get_front_image_url("") is None
