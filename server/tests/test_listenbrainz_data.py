from __future__ import annotations

from unittest.mock import patch

import pytest

from app.albums import rebuild_albums
from app.artist_credits import rebuild_all_credits
from app.discovery import similar_tracks_for_seed
from app.external_cache import cached
from app.listenbrainz_data import online_allowed, similar_owned_weights
from app.metadata_info import credits_from_recording
from app.models import (
    Artist,
    ArtistMetadataCache,
    ExternalCache,
    LibraryRoot,
    MediaItem,
    User,
)
from app.owned_match import match_owned, normalize_title
from tests.conftest import DEFAULT_USERNAME, played_now


def _user(db_session) -> User:
    return db_session.query(User).filter_by(username=DEFAULT_USERNAME).one()


def _link_listenbrainz(db_session, token: str = "TOKEN") -> None:
    """Top recordings need a ListenBrainz user token: link one for the test user."""
    from app import scrobbling

    with patch("app.scrobbling.listenbrainz.validate_token", return_value="me"):
        assert scrobbling.link_listenbrainz(db_session, _user(db_session), token) is None


def _library(db_session):
    root = LibraryRoot(path="/m", display_name="m")
    db_session.add(root)
    db_session.commit()
    rows = [
        ("Seed Song", "Seed Band", "Seed LP", "00000001-0000-4000-8000-000000000001"),
        ("Close Match", "Other Band", "Other LP", "00000002-0000-4000-8000-000000000002"),
        ("Far Match (2011 Remaster)", "Far Band", "Far LP", None),
        ("Unrelated", "Nobody", "Nothing", "00000003-0000-4000-8000-000000000003"),
    ]
    for index, (title, artist, album, mbid) in enumerate(rows):
        db_session.add(
            MediaItem(
                library_root_id=root.id,
                relative_path=f"{index}.mp3",
                title=title,
                artist=artist,
                album=album,
                musicbrainz_recording_mbid=mbid,
            )
        )
    db_session.commit()
    rebuild_all_credits(db_session)
    rebuild_albums(db_session)
    return root


def _pin_artist(db_session, name: str = "Seed Band", mbid: str = "00000004-0000-4000-8000-000000000004") -> Artist:
    artist = db_session.query(Artist).filter_by(name=name).one()
    artist.mbid = mbid
    db_session.commit()
    return artist


SIMILAR = [
    {"artist": "Unowned", "title": "Not Here", "mbid": None, "match": 1.0},
    {"artist": "Wrong Name", "title": "Close Match", "mbid": "00000002-0000-4000-8000-000000000002", "match": 0.9},
    {"artist": "far band", "title": "Far Match", "mbid": None, "match": 0.2},
]


def test_normalize_title_ignores_remasters_and_feats_but_not_live():
    assert normalize_title("Song - 2011 Remaster") == normalize_title("song")
    assert normalize_title("Song (feat. X)") == "song"
    assert normalize_title("Rock & Roll") == "rock and roll"
    assert normalize_title("Song (Live)") != normalize_title("Song")


@pytest.mark.parametrize(
    "title",
    [
        "Song (2011 Remaster)",
        "Song - 2011 Remaster",
        "Song - Remastered 2009",
        "Song (Remastered)",
        "Song [2009 Digital Remaster]",
        "Song (feat. X)",
        "Song (ft. X & Y)",
        "Song [featuring X]",
        "Song (feat. Take That)",
        "Song (feat. 2 Live Crew)",
        "Song (Mono)",
        "Song (Stereo)",
        "Song - Mono",
        "Song (2018 Stereo Mix)",
        "Song (Mono; 2014 Remaster)",
        "Song (Explicit)",
        "Song (Clean)",
        "Song (Deluxe)",
        "Song (Bonus Track)",
        "Song (Single Version)",
        "Song - Single Version",
        "Song - Album Version",
        "Song (feat. X) [2011 Remaster]",
        "Song (2011 Remaster) - Single Version",
        "Song - Single Version - 2011 Remaster",
    ],
)
def test_pure_version_notes_are_dropped(title):
    assert normalize_title(title) == "song"


@pytest.mark.parametrize(
    "title",
    [
        "Song (Live at Wembley; 2011 Remaster)",
        "Song - Live at Wembley / 2011 Remaster",
        "Song (Remix feat. X)",
        "Song (Clean Bandit Remix)",
        "Song (Live in Stereo)",
        "Song (Acoustic; 2011 Remaster)",
        "Song (Demo - Mono)",
        "Song (Instrumental) [Remastered]",
        "Song - Radio Edit - 2011 Remaster",
        "Song (Karaoke Version; Stereo)",
        "Song (BBC Session, Mono)",
        "Song (Cover) (feat. X)",
        "Song (feat. X; Live)",
        "Song (feat. X - Y Remix)",
        "Song - Extended Mix - Remastered",
        "Song (X Remix) [feat. Y]",
        "Song (Live)",
        "Song (Remix)",
    ],
)
def test_a_version_note_beside_another_performance_keeps_the_song_distinct(title):
    key = normalize_title(title)
    assert key and key != "song" and key.startswith("song ")


def test_a_version_note_is_dropped_from_a_live_or_remix_title_without_merging_it():
    """The remaster of a live take is that live take, not the studio song."""
    assert normalize_title("Song - Live - 2011 Remaster") == normalize_title("Song - Live") == "song live"
    assert normalize_title("Song (Live) [2011 Remaster]") == normalize_title("Song (Live)") == "song live"
    assert normalize_title("Song (X Remix) [feat. Y]") == normalize_title("Song (X Remix)")
    # Only the last " - " part is the note: the title's own dash stays.
    assert normalize_title("Love - Hate - Remastered 2009") == "love hate"
    # The words alone are not a note.
    assert normalize_title("Mono") == "mono"
    assert normalize_title("Clean") == "clean"
    assert normalize_title("Live and Let Die (2018 Remaster)") == "live and let die"


def test_titles_of_punctuation_only_keep_a_key_of_their_own():
    keys = {normalize_title(title) for title in ("+", "÷", "×", "=", "!!!", "?")}
    assert len(keys) == 6 and "" not in keys
    assert normalize_title("÷ (Deluxe)") == normalize_title(" ÷ ") == "÷"
    assert normalize_title("(Remastered)") != ""
    assert normalize_title(None) == normalize_title("") == normalize_title("   ") == ""


def test_match_owned_never_matches_by_an_empty_or_punctuation_title(db_session):
    root = LibraryRoot(path="/p", display_name="p")
    db_session.add(root)
    db_session.commit()
    for index, title in enumerate(["+", "   ", "Song (Live at Wembley; 2011 Remaster)"]):
        db_session.add(MediaItem(library_root_id=root.id, relative_path=f"{index}.mp3", title=title, artist="Ed"))
    db_session.commit()
    rebuild_all_credits(db_session)
    rows = [
        {"artist": "Ed", "title": "÷", "mbid": None},
        {"artist": "Ed", "title": "", "mbid": None},
        {"artist": "Ed", "title": "Song", "mbid": None},
        {"artist": "Ed", "title": "+", "mbid": None},
        {"artist": "Ed", "title": "Song - Live at Wembley", "mbid": None},
    ]
    matches = match_owned(db_session, [root.id], rows)
    titles = [db_session.get(MediaItem, m).title if m else None for m in matches]
    assert titles == [None, None, None, "+", None]


def test_match_owned_by_mbid_then_artist_and_title(db_session):
    root = _library(db_session)
    matches = match_owned(db_session, [root.id], SIMILAR)
    titles = [db_session.get(MediaItem, m).title if m else None for m in matches]
    assert titles == [None, "Close Match", "Far Match (2011 Remaster)"]


def test_similar_weights_are_cached_and_autoplay_reads_only_the_cache(db_session):
    root = _library(db_session)
    seed = db_session.query(MediaItem).filter_by(title="Seed Song").one()
    with patch("app.listenbrainz_data.listenbrainz.similar_recordings", return_value=SIMILAR) as fetch:
        weights = similar_owned_weights(db_session, [root.id], [seed], True)
        again = similar_owned_weights(db_session, [root.id], [seed], True)
    fetch.assert_called_once_with("00000001-0000-4000-8000-000000000001")
    assert weights == again
    close = db_session.query(MediaItem).filter_by(title="Close Match").one()
    far = db_session.query(MediaItem).filter_by(title="Far Match (2011 Remaster)").one()
    assert weights[close.id] > weights[far.id] > 1.0

    other = db_session.query(MediaItem).filter_by(title="Unrelated").one()
    with patch("app.listenbrainz_data.listenbrainz.similar_recordings") as fetch:
        assert similar_owned_weights(db_session, [root.id], [other], True, allow_fetch=False) == {}
    fetch.assert_not_called()


def test_similar_weights_need_a_recording_mbid_and_the_feature_on(db_session):
    root = _library(db_session)
    far = db_session.query(MediaItem).filter_by(title="Far Match (2011 Remaster)").one()
    seed = db_session.query(MediaItem).filter_by(title="Seed Song").one()
    with patch("app.listenbrainz_data.listenbrainz.similar_recordings", return_value=SIMILAR) as fetch:
        assert similar_owned_weights(db_session, [root.id], [far], True) == {}
        assert similar_owned_weights(db_session, [root.id], [seed], False) == {}
    fetch.assert_not_called()


def test_online_allowed_follows_the_privacy_switch(db_session):
    user = _user(db_session)
    assert online_allowed(user) is True and online_allowed(None) is True
    user.online_recommendations = False
    db_session.commit()
    assert online_allowed(user) is False


def test_song_mix_draws_listenbrainz_similar_tracks_from_other_artists(db_session):
    _library(db_session)
    user = _user(db_session)
    seed = db_session.query(MediaItem).filter_by(title="Seed Song").one()
    with patch("app.listenbrainz_data.listenbrainz.similar_recordings", return_value=SIMILAR), patch(
        "app.discovery.similar.listenbrainz.similar_artists", return_value=[]
    ):
        ids = similar_tracks_for_seed(db_session, user, seed.id, boost_likes=True)
    titles = {db_session.get(MediaItem, media_id).title for media_id in ids}
    assert titles == {"Seed Song", "Close Match", "Far Match (2011 Remaster)"}


def test_similar_artists_are_asked_by_mbid_and_cached(db_session):
    from app.discovery.similar import _similar_artist_match_scores, _similar_artists_for

    _library(db_session)
    _pin_artist(db_session)
    rows = [{"name": "Far Band", "mbid": "00000005-0000-4000-8000-000000000005", "match": 1.0}, {"name": "Stranger", "mbid": None, "match": 0.25}]
    with patch("app.discovery.similar.listenbrainz.similar_artists", return_value=rows) as fetch:
        assert _similar_artists_for(db_session, "Seed Band", True) == {"Far Band", "Stranger"}
        assert _similar_artists_for(db_session, "Seed Band", True) == {"Far Band", "Stranger"}
    fetch.assert_called_once_with("00000004-0000-4000-8000-000000000004")
    cache = db_session.get(ArtistMetadataCache, "Seed Band")
    assert cache is not None and cache.mbid == "00000004-0000-4000-8000-000000000004"
    assert _similar_artist_match_scores(db_session, ["Seed Band"]) == {"Far Band": 1.0, "Stranger": 0.25}
    # The feature off: nothing is asked (what is cached still counts).
    with patch("app.discovery.similar.listenbrainz.similar_artists") as fetch:
        assert _similar_artists_for(db_session, "Nobody", False) == set()
    fetch.assert_not_called()


def test_similar_artists_without_an_mbid_ask_nothing(db_session):
    from app.discovery.similar import _similar_artists_for

    _library(db_session)
    # Mixes never wait on a MusicBrainz name search either.
    with patch(
        "app.metadata_info.musicbrainz.search_artists", side_effect=AssertionError("searched MusicBrainz")
    ), patch("app.discovery.similar.listenbrainz.similar_artists") as fetch:
        assert _similar_artists_for(db_session, "Seed Band", True) == set()
    fetch.assert_not_called()


TOP = [
    {"title": "Seed Song", "mbid": None, "playcount": 900, "listeners": 50, "rank": 1},
    {"title": "Their Hit", "mbid": None, "playcount": 800, "listeners": 40, "rank": 2},
]


def test_top_tracks_mark_owned_and_missing(client, db_session):
    _library(db_session)
    _pin_artist(db_session)
    _link_listenbrainz(db_session)
    with patch("app.listenbrainz_data.listenbrainz.top_recordings_for_artist", return_value=TOP) as fetch:
        body = client.get("/api/library/artist-top-tracks", params={"name": "Seed Band"}).json()
    fetch.assert_called_once_with("00000004-0000-4000-8000-000000000004", "TOKEN")
    assert body["available"] is True
    assert [row["title"] for row in body["tracks"]] == ["Seed Song", "Their Hit"]
    assert body["tracks"][0]["media_item"]["title"] == "Seed Song"
    assert body["tracks"][1]["media_item"] is None
    assert body["tracks"][0]["playcount"] == 900


def test_top_tracks_use_an_mbid_a_track_carries(client, db_session):
    _library(db_session)
    seed = db_session.query(MediaItem).filter_by(title="Seed Song").one()
    seed.musicbrainz_artist_mbid = "00000006-0000-4000-8000-000000000006"
    db_session.commit()
    _link_listenbrainz(db_session)
    with patch("app.listenbrainz_data.listenbrainz.top_recordings_for_artist", return_value=TOP) as fetch:
        body = client.get("/api/library/artist-top-tracks", params={"name": "Seed Band"}).json()
    fetch.assert_called_once_with("00000006-0000-4000-8000-000000000006", "TOKEN")
    assert body["available"] is True


def test_top_tracks_without_a_listenbrainz_token_ask_nothing(client, db_session):
    """ListenBrainz refuses top recordings without a user token: with none
    linked the section is unavailable and nothing is asked (so the artist
    info task never counts it as a provider failure); what is cached still
    shows."""
    _library(db_session)
    _pin_artist(db_session)
    with patch("app.listenbrainz_data.listenbrainz.top_recordings_for_artist") as fetch:
        body = client.get("/api/library/artist-top-tracks", params={"name": "Seed Band", "refresh": True}).json()
        assert body == {"available": False, "pending": False, "tracks": []}
        body = client.get("/api/library/artist-top-tracks", params={"name": "Seed Band"}).json()
        assert body == {"available": False, "pending": False, "tracks": []}
        cached(db_session, "listenbrainz", "top-recordings", "00000004-0000-4000-8000-000000000004", lambda: TOP)
        body = client.get("/api/library/artist-top-tracks", params={"name": "Seed Band"}).json()
    fetch.assert_not_called()
    assert body["available"] is True and [row["title"] for row in body["tracks"]] == ["Seed Song", "Their Hit"]


def test_a_refused_or_unreadable_token_is_not_sent(db_session):
    from app import scrobbling
    from app.scrobbling import listenbrainz_token

    user = _user(db_session)
    assert listenbrainz_token(db_session, None) is None
    assert listenbrainz_token(db_session, user) is None
    _link_listenbrainz(db_session)
    assert listenbrainz_token(db_session, user) == "TOKEN"
    scrobbling._mark_revoked(db_session, user.id)
    assert listenbrainz_token(db_session, user) is None


def test_top_tracks_without_an_mbid(client, db_session):
    _library(db_session)
    with patch(
        "app.metadata_info.musicbrainz.search_artists",
        return_value=type("R", (), {"error": None, "value": {"artists": []}})(),
    ):
        body = client.get("/api/library/artist-top-tracks", params={"name": "Seed Band"}, ).json()
    assert body == {"available": False, "pending": False, "tracks": []}


MB_ARTIST = {
    "id": "00000004-0000-4000-8000-000000000004",
    "name": "Seed Band",
    "type": "Group",
    "country": "GB",
    "life-span": {"begin": "1990", "ended": False},
    "area": {"name": "United Kingdom"},
    "relations": [
        {"type": "member of band", "direction": "backward", "target-type": "artist", "artist": {"id": "m1", "name": "Other Band"}, "begin": "1990"},
        {"type": "official homepage", "target-type": "url", "url": {"resource": "https://seed.example"}},
        {"type": "wikidata", "target-type": "url", "url": {"resource": "https://www.wikidata.org/wiki/Q1"}},
    ],
    "genres": [{"name": "shoegaze", "count": 3}],
}


def _mb(value):
    return type("R", (), {"error": None, "value": value})()


def test_artist_info_merges_musicbrainz_and_listenbrainz(client, db_session):
    _library(db_session)
    similar_rows = [
        {"name": "Far Band", "mbid": None, "match": 1.0},
        {"name": "Stranger", "mbid": None, "match": 0.4},
    ]
    search = {"artists": [{"id": "00000004-0000-4000-8000-000000000004", "name": "Seed Band", "score": 100}]}
    with patch("app.metadata_info.musicbrainz.search_artists", return_value=_mb(search)), patch(
        "app.metadata_info.musicbrainz.lookup_artist", return_value=_mb(MB_ARTIST)
    ), patch(
        "app.listenbrainz_data.listenbrainz.artist_popularity",
        return_value={"00000004-0000-4000-8000-000000000004": {"playcount": 99999, "listeners": 1234}},
    ), patch(
        "app.listenbrainz_data.listenbrainz.similar_artists", return_value=similar_rows
    ) as similar, patch(
        "app.metadata_info.wikipedia.summary_from_links",
        return_value={"text": "From the encyclopedia", "url": "https://en.wikipedia.org/wiki/Seed"},
    ):
        body = client.get("/api/library/artist-info", params={"name": "Seed Band"}).json()
    # Similar artists are asked by MBID for any artist, not only Discovery's seeds.
    similar.assert_called_once_with("00000004-0000-4000-8000-000000000004")
    assert body["mbid"] == "00000004-0000-4000-8000-000000000004"
    assert body["bio"] == {
        "text": "From the encyclopedia",
        "html": False,
        "source": "Wikipedia",
        "url": "https://en.wikipedia.org/wiki/Seed",
    }
    assert (body["listeners"], body["playcount"]) == (1234, 99999)
    assert body["listenbrainz_url"] == "https://listenbrainz.org/artist/00000004-0000-4000-8000-000000000004/"
    assert body["type"] == "Group" and body["country"] == "GB" and body["begin"] == "1990"
    assert body["members"] == [
        {"name": "Other Band", "mbid": "m1", "begin": "1990", "end": None, "ended": False, "attributes": [], "in_library": True}
    ]
    assert {link["label"] for link in body["links"]} == {"Official site", "Wikidata"}
    assert body["similar"] == [
        {"name": "Far Band", "in_library": True},
        {"name": "Stranger", "in_library": False},
    ]
    assert body["tags"] == ["shoegaze"]
    assert body["sources"] == ["MusicBrainz", "ListenBrainz"]
    assert body["image_credit"] is None


def test_artist_info_credits_the_photo_that_is_shown(client, db_session):
    from app.models import CustomArt

    _library(db_session)
    _pin_artist(db_session)
    credit = {
        "author": "Pho Tographer", "license": "CC BY 4.0",
        "license_url": "https://creativecommons.org/licenses/by/4.0", "page_url": "https://commons.wikimedia.org/wiki/File:A.jpg",
    }
    # The artist's art came from the art picker's Commons candidate.
    db_session.add(CustomArt(
        entity_type="artist", entity_key="Seed Band", path="a.jpg", thumb_path="a.t.jpg",
        source="wikimedia", credit_json=credit,
    ))
    db_session.commit()
    with patch("app.metadata_info.musicbrainz.lookup_artist", return_value=_mb(MB_ARTIST)), patch(
        "app.listenbrainz_data.listenbrainz.artist_popularity", return_value={}
    ), patch("app.metadata_info.wikipedia.summary_from_links", return_value={}):
        body = client.get("/api/library/artist-info", params={"name": "Seed Band"}).json()
    assert body["image_credit"] == credit
    # Art from elsewhere (a folder image, an upload) carries no credit.
    art = db_session.query(CustomArt).one()
    art.source, art.credit_json = "local", None
    db_session.commit()
    with patch("app.metadata_info.musicbrainz.lookup_artist", return_value=_mb(MB_ARTIST)), patch(
        "app.metadata_info.wikipedia.summary_from_links", return_value={}
    ):
        assert client.get("/api/library/artist-info", params={"name": "Seed Band"}).json()["image_credit"] is None


def test_an_ambiguous_musicbrainz_name_saves_no_mbid(client, db_session):
    """MusicBrainz finds two artists of this name and will not guess; once
    it has a clear answer, that is what is saved."""
    from app.metadata_info import resolve_artist_mbid

    _library(db_session)
    with patch(
        "app.metadata_info.musicbrainz.search_artists",
        return_value=_mb({"artists": [{"id": "mb-one", "name": "Seed Band", "score": 100}, {"id": "mb-two", "name": "Seed Band", "score": 98}]}),
    ), patch("app.metadata_info.musicbrainz.lookup_artist", side_effect=AssertionError("looked up a guess")):
        body = client.get("/api/library/artist-info", params={"name": "Seed Band"}).json()
    assert body["mbid"] is None and body["listeners"] is None
    artist = db_session.query(Artist).filter_by(name="Seed Band").one()
    db_session.refresh(artist)
    assert artist.mbid is None and artist.mbid_locked is False
    db_session.query(ExternalCache).filter_by(provider="musicbrainz").delete()
    db_session.commit()
    with patch(
        "app.metadata_info.musicbrainz.search_artists",
        return_value=_mb({"artists": [{"id": "mb-one", "name": "Seed Band", "score": 100}, {"id": "mb-two", "name": "Seed Band", "score": 60}]}),
    ):
        assert resolve_artist_mbid(db_session, artist) == "mb-one"


def test_fix_match_pins_the_artist(client, db_session):
    _library(db_session)
    with patch(
        "app.metadata_info.musicbrainz.lookup_artist",
        return_value=type("R", (), {"error": None, "value": {**MB_ARTIST, "id": "00000000-0000-4000-8000-0000000000ff"}})(),
    ):
        body = client.put("/api/library/artist-match", json={"name": "Seed Band", "mbid": "00000000-0000-4000-8000-0000000000ff"}).json()
    assert body["mbid"] == "00000000-0000-4000-8000-0000000000ff" and body["mbid_locked"] is True


def test_fix_match_and_choose_edition_drop_cached_listings(client, db_session):
    """Both change fields the cached album/artist listings show."""
    from app import library_cache

    _library(db_session)
    with patch(
        "app.metadata_info.musicbrainz.lookup_artist",
        return_value=type("R", (), {"error": None, "value": MB_ARTIST})(),
    ):
        library_cache.get_or_compute(("probe",), lambda: "stale")
        client.put("/api/library/artist-match", json={"name": "Seed Band", "mbid": "00000000-0000-4000-8000-0000000000ff"})
        assert library_cache.get_or_compute(("probe",), lambda: "fresh") == "fresh"

    library_cache.get_or_compute(("probe2",), lambda: "stale")
    with patch("app.metadata_info.album_editions", return_value=[]):
        resp = client.put("/api/library/album-edition", json={"artist": "Seed Band", "album": "Seed LP"})
    assert resp.status_code == 200
    assert library_cache.get_or_compute(("probe2",), lambda: "fresh") == "fresh"


def test_recording_credits_include_work_writers():
    recording = {
        "relations": [
            {"target-type": "artist", "type": "producer", "artist": {"name": "Pro Ducer"}},
            {"target-type": "artist", "type": "instrument", "attributes": ["guitar"], "artist": {"name": "Six String"}},
            {
                "target-type": "work",
                "type": "performance",
                "work": {
                    "title": "The Work",
                    "relations": [{"target-type": "artist", "type": "composer", "artist": {"name": "Com Poser"}}],
                },
            },
        ]
    }
    assert credits_from_recording(recording) == [
        {"role": "Producer", "name": "Pro Ducer", "detail": None},
        {"role": "Instruments", "name": "Six String", "detail": "guitar"},
        {"role": "Composer", "name": "Com Poser", "detail": None},
    ]


def test_track_info_endpoint(client, db_session):
    _library(db_session)
    item = db_session.query(MediaItem).filter_by(title="Close Match").one()
    with patch(
        "app.listenbrainz_data.listenbrainz.recording_popularity",
        return_value={"00000002-0000-4000-8000-000000000002": {"playcount": 70, "listeners": 7}},
    ):
        body = client.get(f"/api/library/media/{item.id}/info").json()
    assert body["recording_mbid"] == "00000002-0000-4000-8000-000000000002"
    assert (body["playcount"], body["listeners"]) == (70, 7)
    assert body["listenbrainz_url"] == "https://listenbrainz.org/track/00000002-0000-4000-8000-000000000002/"
    assert body["library_playcount"] == 0
    assert body["wiki"] is None
    assert "lastfm_available" not in body and "user_lastfm_playcount" not in body


def test_track_info_without_a_recording_mbid_asks_nothing(client, db_session):
    _library(db_session)
    item = db_session.query(MediaItem).filter_by(title="Far Match (2011 Remaster)").one()
    with patch("app.listenbrainz_data.listenbrainz.recording_popularity") as fetch:
        body = client.get(f"/api/library/media/{item.id}/info").json()
    fetch.assert_not_called()
    assert body["playcount"] is None and body["listenbrainz_url"] is None
    assert body["tags"] == []


def test_cache_keeps_stale_answer_when_a_refresh_fails(db_session):
    assert cached(db_session, "p", "k", "x", lambda: {"v": 1}) == {"v": 1}
    row = db_session.get(ExternalCache, ("p", "k", "x"))
    assert row is not None
    assert cached(db_session, "p", "k", "x", lambda: None, refresh=True) == {"v": 1}
    assert cached(db_session, "p", "k", "x", lambda: {"v": 2}, refresh=True) == {"v": 2}


def test_starting_a_song_prefetches_its_similar_tracks(client, db_session):
    _library(db_session)
    item = db_session.query(MediaItem).first()
    with patch("app.routers.stream.prefetch_similar_in_background") as prefetch, patch(
        "app.routers.stream.scrobble_track"
    ):
        assert client.post(f"/api/stream/{item.id}/played", json=played_now()).status_code == 200
    prefetch.assert_called_once_with(item.id)


def test_no_prefetch_for_a_user_who_turned_it_off(client, db_session):
    _library(db_session)
    user = _user(db_session)
    user.online_recommendations = False
    db_session.commit()
    item = db_session.query(MediaItem).first()
    with patch("app.routers.stream.prefetch_similar_in_background") as prefetch, patch(
        "app.routers.stream.scrobble_track"
    ):
        assert client.post(f"/api/stream/{item.id}/played", json=played_now()).status_code == 200
    prefetch.assert_not_called()
