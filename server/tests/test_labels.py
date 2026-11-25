"""GET /api/library/labels: genres and tags for one song, or pooled across an
album's or artist's tracks."""

from __future__ import annotations

from app.models import (
    Genre,
    LibraryRoot,
    MediaItem,
    MediaItemGenre,
    MediaItemMusicBrainzTag,
    MediaKind,
    MusicBrainzTag,
    Tag,
)


def _seed(db_session):
    root = LibraryRoot(path="/labels", display_name="Labels")
    db_session.add(root)
    db_session.commit()
    items = []
    for i, (artist, album) in enumerate([("A", "One"), ("A", "One"), ("B", "Two")]):
        item = MediaItem(
            library_root_id=root.id,
            kind=MediaKind.track,
            relative_path=f"t{i}.mp3",
            title=f"T{i}",
            artist=artist,
            album=album,
            genre="rock" if i == 0 else None,
        )
        db_session.add(item)
        items.append(item)
    db_session.commit()
    rock = Genre(name="Rock", normalized_name="rock")
    tag = MusicBrainzTag(name="chill", normalized_name="chill")
    mine = Tag(name="mine")
    db_session.add_all([rock, tag, mine])
    db_session.commit()
    for item in items[:2]:
        db_session.add(MediaItemGenre(media_item_id=item.id, genre_id=rock.id, source="embedded"))
        db_session.add(MediaItemMusicBrainzTag(media_item_id=item.id, musicbrainz_tag_id=tag.id))
    items[0].tags.append(mine)
    db_session.commit()
    return items


def test_labels_for_one_song(client, db_session):
    items = _seed(db_session)
    body = client.get("/api/library/labels", params={"media_item_id": items[0].id}).json()
    # The legacy "rock" column counts only for a track without normalised
    # genres (app.library_query.genre_rows), so the canonical name shows.
    assert body["genres"] == [{"name": "Rock", "track_count": 1}]
    assert {t["name"] for t in body["tags"]} == {"chill", "mine"}


def test_labels_pooled_for_album_and_artist(client, db_session):
    _seed(db_session)
    album = client.get("/api/library/labels", params={"artist": "A", "album": "One"}).json()
    assert album["genres"] == [{"name": "Rock", "track_count": 2}]
    assert album["tags"][0] == {"name": "chill", "track_count": 2}
    other = client.get("/api/library/labels", params={"artist": "B"}).json()
    assert other == {"genres": [], "tags": []}


def test_labels_requires_a_target(client):
    assert client.get("/api/library/labels").status_code == 422


def test_tracks_can_be_filtered_by_tag(client, db_session):
    items = _seed(db_session)
    by_provider = client.get("/api/library/tracks", params={"tag": "Chill"}).json()
    assert {t["id"] for t in by_provider} == {items[0].id, items[1].id}
    by_local = client.get("/api/library/tracks", params={"tag": "mine"}).json()
    assert [t["id"] for t in by_local] == [items[0].id]
    assert client.get("/api/library/tracks", params={"tag": "nope"}).json() == []


def test_all_labels_lists_every_genre_and_tag_with_its_tracks(client, db_session):
    items = _seed(db_session)
    first, second = items[0].id, items[1].id
    assert client.get("/api/library/labels/all").json() == {
        "genres": {"Rock": [first, second]},
        "tags": {"chill": [first, second], "mine": [first]},
    }
