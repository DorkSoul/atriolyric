"""Coherent automatic mixes, the shared cohesion ordering, and specified
mixes with include/exclude labels."""

from __future__ import annotations

import json
import random
from collections import Counter
from types import SimpleNamespace

import pytest

from app.discovery import (
    ADD_MORE_TRACKS_COUNT,
    add_more_tracks_to_auto_playlist,
    add_specified_mix_playlist,
    parse_mix_selector,
    refresh_all_genre_mixes,
    selector_display_name,
)
from app.discovery.labels import _order_by_cohesion
from app.discovery.sampling import _balanced_track_sample
from app.models import (
    LibraryRoot,
    MediaItem,
    MediaKind,
    MusicBrainzTag,
    Tag,
    User,
)


def _root(db_session) -> LibraryRoot:
    root = LibraryRoot(path="/cohesion", display_name="Cohesion")
    db_session.add(root)
    db_session.commit()
    return root


def _tracks(db_session, root, prefix, count, *, genre=None, tags=()) -> list[MediaItem]:
    items = []
    for i in range(count):
        item = MediaItem(
            library_root_id=root.id,
            kind=MediaKind.track,
            relative_path=f"{prefix}-{i}.mp3",
            title=f"{prefix} {i}",
            artist=f"{prefix} artist {i}",
            genre=genre,
        )
        item.tags.extend(tags)
        db_session.add(item)
        items.append(item)
    db_session.commit()
    return items


def _fake(item_id, artist, *labels):
    return SimpleNamespace(
        id=item_id,
        artist=artist,
        genre=None,
        genres=[],
        tags=[SimpleNamespace(name=label) for label in labels],
        musicbrainz_tags=[],
    )


def test_cohesion_ordering_keeps_similar_tracks_together():
    for _ in range(20):
        items = [_fake(i, f"a{i}", "rock", "indie") for i in range(4)] + [
            _fake(10 + i, f"b{i}", "jazz", "swing") for i in range(4)
        ]
        ordered = [item.id < 10 for item in _order_by_cohesion(items)]
        assert len(ordered) == 8
        assert sum(a != b for a, b in zip(ordered, ordered[1:])) == 1


def test_cohesion_ordering_spreads_one_artist_when_labels_tie():
    items = [_fake(1, "same", "x"), _fake(2, "same", "x"), _fake(3, "other", "x")]
    for _ in range(20):
        ordered = _order_by_cohesion(items)
        assert [item.artist for item in ordered][1] != ordered[0].artist or (
            ordered[0].artist == "other"
        )


def test_cohesion_ordering_keeps_a_strongly_matching_artist_from_clumping():
    # "star" tracks share five labels with each other; everyone else only three.
    star_labels = ("a", "b", "c", "d", "e")
    items = [_fake(i, "star", *star_labels) for i in range(6)] + [
        _fake(10 + i, f"other{i}", "a", "b", "c") for i in range(6)
    ]
    for _ in range(50):
        artists = [item.artist for item in _order_by_cohesion(items)]
        assert all(a != b for a, b in zip(artists, artists[1:])), artists


def test_cohesion_ordering_allows_a_run_when_one_artist_is_all_there_is():
    items = [_fake(i, "Solo ", "x") for i in range(5)] + [_fake(9, "solo", "x")]
    assert len(_order_by_cohesion(items)) == 6


def test_cohesion_ordering_ignores_missing_artists():
    items = [_fake(i, None, "x") for i in range(4)]
    assert len(_order_by_cohesion(items)) == 4


def test_sampling_caps_tracks_per_artist_while_others_remain():
    star = [SimpleNamespace(id=i, artist="star") for i in range(10)]
    others = [SimpleNamespace(id=100 + i, artist=f"o{i}") for i in range(20)]
    weights = {item.id: 3.0 for item in star}
    for _ in range(30):
        chosen = _balanced_track_sample(star + others, 12, weights=weights, max_per_artist=3)
        assert len(chosen) == 12
        assert sum(item.artist == "star" for item in chosen) <= 3


def test_sampling_cap_is_lifted_when_there_are_too_few_artists():
    items = [SimpleNamespace(id=i, artist=f"a{i % 2}") for i in range(20)]
    for _ in range(10):
        chosen = _balanced_track_sample(items, 8, max_per_artist=3)
        assert len(chosen) == 8
        assert len({item.id for item in chosen}) == 8


def test_weighted_sampling_favours_heavier_tracks():
    items = [SimpleNamespace(id=i, artist=f"artist {i}") for i in range(21)]
    counts = Counter()
    for _ in range(4000):
        counts[_balanced_track_sample(items, 1, weights={0: 2.0})[0].id] += 1
    light = sum(counts[i] for i in range(1, 21)) / 20
    assert counts[0] > 1.4 * light


def test_automatic_mix_only_uses_primary_tracks_when_there_are_enough(db_session):
    user = db_session.query(User).one()
    root = _root(db_session)
    indie = Tag(name="Indie")
    unrelated = Tag(name="Unrelated")
    db_session.add_all([indie, unrelated])
    db_session.commit()
    _tracks(db_session, root, "rock", 40, genre="Rock", tags=[indie])
    _tracks(db_session, root, "pop", 20, genre="Pop", tags=[indie])
    _tracks(db_session, root, "other", 20, genre="Folk", tags=[unrelated])

    mixes = refresh_all_genre_mixes(db_session, user)
    rock = next(m for m in mixes if m.mix_genre == "Rock")
    genres = Counter(item.media_item.genre for item in rock.items)
    assert genres == {"Rock": 40}
    assert [i.position for i in rock.items] == list(range(40))


def test_automatic_mix_tops_up_from_primary_without_co_tracks(db_session):
    user = db_session.query(User).one()
    root = _root(db_session)
    indie = Tag(name="Indie")
    db_session.add(indie)
    db_session.commit()
    _tracks(db_session, root, "rock", 40, genre="Rock", tags=[indie])

    rock = next(m for m in refresh_all_genre_mixes(db_session, user) if m.mix_genre == "Rock")
    assert len(rock.items) == 40


def test_specified_mix_includes_excludes_and_stable_selector(db_session):
    user = db_session.query(User).one()
    root = _root(db_session)
    focus, bad = Tag(name="Focus"), Tag(name="Bad")
    db_session.add_all([focus, bad])
    db_session.commit()
    rock_focus = _tracks(db_session, root, "rf", 3, genre="Rock", tags=[focus])
    rock = _tracks(db_session, root, "r", 3, genre="Rock")
    jazz_focus = _tracks(db_session, root, "jf", 3, genre="Jazz", tags=[focus])
    rock_bad = _tracks(db_session, root, "rb", 3, genre="Rock", tags=[bad])
    _tracks(db_session, root, "jazz", 3, genre="Jazz")

    mix = add_specified_mix_playlist(
        db_session,
        user,
        include=[("tag", "focus"), ("genre", "Rock"), ("genre", "rock")],
        exclude=[("tag", "BAD")],
    )
    ids = {item.media_item_id for item in mix.items}
    expected = {i.id for i in rock_focus + rock + jazz_focus}
    assert ids == expected
    assert not ids & {i.id for i in rock_bad}
    includes, excludes = parse_mix_selector(mix.mix_genre)
    assert includes == [("genre", "Rock"), ("tag", "focus")]
    assert excludes == [("tag", "BAD")]
    assert selector_display_name(mix.mix_genre) == "Rock + focus (no BAD)"
    assert mix.name == "Rock + focus (no BAD) Mix"
    assert mix.mix_genre.startswith("specified:{")

    # Same labels in another order and case replace the same slot.
    again = add_specified_mix_playlist(
        db_session,
        user,
        include=[("genre", "rock"), ("tag", "Focus")],
        exclude=[("TAG", "bad")],
    )
    assert again.id == mix.id
    assert again.mix_genre == mix.mix_genre

    # Single include keeps the original selector format.
    single = add_specified_mix_playlist(db_session, user, "genre", "Jazz")
    assert single.mix_genre == "specified:genre:Jazz"

    with pytest.raises(ValueError):
        add_specified_mix_playlist(db_session, user)
    with pytest.raises(ValueError):
        add_specified_mix_playlist(
            db_session, user, include=[("genre", "Rock")], exclude=[("genre", "Rock")]
        )


def test_specified_mix_add_more_honours_excludes(db_session):
    user = db_session.query(User).one()
    root = _root(db_session)
    bad = Tag(name="Bad")
    db_session.add(bad)
    db_session.commit()
    good = _tracks(db_session, root, "good", 50, genre="Rock")
    _tracks(db_session, root, "bad", 20, genre="Rock", tags=[bad])

    mix = add_specified_mix_playlist(
        db_session, user, include=[("genre", "Rock")], exclude=[("tag", "Bad")]
    )
    assert len(mix.items) == 40
    added = add_more_tracks_to_auto_playlist(db_session, user, mix)
    assert added == 10
    assert len(mix.items) == 50
    assert {item.media_item_id for item in mix.items} == {i.id for i in good}


def test_tag_matching_uses_normalised_musicbrainz_names(db_session):
    user = db_session.query(User).one()
    root = _root(db_session)
    tag = MusicBrainzTag(name="Math Rock", normalized_name="math rock")
    db_session.add(tag)
    db_session.commit()
    items = _tracks(db_session, root, "math", 3)
    for item in items:
        item.musicbrainz_tags.append(tag)
    db_session.commit()

    mix = add_specified_mix_playlist(db_session, user, include=[("tag", "  MATH   rock ")])
    assert {i.media_item_id for i in mix.items} == {i.id for i in items}


def test_specified_mix_endpoint_contract(client, db_session):
    root = _root(db_session)
    bad = Tag(name="Bad")
    db_session.add(bad)
    db_session.commit()
    _tracks(db_session, root, "rock", 3, genre="Rock")
    _tracks(db_session, root, "jazz", 3, genre="Jazz")
    _tracks(db_session, root, "bad", 2, genre="Rock", tags=[bad])

    url = "/api/library/mixes/specified"
    body = {
        "include": [{"kind": "genre", "name": "Rock"}, {"kind": "genre", "name": "Jazz"}],
        "exclude": [{"kind": "tag", "name": "Bad"}],
    }
    resp = client.post(url, json=body)
    assert resp.status_code == 200
    mix = resp.json()[-1]
    assert mix["is_specified"] is True
    assert mix["genre"] == "Jazz + Rock (no Bad)"
    assert mix["track_count"] == 6

    # The legacy single body still works, alone or alongside include.
    legacy = client.post(url, json={"kind": "genre", "name": "Jazz"})
    assert legacy.status_code == 200
    assert legacy.json()[-1]["genre"] == "Jazz"
    merged = client.post(
        url, json={"kind": "genre", "name": "Jazz", "include": [{"kind": "genre", "name": "Rock"}]}
    )
    assert merged.json()[-1]["genre"] == "Jazz + Rock"

    for empty in ({}, {"exclude": [{"kind": "tag", "name": "Bad"}]}, {"include": [{"kind": "tag", "name": " "}]}):
        assert client.post(url, json=empty).status_code == 400
    assert client.post(url, json={"include": [{"kind": "mood", "name": "x"}]}).status_code == 422
    assert json.loads(json.dumps(mix))["playlist_id"]


def test_umbrella_labels_are_down_weighted_not_dropped(db_session, monkeypatch):
    user = db_session.query(User).one()
    root = _root(db_session)
    shoegaze = Tag(name="Shoegaze")
    db_session.add(shoegaze)
    db_session.commit()
    _tracks(db_session, root, "gaze", 16, genre="Rock", tags=[shoegaze])
    _tracks(db_session, root, "plain", 20, genre="Rock")
    _tracks(db_session, root, "folk", 20, genre="Folk")

    first_weights = []
    real_choices = random.choices

    def spy(options, weights, k):
        if not first_weights:
            first_weights.append(dict(zip((o[:2] for o in options), weights)))
        return real_choices(options, weights=weights, k=k)

    monkeypatch.setattr("app.discovery.pools.random.choices", spy)
    names = {m.mix_genre for m in refresh_all_genre_mixes(db_session, user)}
    # All three are eligible: the biggest label is not excluded...
    assert names == {"tag:Shoegaze", "Rock", "Folk"}
    weights = first_weights[0]
    # ...but Rock, an umbrella over Shoegaze, only carries half its weight.
    assert weights[("genre", "Rock")] == 36 * 0.5
    assert weights[("genre", "Folk")] == 20
    assert weights[("tag", "Shoegaze")] == 16


def test_biggest_labels_are_drawn_most_often(db_session):
    from app.discovery.pools import _weighted_mix_selection

    options = [("genre", "Huge", 20_000)] + [("tag", f"t{i}", 50) for i in range(300)]
    random.seed(7)
    huge_picks = sum(
        any(name == "Huge" for _k, name, _c in _weighted_mix_selection(options, 13))
        for _ in range(300)
    )
    # 20,000 of ~35,000 tracks: it should appear in most refreshes.
    assert huge_picks > 150


def test_specified_mix_uses_tracks_with_every_label_first(db_session):
    user = db_session.query(User).one()
    root = _root(db_session)
    focus, calm = Tag(name="Focus"), Tag(name="Calm")
    db_session.add_all([focus, calm])
    db_session.commit()
    both = _tracks(db_session, root, "both", 10, genre="Jazz", tags=[focus, calm])
    _tracks(db_session, root, "g", 40, genre="Jazz")
    _tracks(db_session, root, "t", 40, genre="Rock", tags=[focus])
    _tracks(db_session, root, "other", 40, genre="Rock", tags=[calm])

    # Jazz + Focus + Calm: 10 tracks carry all three, the rest carry fewer.
    mix = add_specified_mix_playlist(
        db_session, user, include=[("genre", "Jazz"), ("tag", "Focus"), ("tag", "Calm")]
    )
    ids = [item.media_item_id for item in mix.items]
    assert len(ids) == 40
    # Every all-label track is in; the rest are only as much of a subset as needed.
    assert {t.id for t in both} <= set(ids)


def test_specified_mix_does_not_dilute_when_enough_tracks_match_all(db_session):
    user = db_session.query(User).one()
    root = _root(db_session)
    focus = Tag(name="Focus")
    db_session.add(focus)
    db_session.commit()
    both = _tracks(db_session, root, "both", 60, genre="Jazz", tags=[focus])
    _tracks(db_session, root, "jazz", 60, genre="Jazz")
    _tracks(db_session, root, "focus", 60, genre="Rock", tags=[focus])

    mix = add_specified_mix_playlist(
        db_session, user, include=[("genre", "Jazz"), ("tag", "Focus")]
    )
    ids = {item.media_item_id for item in mix.items}
    assert len(ids) == 40
    assert ids <= {t.id for t in both}


def test_specified_mix_falls_back_to_subsets_only_for_the_shortfall(db_session):
    user = db_session.query(User).one()
    root = _root(db_session)
    focus = Tag(name="Focus")
    db_session.add(focus)
    db_session.commit()
    both = _tracks(db_session, root, "both", 5, genre="Jazz", tags=[focus])
    jazz = _tracks(db_session, root, "jazz", 60, genre="Jazz")
    _tracks(db_session, root, "none", 60, genre="Pop")

    mix = add_specified_mix_playlist(
        db_session, user, include=[("genre", "Jazz"), ("tag", "Focus")]
    )
    ids = {item.media_item_id for item in mix.items}
    assert len(ids) == 40
    assert {t.id for t in both} <= ids            # all five full matches
    assert ids - {t.id for t in both} <= {t.id for t in jazz}  # filler is a subset
    # "Add more" keeps preferring the best remaining tier.
    more = add_more_tracks_to_auto_playlist(db_session, user, mix)
    assert more == ADD_MORE_TRACKS_COUNT


def test_umbrella_label_kept_when_nothing_narrower_qualifies(db_session):
    user = db_session.query(User).one()
    root = _root(db_session)
    rare = Tag(name="Rare")
    db_session.add(rare)
    db_session.commit()
    _tracks(db_session, root, "a", 5, genre="Rock", tags=[rare])
    _tracks(db_session, root, "b", 20, genre="Rock")

    names = {m.mix_genre for m in refresh_all_genre_mixes(db_session, user)}
    assert names == {"Rock"}


def test_automatic_mix_fills_only_the_shortfall_from_co_occurring_tags(db_session):
    user = db_session.query(User).one()
    root = _root(db_session)
    indie = Tag(name="Indie")
    unrelated = Tag(name="Unrelated")
    db_session.add_all([indie, unrelated])
    db_session.commit()
    _tracks(db_session, root, "rock", 20, genre="Rock", tags=[indie])
    _tracks(db_session, root, "pop", 30, genre="Pop", tags=[indie])
    _tracks(db_session, root, "other", 20, genre="Folk", tags=[unrelated])

    rock = next(m for m in refresh_all_genre_mixes(db_session, user) if m.mix_genre == "Rock")
    genres = Counter(item.media_item.genre for item in rock.items)
    assert genres == {"Rock": 20, "Pop": 20}
