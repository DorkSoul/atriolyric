from __future__ import annotations

from unittest.mock import Mock, patch

from app.albums import rebuild_albums, section_for
from app.artist_credits import rebuild_all_credits
from app.metadata_providers import musicbrainz
from app.metadata_taxonomy import (
    replace_item_listenbrainz_tags,
    replace_item_musicbrainz_tags,
)
from app.models import (
    Album,
    Artist,
    LibraryRoot,
    MediaItem,
    MediaItemMusicBrainzTag,
    MusicBrainzTag,
)


def _recording(release_title: str, group_id: str, *, release_id: str, primary="Album", secondary=(), first="1999-05", date="2004", artists=(("Host", "mb-host"),)):
    return (
        {
            "id": f"rec-{release_id}",
            "artist-credit": [{"name": name, "artist": {"id": mbid, "name": name}} for name, mbid in artists],
            "releases": [
                {
                    "id": "other-release",
                    "title": "Greatest Hits",
                    "status": "Official",
                    "release-group": {"id": "rg-hits", "primary-type": "Album", "secondary-types": ["Compilation"]},
                },
                {
                    "id": release_id,
                    "title": release_title,
                    "status": "Official",
                    "date": date,
                    "country": "GB",
                    "label-info": [{"catalog-number": "CAT1", "label": {"name": "Label Co"}}],
                    "release-group": {
                        "id": group_id,
                        "primary-type": primary,
                        "secondary-types": list(secondary),
                        "first-release-date": first,
                    },
                },
            ],
        }
    )


def _root(db_session):
    root = LibraryRoot(path="/m", display_name="m")
    db_session.add(root)
    db_session.commit()
    return root


def _track(db_session, root, title, *, album="Debut", artist="Host", metadata=None, album_artist=None, release_date=None):
    item = MediaItem(
        library_root_id=root.id,
        relative_path=f"{title}.mp3",
        title=title,
        artist=artist,
        album=album,
        album_artist=album_artist,
        musicbrainz_metadata_json=metadata,
        release_date=release_date,
    )
    db_session.add(item)
    db_session.commit()
    return item


def test_release_group_is_voted_from_tracks_and_ignores_other_releases(db_session):
    root = _root(db_session)
    _track(db_session, root, "One", metadata=_recording("Debut", "rg-debut", release_id="rel-uk"))
    _track(db_session, root, "Two", metadata=_recording("Debut (Deluxe)", "rg-debut", release_id="rel-deluxe"))
    _track(db_session, root, "Three", metadata=_recording("Debut", "rg-debut", release_id="rel-uk"))
    _track(db_session, root, "Four")  # no MusicBrainz data yet
    rebuild_all_credits(db_session)
    rebuild_albums(db_session)

    album = db_session.query(Album).one()
    assert (album.owner, album.title) == ("Host", "Debut")
    assert album.release_group_mbid == "rg-debut"
    assert album.release_mbid == "rel-uk"
    assert album.primary_type == "Album"
    assert album.first_release_date == "1999-05-01"
    assert album.release_date == "2004-01-01"
    assert (album.country, album.label, album.catalog_number) == ("GB", "Label Co", "CAT1")
    assert {t.album_id for t in db_session.query(MediaItem)} == {album.id}
    assert db_session.query(Artist).filter_by(name="Host").one().mbid == "mb-host"


def test_albums_list_types_sections_and_original_date(client, db_session):
    root = _root(db_session)
    _track(db_session, root, "Live1", album="Live at X", metadata=_recording("Live at X", "rg-live", release_id="r1", secondary=["Live"], first="2010"), release_date="2015-01-01")
    _track(db_session, root, "Ep1", album="Small", metadata=_recording("Small", "rg-ep", release_id="r2", primary="EP", first="2001-02-03"))
    _track(db_session, root, "Plain", album="Untyped", release_date="2020-01-01")
    rebuild_all_credits(db_session)
    rebuild_albums(db_session)

    albums = {a["album"]: a for a in client.get("/api/library/albums", params={"artist": "Host"}).json()}
    assert albums["Live at X"]["section"] == "Live"
    assert albums["Live at X"]["secondary_types"] == ["Live"]
    assert albums["Live at X"]["release_date"] == "2010-01-01"
    assert albums["Small"]["section"] == "EP"
    assert albums["Untyped"]["section"] == "Album"
    assert albums["Untyped"]["release_date"] == "2020-01-01"

    newest = client.get(
        "/api/library/albums", params={"artist": "Host", "sort": "date_published"}
    ).json()
    assert [a["album"] for a in newest] == ["Untyped", "Live at X", "Small"]


def test_section_for():
    assert section_for("Album", ["Compilation"]) == "Compilation"
    assert section_for("Album", ["Live"]) == "Live"
    assert section_for("Single", []) == "Single"
    assert section_for("Broadcast", []) == "Other"
    assert section_for(None, []) == "Album"


def test_a_hand_fixed_artist_mbid_is_kept(db_session):
    root = _root(db_session)
    _track(db_session, root, "One", metadata=_recording("Debut", "rg", release_id="r"))
    rebuild_all_credits(db_session)
    artist = db_session.query(Artist).filter_by(name="Host").one()
    artist.mbid, artist.mbid_locked = "picked", True
    db_session.commit()
    rebuild_albums(db_session)
    assert db_session.query(Artist).filter_by(name="Host").one().mbid == "picked"


def _tags(db_session, item):
    return {
        (tag.name, row.source)
        for row, tag in db_session.query(MediaItemMusicBrainzTag, MusicBrainzTag)
        .join(MusicBrainzTag)
        .filter(MediaItemMusicBrainzTag.media_item_id == item.id)
    }


def test_the_recordings_own_tags_win_over_its_artists(db_session):
    root = _root(db_session)
    item = _track(db_session, root, "T", metadata={"id": "r", "tags": [{"name": "dreamy", "count": 3}]})
    replace_item_listenbrainz_tags(db_session, item, [("dreamy", 9), ("shoegaze", 4)])
    db_session.commit()
    assert _tags(db_session, item) == {("dreamy", "listenbrainz"), ("shoegaze", "listenbrainz")}

    # The recording's MusicBrainz tags take the shared one over...
    replace_item_musicbrainz_tags(db_session, item, [("dreamy", 3), ("90s", 1)])
    db_session.commit()
    assert _tags(db_session, item) == {("dreamy", "musicbrainz"), ("90s", "musicbrainz"), ("shoegaze", "listenbrainz")}

    # ...and a later artist-tag refresh never takes it back.
    replace_item_listenbrainz_tags(db_session, item, [("dreamy", 9)])
    db_session.commit()
    assert _tags(db_session, item) == {("dreamy", "musicbrainz"), ("90s", "musicbrainz")}


def test_browse_release_groups_pages_until_complete():
    pages = [
        {"release-group-count": 3, "release-groups": [{"id": "a"}, {"id": "b"}]},
        {"release-group-count": 3, "release-groups": [{"id": "c"}]},
    ]
    responses = [Mock(status_code=200, json=Mock(return_value=page), raise_for_status=Mock()) for page in pages]
    with patch.object(musicbrainz._throttle, "wait"), patch.object(
        musicbrainz._client, "get", side_effect=responses
    ) as get:
        result = musicbrainz.browse_release_groups("artist-mbid")
    assert [g["id"] for g in result.value["release-groups"]] == ["a", "b", "c"]
    assert get.call_args_list[1].kwargs["params"]["offset"] == "2"


def test_browse_release_groups_failure_is_not_partial():
    ok = Mock(status_code=200, json=Mock(return_value={"release-group-count": 5, "release-groups": [{"id": "a"}]}), raise_for_status=Mock())
    with patch.object(musicbrainz._throttle, "wait"), patch.object(
        musicbrainz._client, "get", side_effect=[ok, RuntimeError("down")]
    ):
        result = musicbrainz.browse_release_groups("artist-mbid")
    assert result.value is None and result.error
