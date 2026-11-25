from __future__ import annotations

from unittest.mock import patch

from sqlalchemy import text

from app.enrichment import (
    enrich_media_item,
    fetch_cover_art_for_item,
    get_or_fetch_lyrics,
)
from app.metadata_providers.musicbrainz import LookupResult
from app.metadata_providers.result import Found
from app.models import LibraryRoot, MediaItem, MediaKind, MusicBrainzTag
from tests.conftest import make_audio_fixture


def _make_item(db_session, **kwargs) -> MediaItem:
    root = LibraryRoot(path="/fake", display_name="Fake")
    db_session.add(root)
    db_session.commit()
    defaults = dict(
        library_root_id=root.id,
        kind=MediaKind.track,
        relative_path="song.mp3",
        title="Bohemian Rhapsody",
    )
    defaults.update(kwargs)
    item = MediaItem(**defaults)
    db_session.add(item)
    db_session.commit()
    db_session.refresh(item)
    return item


def test_enrich_fills_only_blank_fields(db_session):
    item = _make_item(db_session, artist="Queen (my own tag)", album=None)

    with (
        patch(
            "app.enrichment.musicbrainz.search_recording_result",
            return_value=LookupResult(value={
                "recording_mbid": "rec-123",
                "artist_mbid": "art-456",
                "artist": "Queen",
                "album": "A Night at the Opera",
                "release_mbid": "rel-789",
            }),
        ),
        patch(
            "app.enrichment.musicbrainz.fetch_recording_result",
            return_value=LookupResult(),
        ),
    ):
        result = enrich_media_item(db_session, item)

    assert result["matched"] is True
    assert item.artist == "Queen (my own tag)"  # not overwritten
    assert item.album == "A Night at the Opera"  # filled in, was blank
    assert item.musicbrainz_recording_mbid == "rec-123"
    assert item.cover_art_url is None


def test_enrich_can_optionally_fetch_cover_art(db_session):
    item = _make_item(db_session, artist="Artist")
    with (
        patch(
            "app.enrichment.musicbrainz.search_recording_result",
            return_value=LookupResult(
                value={"recording_mbid": "rec-123", "release_mbid": "rel-789"}
            ),
        ),
        patch(
            "app.enrichment.coverart.front_image",
            return_value=Found("https://example/art.jpg"),
        ) as fetch_art,
        patch(
            "app.enrichment.musicbrainz.fetch_recording_result",
            return_value=LookupResult(),
        ),
    ):
        enrich_media_item(db_session, item, fetch_cover_art=True)

    assert item.cover_art_url == "https://example/art.jpg"
    fetch_art.assert_called_once_with("rel-789", None)


def test_enrich_no_match_leaves_item_untouched(db_session):
    item = _make_item(db_session, artist=None, album=None)
    with patch(
        "app.enrichment.musicbrainz.search_recording_result", return_value=LookupResult()
    ):
        result = enrich_media_item(db_session, item)
    assert result == {"matched": False, "metadata_saved": False, "status": "no_match"}
    assert item.artist is None
    assert item.musicbrainz_recording_mbid is None


def test_enrich_ignores_a_search_result_without_a_recording_id(db_session):
    item = _make_item(db_session, artist="Artist")
    with patch(
        "app.enrichment.musicbrainz.search_recording_result",
        return_value=LookupResult(value={"artist": "Artist"}),
    ):
        result = enrich_media_item(db_session, item)

    assert result == {"matched": False, "metadata_saved": False, "status": "no_match"}
    assert item.musicbrainz_recording_mbid is None


def test_enrich_can_defer_committing_a_catalogue_miss(db_session):
    item = _make_item(db_session, artist="Artist")
    with patch(
        "app.enrichment.musicbrainz.search_recording_result", return_value=LookupResult()
    ), patch.object(db_session, "commit", wraps=db_session.commit) as commit:
        result = enrich_media_item(db_session, item, commit=False)

    assert result["status"] == "no_match"
    assert item.musicbrainz_lookup_status == "no_match"
    commit.assert_not_called()


def test_enrich_can_defer_committing_an_invalid_search_result(db_session):
    item = _make_item(db_session, artist="Artist")
    with patch(
        "app.enrichment.musicbrainz.search_recording_result",
        return_value=LookupResult(value={"artist": "Artist"}),
    ), patch.object(db_session, "commit", wraps=db_session.commit) as commit:
        result = enrich_media_item(db_session, item, commit=False)

    assert result["status"] == "no_match"
    commit.assert_not_called()


def test_enrich_prefers_an_embedded_isrc(db_session):
    item = _make_item(db_session, artist="Artist", musicbrainz_isrc="GBABC1234567")
    detail = {"id": "rec-isrc", "artist-credit": [], "releases": [], "genres": [], "tags": []}
    with (
        patch(
            "app.enrichment.musicbrainz.lookup_recording_by_isrc",
            return_value=LookupResult(value={"recording_mbid": "rec-isrc"}),
        ) as isrc_lookup,
        patch("app.enrichment.musicbrainz.search_recording_result") as title_lookup,
        patch(
            "app.enrichment.musicbrainz.fetch_recording_result",
            return_value=LookupResult(value=detail),
        ),
    ):
        result = enrich_media_item(db_session, item)

    assert result["metadata_saved"] is True
    isrc_lookup.assert_called_once_with("GBABC1234567")
    title_lookup.assert_not_called()


def test_enrich_falls_back_to_title_when_embedded_isrc_is_unknown(db_session):
    item = _make_item(db_session, artist="Artist", musicbrainz_isrc="GBABC1234567")
    detail = {"id": "rec-title", "artist-credit": [], "releases": [], "genres": [], "tags": []}
    with (
        patch(
            "app.enrichment.musicbrainz.lookup_recording_by_isrc",
            return_value=LookupResult(),
        ),
        patch(
            "app.enrichment.musicbrainz.search_recording_result",
            return_value=LookupResult(value={"recording_mbid": "rec-title"}),
        ) as title_lookup,
        patch(
            "app.enrichment.musicbrainz.fetch_recording_result",
            return_value=LookupResult(value=detail),
        ),
    ):
        result = enrich_media_item(db_session, item)

    assert result["metadata_saved"] is True
    title_lookup.assert_called_once_with("Artist", "Bohemian Rhapsody")


def test_enrich_uses_a_cautious_version_title_fallback(db_session):
    item = _make_item(db_session, artist="Artist", title="Song - Edit")
    detail = {"id": "rec-edit", "artist-credit": [], "releases": [], "genres": [], "tags": []}
    with (
        patch(
            "app.enrichment.musicbrainz.search_recording_result",
            side_effect=[LookupResult(), LookupResult(value={"recording_mbid": "rec-edit"})],
        ) as search,
        patch(
            "app.enrichment.musicbrainz.fetch_recording_result",
            return_value=LookupResult(value=detail),
        ),
    ):
        result = enrich_media_item(db_session, item)

    assert result["metadata_saved"] is True
    assert [call.args for call in search.call_args_list] == [
        ("Artist", "Song - Edit"),
        ("Artist", "Song"),
    ]




def test_enrich_keeps_the_version_fallback_error_for_retry(db_session):
    item = _make_item(db_session, artist="Artist", title="Song - Edit")
    with patch(
        "app.enrichment.musicbrainz.search_recording_result",
        side_effect=[LookupResult(error="first"), LookupResult(error="second")],
    ):
        result = enrich_media_item(db_session, item)

    assert result["status"] == "retry"
    assert item.musicbrainz_last_error == "first"


def test_enrich_records_retryable_provider_failures(db_session):
    item = _make_item(db_session, artist="Artist")
    with patch(
        "app.enrichment.musicbrainz.search_recording_result",
        return_value=LookupResult(error="MusicBrainz request failed: ReadTimeout"),
    ):
        result = enrich_media_item(db_session, item)

    assert result == {"matched": False, "metadata_saved": False, "status": "retry"}
    assert item.musicbrainz_lookup_status == "retry"
    assert item.musicbrainz_last_error == "MusicBrainz request failed: ReadTimeout"
    assert item.musicbrainz_last_attempt_at is not None


def test_enrich_records_detail_failures_for_retry(db_session):
    item = _make_item(db_session, artist="Artist", musicbrainz_recording_mbid="rec-1")
    with patch(
        "app.enrichment.musicbrainz.fetch_recording_result",
        return_value=LookupResult(error="MusicBrainz HTTP 503"),
    ):
        result = enrich_media_item(db_session, item)

    assert result["matched"] is True
    assert result["metadata_saved"] is False
    assert result["status"] == "retry"
    assert item.musicbrainz_last_error == "MusicBrainz HTTP 503"


def test_enrich_stores_full_musicbrainz_record_and_queryable_taxonomy(db_session):
    item = _make_item(db_session, artist="Artist", album="Album")
    detail = {
        "id": "rec-123",
        "artist-credit": [{"name": "Artist", "artist": {"id": "art-456"}}],
        "releases": [{"id": "rel-789", "title": "Album"}],
        "genres": [{"name": "rock"}, {"name": "alternative rock"}],
        "tags": [
            {"name": "rock", "count": 12},
            {"name": "driving", "count": 4},
        ],
        "isrcs": ["GBABC1234567"],
    }
    with (
        patch(
            "app.enrichment.musicbrainz.search_recording_result",
            return_value=LookupResult(
                value={"recording_mbid": "rec-123", "artist_mbid": "art-456"}
            ),
        ),
        patch(
            "app.enrichment.musicbrainz.fetch_recording_result",
            return_value=LookupResult(value=detail),
        ),
    ):
        result = enrich_media_item(db_session, item)

    assert result == {
        "matched": True,
        "metadata_saved": True,
        "status": "complete",
        "musicbrainz_recording_mbid": "rec-123",
        "genres": 2,
        "tags": 2,
    }
    assert {genre.name for genre in item.genres} == {"rock", "alternative rock"}
    assert [tag.name for tag in item.musicbrainz_tags] == ["driving"]
    assert db_session.query(MusicBrainzTag).one().name == "driving"
    assert item.musicbrainz_metadata_json["isrcs"] == ["GBABC1234567"]
    # Stored compact, with sorted keys, non-ASCII kept as is.
    stored = db_session.execute(
        text("SELECT musicbrainz_metadata_json FROM media_items WHERE id = :id"), {"id": item.id}
    ).scalar_one()
    assert '"isrcs":["GBABC1234567"]' in stored


def test_cover_art_is_fetched_from_cached_musicbrainz_release(db_session):
    item = _make_item(
        db_session,
        musicbrainz_metadata_json={"releases": [{"id": "rel-789", "release-group": {"id": "rg-1"}}]},
    )
    with patch(
        "app.enrichment.coverart.front_image",
        return_value=Found("https://example/art.jpg"),
    ) as fetch:
        result = fetch_cover_art_for_item(db_session, item, commit=False)
        assert result == {"matched": True, "fetched": True}
        assert item.cover_art_url == "https://example/art.jpg"
        # The release group is passed too, for another edition's cover.
        fetch.assert_called_once_with("rel-789", "rg-1")
        item.cover_art_url = None
        result = fetch_cover_art_for_item(db_session, item)
        assert result == {"matched": True, "fetched": True}
        assert fetch.call_count == 2


def test_cover_art_skips_missing_or_invalid_cached_metadata(db_session):
    item = _make_item(db_session)
    assert fetch_cover_art_for_item(db_session, item) == {"matched": False, "fetched": False}
    for stored in ("not json", "", "[]", "{}", "null"):
        # Written behind the ORM's back, as an old or damaged row would be.
        db_session.execute(
            text("UPDATE media_items SET musicbrainz_metadata_json = :v WHERE id = :id"),
            {"v": stored, "id": item.id},
        )
        db_session.commit()
        db_session.expire(item)
        assert fetch_cover_art_for_item(db_session, item) == {"matched": False, "fetched": False}


def test_enrich_can_defer_database_commit(db_session):
    item = _make_item(db_session, artist="Artist", album="Album")
    detail = {
        "id": "rec-deferred",
        "artist-credit": [{"name": "Artist", "artist": {"id": "art-456"}}],
        "releases": [],
        "genres": [],
        "tags": [],
    }
    with (
        patch(
            "app.enrichment.musicbrainz.search_recording_result",
            return_value=LookupResult(value={"recording_mbid": "rec-deferred"}),
        ),
        patch(
            "app.enrichment.musicbrainz.fetch_recording_result",
            return_value=LookupResult(value=detail),
        ),
    ):
        result = enrich_media_item(db_session, item, commit=False)

    assert result["matched"] is True
    assert item.musicbrainz_recording_mbid == "rec-deferred"
    db_session.commit()


def test_lyrics_prefers_embedded_over_lrclib(db_session, tmp_path):
    path = tmp_path / "song.wav"
    make_audio_fixture(path, title="Song", artist="A", album="X", genre="Rock")
    item = _make_item(db_session, relative_path="song.wav")

    with (
        patch(
            "app.enrichment.lyrics.get_embedded_lyrics",
            return_value="Embedded lyrics here",
        ),
        patch("app.enrichment.lyrics.fetch_lrclib_lyrics") as mock_lrclib,
    ):
        result = get_or_fetch_lyrics(db_session, item, path)

    assert result == {
        "plain": "Embedded lyrics here",
        "synced": None,
        "source": "embedded",
    }
    mock_lrclib.assert_not_called()
    assert item.lyrics_plain == "Embedded lyrics here"


def test_lyrics_falls_back_to_lrclib(db_session, tmp_path):
    path = tmp_path / "song.wav"
    item = _make_item(db_session)

    with (
        patch("app.enrichment.lyrics.get_embedded_lyrics", return_value=None),
        patch(
            "app.enrichment.lyrics.fetch_lrclib_lyrics",
            return_value={"plain": "Fetched plain", "synced": "[00:00.00] synced"},
        ),
    ):
        result = get_or_fetch_lyrics(db_session, item, path)

    assert result["source"] == "lrclib"
    assert item.lyrics_plain == "Fetched plain"
    assert item.lyrics_synced == "[00:00.00] synced"


def test_ignored_track_skips_external_lyrics_lookup_without_caching_a_miss(
    db_session, tmp_path
):
    path = tmp_path / "song.wav"
    item = _make_item(db_session)

    with (
        patch("app.enrichment.lyrics.get_embedded_lyrics", return_value=None),
        patch("app.enrichment.lyrics.fetch_lrclib_lyrics") as mock_lrclib,
    ):
        result = get_or_fetch_lyrics(
            db_session, item, path, allow_provider_fetch=False
        )

    assert result == {"plain": None, "synced": None, "source": None}
    mock_lrclib.assert_not_called()
    assert item.lyrics_fetched_at is None


def test_lyrics_uses_cache_on_second_call(db_session, tmp_path):
    path = tmp_path / "song.wav"
    item = _make_item(db_session, lyrics_plain="Already cached")

    with patch("app.enrichment.lyrics.get_embedded_lyrics") as mock_embedded:
        result = get_or_fetch_lyrics(db_session, item, path)

    assert result == {"plain": "Already cached", "synced": None, "source": "cached"}
    mock_embedded.assert_not_called()


def test_lyrics_records_fetch_attempt_even_when_nothing_found(db_session, tmp_path):
    path = tmp_path / "song.wav"
    item = _make_item(db_session)
    assert item.lyrics_fetched_at is None

    with (
        patch("app.enrichment.lyrics.get_embedded_lyrics", return_value=None),
        patch("app.enrichment.lyrics.fetch_lrclib_lyrics", return_value=None),
    ):
        result = get_or_fetch_lyrics(db_session, item, path)
    # None is lrclib failing: not a miss, so not remembered as one.
    assert result == {"plain": None, "synced": None, "source": None}
    assert item.lyrics_fetched_at is None

    with (
        patch("app.enrichment.lyrics.get_embedded_lyrics", return_value=None),
        patch("app.enrichment.lyrics.fetch_lrclib_lyrics", return_value={}),
    ):
        result = get_or_fetch_lyrics(db_session, item, path)

    assert result == {"plain": None, "synced": None, "source": None}
    assert item.lyrics_fetched_at is not None


def test_recent_lyrics_miss_is_negatively_cached(db_session, tmp_path):
    from app.models import utcnow

    path = tmp_path / "song.wav"
    item = _make_item(db_session)
    item.lyrics_fetched_at = utcnow()
    db_session.commit()

    with (
        patch("app.enrichment.lyrics.get_embedded_lyrics") as embedded,
        patch("app.enrichment.lyrics.fetch_lrclib_lyrics") as remote,
    ):
        result = get_or_fetch_lyrics(db_session, item, path)

    assert result == {"plain": None, "synced": None, "source": None}
    embedded.assert_not_called()
    remote.assert_not_called()


def test_listenbrainz_tags_are_additive_to_file_and_musicbrainz_metadata(db_session):
    from app.enrichment import apply_listenbrainz_tags
    from app.metadata_taxonomy import replace_item_genres, replace_item_musicbrainz_tags
    from app.models import MediaItemMusicBrainzTag

    item = _make_item(db_session, artist="Queen", genre="Rock")
    replace_item_genres(db_session, item, ["Rock"], "embedded")
    replace_item_musicbrainz_tags(db_session, item, [("glam", 3), ("anthem", 2)])
    db_session.commit()

    apply_listenbrainz_tags(db_session, item, [("rock", 10), ("glam", 8), ("epic", 7)])
    db_session.commit()

    assert item.genre == "Rock"
    assert item.online_tags_fetched_at is not None
    assert {g.name for g in item.genres} == {"Rock"}  # "rock" matched the known genre
    sources = {
        row.musicbrainz_tag_id: row.source
        for row in db_session.query(MediaItemMusicBrainzTag).filter_by(media_item_id=item.id)
    }
    names = {t.name: sources[t.id] for t in item.musicbrainz_tags}
    # The recording's own tag keeps its row ("glam"); the artist adds "epic".
    assert names == {"glam": "musicbrainz", "anthem": "musicbrainz", "epic": "listenbrainz"}

    # A later MusicBrainz refresh keeps the artist-only tag.
    replace_item_musicbrainz_tags(db_session, item, [("glam", 4)])
    db_session.commit()
    db_session.refresh(item)
    assert {t.name for t in item.musicbrainz_tags} == {"glam", "epic"}


def test_lyrics_embedded_lrc_is_returned_as_synced_and_cached_rows_are_repaired(db_session, tmp_path):
    path = tmp_path / "song.wav"
    item = _make_item(db_session)
    with patch(
        "app.enrichment.lyrics.get_embedded_lyrics",
        return_value="[00:15.69] Why won't they talk to me",
    ):
        result = get_or_fetch_lyrics(db_session, item, path)
    assert result == {
        "plain": "Why won't they talk to me",
        "synced": "[00:15.69] Why won't they talk to me",
        "source": "embedded",
    }

    # A row with LRC in lyrics_plain is repaired on read.
    item.lyrics_plain, item.lyrics_synced = "[00:01.00] Old row", None
    db_session.commit()
    cached = get_or_fetch_lyrics(db_session, item, path)
    assert cached == {"plain": "Old row", "synced": "[00:01.00] Old row", "source": "cached"}
