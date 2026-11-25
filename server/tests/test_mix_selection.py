"""Mix generation: similar-artist scoring, the shared selection pipeline,
recording de-duplication, eligibility, collection mixes and autoplay
exclusions - every one without outside services."""

from __future__ import annotations

import random
from collections import Counter
from contextlib import contextmanager
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import event

from app.artist_credits import rebuild_all_credits
from app.config import settings
from app.discovery import (
    add_more_tracks_to_auto_playlist,
    add_specified_mix_playlist,
    generate_playlist_from_collection,
    refresh_all_genre_mixes,
    refresh_discovery_playlist,
    similar,
    similar_tracks_for_seed,
)
from app.discovery.common import MIX_MAX_PER_ARTIST, SHORT_MIX_MAX_PER_ARTIST
from app.discovery.labels import _order_by_cohesion
from app.discovery.sampling import _balanced_track_sample, _picks
from app.library_cache import bump_library_version
from app.metadata_taxonomy import replace_item_genres, replace_item_musicbrainz_tags
from app.models import (
    Album,
    ArtistMetadataCache,
    Favorite,
    LibraryRoot,
    MediaItem,
    MediaKind,
    PlaybackQueueItem,
    PlayHistory,
    Playlist,
    TrackDislike,
    User,
    utcnow,
)
from app.playback import _continuation_tracks


def _seeded(seed: int) -> random.Random:
    return random.Random(seed)  # noqa: S311 - reproducible test draws


@pytest.fixture(autouse=True)
def _no_lastfm():
    """No similar-artist service is asked: any call out to ListenBrainz
    fails the test."""
    with (
        patch("app.discovery.similar.listenbrainz.similar_artists", side_effect=AssertionError("ListenBrainz called")),
        patch("app.listenbrainz_data.listenbrainz.similar_recordings", side_effect=AssertionError("ListenBrainz called")),
    ):
        yield


@pytest.fixture()
def user(db_session) -> User:
    return db_session.query(User).one()


@pytest.fixture()
def root(db_session) -> LibraryRoot:
    root = LibraryRoot(path="/mixes", display_name="Mixes")
    db_session.add(root)
    db_session.commit()
    return root


_counter = iter(range(10**6))


def _track(db_session, root, title, artist, *, genres=(), commit=True, **values) -> MediaItem:
    item = MediaItem(
        library_root_id=root.id,
        kind=MediaKind.track,
        relative_path=f"{next(_counter)}-{title}.mp3",
        title=title,
        artist=artist,
        **values,
    )
    db_session.add(item)
    db_session.flush()
    if genres:
        replace_item_genres(db_session, item, list(genres), "embedded")
    if commit:
        db_session.commit()
    return item


def _album(db_session, owner, title, primary="Album", secondary=None) -> Album:
    album = Album(
        owner=owner, title=title, owner_key=owner.casefold(), title_key=title.casefold(),
        primary_type=primary, secondary_types_json=secondary,
    )
    db_session.add(album)
    db_session.commit()
    return album


def _play(db_session, user, item, times=1, hours_ago=0.0):
    for _ in range(times):
        db_session.add(
            PlayHistory(user_id=user.id, media_item_id=item.id, played_at=utcnow() - timedelta(hours=hours_ago))
        )
    db_session.commit()


def _artists(db_session, ids):
    return [db_session.get(MediaItem, media_id).artist for media_id in ids]


# --- 1. similar-artist selection ------------------------------------------------


def test_a_label_most_artists_carry_counts_less_than_a_rare_one(db_session, root):
    _track(db_session, root, "Seed", "Seed", genres=["Rock", "Shoegaze"])
    _track(db_session, root, "Common", "Common Match", genres=["Rock"])
    _track(db_session, root, "Rare", "Rare Match", genres=["Shoegaze"])
    for n in range(10):
        _track(db_session, root, f"Filler {n}", f"Filler {n}", genres=["Rock"])

    scores = similar._label_overlap_by_artist(db_session, {root.id}, frozenset({"Seed"}))
    assert scores["Rare Match"] > 2 * scores["Common Match"] > 0


def test_a_broadly_tagged_catalogue_does_not_win_by_breadth(db_session, root):
    _track(db_session, root, "Seed", "Seed", genres=["Shoegaze", "Dream Pop"])
    focused = [_track(db_session, root, f"F{n}", "Focused", genres=["Shoegaze", "Dream Pop"]) for n in range(2)]
    # Thirty tracks over twenty labels: more shared labels in total than
    # the focused artist, but most of its catalogue is something else.
    for n in range(30):
        _track(db_session, root, f"B{n}", "Broad", genres=["Shoegaze" if n < 6 else f"Style {n % 20}", "Dream Pop" if n < 3 else "Noise"])
    assert focused
    scores = similar._label_overlap_by_artist(db_session, {root.id}, frozenset({"Seed"}))
    assert scores["Focused"] > scores["Broad"] > 0


def test_tag_votes_weight_a_shared_tag(db_session, root):
    seed = _track(db_session, root, "Seed", "Seed")
    strong = _track(db_session, root, "Strong", "Strongly Tagged")
    weak = _track(db_session, root, "Weak", "Weakly Tagged")
    replace_item_musicbrainz_tags(db_session, seed, [("dreamy", 3)])
    replace_item_musicbrainz_tags(db_session, strong, [("dreamy", 4), ("other", 1)])
    replace_item_musicbrainz_tags(db_session, weak, [("dreamy", 1), ("other", 1)])
    db_session.commit()
    scores = similar._label_overlap_by_artist(db_session, {root.id}, frozenset({"Seed"}))
    assert scores["Strongly Tagged"] > scores["Weakly Tagged"] > 0


def test_equal_artists_are_drawn_at_random_not_alphabetically(db_session, root):
    _track(db_session, root, "Seed", "Seed", genres=["Krautrock"])
    for n in range(40):
        _track(db_session, root, f"T{n}", f"Artist {n:02d}", genres=["Krautrock"], commit=False)
    db_session.commit()
    seen: Counter[str] = Counter()
    sets = set()
    for run in range(200):
        chosen = similar._artists_similar_by_genre(
            db_session, {root.id}, ["Seed"], allow_fetch=False, rng=_seeded(run)
        )
        assert len(chosen) == similar.MAX_DISCOVERY_ARTISTS
        seen.update(chosen)
        sets.add(frozenset(chosen))
    assert len(sets) > 100  # the same seed does not keep giving the same artists
    assert len(seen) == 40  # every equal artist turns up
    # The alphabetically first twenty are no likelier than the rest.
    first = sum(seen[f"Artist {n:02d}"] for n in range(20))
    assert 0.4 < first / sum(seen.values()) < 0.6


def test_similar_artists_come_from_a_shortlist_weighted_by_score():
    scores = {f"a{n}": 1.0 - n / 200 for n in range(100)}
    picked: Counter[str] = Counter()
    for run in range(300):
        picked.update(similar._sample_artists(scores, _seeded(run)))
    assert set(picked) <= {f"a{n}" for n in range(similar.SIMILAR_ARTIST_SHORTLIST)}
    assert picked["a0"] > picked["a59"]


def test_scores_are_cached_but_the_draw_is_not(db_session, root):
    _track(db_session, root, "Seed", "Seed", genres=["Krautrock"])
    for n in range(40):
        _track(db_session, root, f"T{n}", f"Artist {n}", genres=["Krautrock"], commit=False)
    db_session.commit()
    with patch.object(similar, "_build_label_index", wraps=similar._build_label_index) as build:
        draws = {
            frozenset(similar._artists_similar_by_genre(db_session, {root.id}, ["Seed"], allow_fetch=False, rng=_seeded(run)))
            for run in range(10)
        }
    assert build.call_count == 1
    assert len(draws) > 1


def test_lastfm_similar_artists_are_only_an_optional_boost(db_session, root):
    _track(db_session, root, "Seed", "Seed", genres=["Rock"])
    _track(db_session, root, "Label", "Label Match", genres=["Rock"])
    _track(db_session, root, "Only Lastfm", "Lastfm Match", genres=["Polka"])
    without = similar._artists_similar_by_genre(db_session, {root.id}, ["Seed"], allow_fetch=False)
    assert without == {"Label Match"}
    db_session.add(ArtistMetadataCache(artist="Seed", similar_artists_json=["Lastfm Match"], similar_scores_json={"Lastfm Match": 1.0}, fetched_at=utcnow()))
    db_session.commit()
    with_cache = similar._artists_similar_by_genre(db_session, {root.id}, ["Seed"], allow_fetch=False, online=True)
    assert with_cache == {"Label Match", "Lastfm Match"}
    # Song Mix from any track reads (never fetches) that cache too, for a
    # user who allows ListenBrainz...
    from app.discovery import similar_tracks_for_seed

    user = db_session.query(User).one()
    seed = db_session.query(MediaItem).filter_by(artist="Seed").one()
    picked = {db_session.get(MediaItem, i).artist for i in similar_tracks_for_seed(db_session, user, seed.id)}
    assert "Lastfm Match" in picked
    # A user who turned ListenBrainz off gets none of its data, cached or not.
    assert similar._artists_similar_by_genre(db_session, {root.id}, ["Seed"], allow_fetch=False) == {"Label Match"}


def test_a_seeds_feat_variants_are_not_recommended_and_variants_come_together(db_session, root):
    _track(db_session, root, "Seed", "Seed", genres=["Rock"])
    _track(db_session, root, "Guest", "Seed feat. Guest", genres=["Rock"])
    _track(db_session, root, "Other", "Other", genres=["Rock"])
    _track(db_session, root, "Other duet", "Other feat. Someone", genres=["Rock"])
    chosen = similar._artists_similar_by_genre(db_session, {root.id}, ["Seed"], allow_fetch=False)
    assert chosen == {"Other", "Other feat. Someone"}


# --- 2. one pipeline ----------------------------------------------------------


def _prolific_library(db_session, root, user):
    seed = _track(db_session, root, "Seed song", "Seed Artist", genres=["Rock"], album="Seed LP")
    for n in range(4):
        _track(db_session, root, f"Seed {n}", "Seed Artist", genres=["Rock"], album="Seed LP", commit=False)
    for n in range(60):
        _track(db_session, root, f"Hit {n}", "Prolific", genres=["Rock"], album=f"P{n % 6}", commit=False)
    for artist in range(15):
        for n in range(3):
            _track(db_session, root, f"Song {artist}-{n}", f"Band {artist}", genres=["Rock"], album=f"B{artist}", commit=False)
    db_session.commit()
    _play(db_session, user, seed, times=3)
    return seed


def test_a_prolific_artist_gets_at_most_the_cap_in_every_mix_kind(db_session, root, user):
    seed = _prolific_library(db_session, root, user)

    def prolific(ids):
        return sum(artist == "Prolific" for artist in _artists(db_session, ids))

    for run in range(5):
        rng = _seeded(run)
        refresh_discovery_playlist(db_session, user, rng=rng)
        discovery = db_session.query(Playlist).filter_by(auto_kind="discovery").one()
        assert prolific([i.media_item_id for i in discovery.items]) <= SHORT_MIX_MAX_PER_ARTIST

        song_mix = similar_tracks_for_seed(db_session, user, seed.id, boost_likes=True, rng=rng)
        assert song_mix[0] == seed.id and prolific(song_mix) <= SHORT_MIX_MAX_PER_ARTIST

        result = generate_playlist_from_collection(db_session, user, "Seed Artist", artist="Seed Artist", rng=rng)
        artist_mix = db_session.get(Playlist, result["playlist_id"])
        assert prolific([i.media_item_id for i in artist_mix.items]) <= SHORT_MIX_MAX_PER_ARTIST
        assert "Seed Artist" not in _artists(db_session, [i.media_item_id for i in artist_mix.items])

        for mix in refresh_all_genre_mixes(db_session, user, rng=rng):
            assert prolific([i.media_item_id for i in mix.items]) <= MIX_MAX_PER_ARTIST
        specified = add_specified_mix_playlist(db_session, user, "genre", "rock", rng=rng)
        assert prolific([i.media_item_id for i in specified.items]) <= MIX_MAX_PER_ARTIST

        assert prolific(_continuation_tracks(db_session, user, seed.id)) <= SHORT_MIX_MAX_PER_ARTIST


def test_feat_and_duet_credits_share_one_artists_cap(db_session, root):
    for n in range(6):
        _track(db_session, root, f"Solo {n}", "Ana", genres=["Pop"], commit=False)
        _track(db_session, root, f"Feat {n}", "Ana feat. Bo", genres=["Pop"], commit=False)
        _track(db_session, root, f"Duet {n}", "Ana & Cy", genres=["Pop"], commit=False)
        _track(db_session, root, f"Pair {n}", "Simon & Garfunkel", genres=["Pop"], commit=False)
    for n in range(20):
        _track(db_session, root, f"Other {n}", f"Other {n}", genres=["Pop"], commit=False)
    db_session.commit()
    rebuild_all_credits(db_session)
    pool = _picks(db_session, db_session.query(MediaItem).statement)
    keys = {pick.artist: pick.artist_key for pick in pool}
    assert keys["Ana"] == keys["Ana feat. Bo"] == keys["Ana & Cy"]
    assert keys["Simon & Garfunkel"] not in {keys["Ana"], "simon"}
    for run in range(20):
        chosen = _balanced_track_sample(pool, 20, max_per_artist=2, rng=_seeded(run))
        assert sum(pick.artist_key == keys["Ana"] for pick in chosen) <= 2


def _fake(item_id, artist, album_id, *labels):
    return SimpleNamespace(
        id=item_id, artist=artist, album_id=album_id, genre=None, genres=[],
        tags=[SimpleNamespace(name=label) for label in labels], musicbrainz_tags=[],
    )


def test_ordering_spreads_a_compilations_tracks_apart():
    # Ten different artists, but four tracks from one compilation that share
    # more labels with each other than with anything else.
    items = [_fake(n, f"Various {n}", 99, "a", "b", "c") for n in range(4)]
    items += [_fake(10 + n, f"Band {n}", n, "a") for n in range(6)]
    for run in range(30):
        ordered = _order_by_cohesion(items, rng=_seeded(run))
        albums = [item.album_id for item in ordered]
        assert all(a != b for a, b in zip(albums, albums[1:])), albums


def test_song_mix_seed_stays_first_through_ordering(db_session, root, user):
    seed = _prolific_library(db_session, root, user)
    for run in range(10):
        assert similar_tracks_for_seed(db_session, user, seed.id, rng=_seeded(run))[0] == seed.id


# --- 3. one version per recording --------------------------------------------


def test_versions_of_one_recording_never_share_a_mix(db_session, root, user):
    seed = _track(db_session, root, "Seed", "Seed", genres=["Rock"])
    titles = ["Song", "Song (2011 Remaster)", "Song - Remastered 2009", "Song (feat. Guest)"]
    versions = [_track(db_session, root, title, "Other", genres=["Rock"]) for title in titles]
    live = _track(db_session, root, "Song (Live)", "Other", genres=["Rock"])
    by_mbid = [
        _track(db_session, root, title, "Third", genres=["Rock"], musicbrainz_recording_mbid="rec-1")
        for title in ("Original Title", "Retitled")
    ]
    for run in range(20):
        ids = set(similar_tracks_for_seed(db_session, user, seed.id, rng=_seeded(run)))
        assert len(ids & {v.id for v in versions}) == 1
        assert live.id in ids  # a live version is another song
        assert len(ids & {v.id for v in by_mbid}) == 1


def test_the_kept_version_is_the_liked_then_most_played_then_studio_one(db_session, root, user):
    seed = _track(db_session, root, "Seed", "Seed", genres=["Rock"])
    studio = _album(db_session, "Other", "Studio")
    best_of = _album(db_session, "Other", "Best Of", secondary=["Compilation"])
    on_studio = _track(db_session, root, "Hit", "Other", genres=["Rock"], album_id=studio.id)
    on_compilation = _track(db_session, root, "Hit (Remastered)", "Other", genres=["Rock"], album_id=best_of.id)

    def kept():
        ids = set(similar_tracks_for_seed(db_session, user, seed.id, rng=_seeded(len(kept_runs))))
        kept_runs.append(1)
        return ids & {on_studio.id, on_compilation.id}

    kept_runs: list[int] = []
    for _ in range(10):
        assert kept() == {on_studio.id}
    _play(db_session, user, on_compilation, times=2)
    for _ in range(10):
        assert kept() == {on_compilation.id}
    db_session.add(Favorite(user_id=user.id, media_item_id=on_studio.id))
    db_session.commit()
    for _ in range(10):
        assert kept() == {on_studio.id}


def test_add_more_does_not_bring_back_another_version(db_session, root, user):
    for n in range(10):
        _track(db_session, root, f"Song {n}", "Band", genres=["Jazz"], commit=False)
        _track(db_session, root, f"Song {n} (Remastered)", "Band", genres=["Jazz"], commit=False)
    for n in range(10):
        _track(db_session, root, f"Tune {n}", f"Act {n}", genres=["Jazz"], commit=False)
    db_session.commit()
    mix = add_specified_mix_playlist(db_session, user, "genre", "Jazz", rng=_seeded(1))
    add_more_tracks_to_auto_playlist(db_session, user, mix, rng=_seeded(2))
    titles = [db_session.get(MediaItem, item.media_item_id).title.replace(" (Remastered)", "") for item in mix.items]
    assert len(titles) == len(set(titles)) == 20


# --- 4. eligibility -------------------------------------------------------------


def test_short_long_and_spoken_word_tracks_are_left_out(db_session, root, user, monkeypatch):
    seed = _track(db_session, root, "Intro", "Seed", genres=["Rock"], duration_seconds=30)
    spoken = _album(db_session, "Other", "Interview", primary="Other", secondary=["Interview"])
    live = _album(db_session, "Other", "Live at X", secondary=["Live"])
    ok = _track(db_session, root, "Song", "Other", genres=["Rock"], duration_seconds=200)
    unknown = _track(db_session, root, "Unknown length", "Other", genres=["Rock"])
    short = _track(db_session, root, "Skit", "Other", genres=["Rock"], duration_seconds=20)
    long = _track(db_session, root, "DJ mix", "Other", genres=["Rock"], duration_seconds=3600)
    talk = _track(db_session, root, "Talk", "Other", genres=["Rock"], duration_seconds=200, album_id=spoken.id)
    on_stage = _track(db_session, root, "Song live", "Other", genres=["Rock"], duration_seconds=200, album_id=live.id)

    ids = similar_tracks_for_seed(db_session, user, seed.id)
    assert ids[0] == seed.id  # a seed the user picked is never filtered
    assert set(ids[1:]) == {ok.id, unknown.id, on_stage.id}
    assert not {short.id, long.id, talk.id} & set(ids)

    monkeypatch.setattr(settings, "mix_allow_live_albums", False)
    assert set(similar_tracks_for_seed(db_session, user, seed.id)[1:]) == {ok.id, unknown.id}


def test_specified_mix_applies_eligibility_on_top_of_includes_and_excludes(db_session, root, user):
    keep = [_track(db_session, root, f"Keep {n}", f"A{n}", genres=["Jazz"], duration_seconds=180) for n in range(5)]
    _track(db_session, root, "Too short", "B", genres=["Jazz"], duration_seconds=10)
    _track(db_session, root, "Excluded", "C", genres=["Jazz", "Swing"], duration_seconds=180)
    mix = add_specified_mix_playlist(db_session, user, include=[("genre", "jazz")], exclude=[("genre", "swing")])
    assert {item.media_item_id for item in mix.items} == {item.id for item in keep}


# --- every mix kind without outside services ----------------------------------


def test_every_mix_kind_works_without_any_lastfm_data(client, db_session, root, user):
    seed = _prolific_library(db_session, root, user)
    assert refresh_discovery_playlist(db_session, user)["tracks_added"] > 0
    assert len(similar_tracks_for_seed(db_session, user, seed.id, boost_likes=True)) > 1
    assert client.post("/api/library/discovery/generate", json={"media_item_id": seed.id}).status_code == 200
    artist_mix = client.post("/api/library/discovery/generate-collection", json={"label": "Seed Artist", "artist": "Seed Artist"})
    assert artist_mix.status_code == 200 and artist_mix.json()["track_count"] > 1
    album_mix = client.post(
        "/api/library/discovery/generate-collection",
        json={"label": "Seed LP", "artist": "Seed Artist", "album": "Seed LP"},
    )
    assert album_mix.status_code == 200 and album_mix.json()["track_count"] > 1
    assert refresh_all_genre_mixes(db_session, user)
    assert add_specified_mix_playlist(db_session, user, "genre", "Rock").items
    assert _continuation_tracks(db_session, user, seed.id)
    db_session.expire_all()
    for playlist in db_session.query(Playlist).filter(Playlist.auto_kind.isnot(None), Playlist.auto_kind != "favorites"):
        assert add_more_tracks_to_auto_playlist(db_session, user, playlist) > 0, playlist.name


# --- 5. collection mixes --------------------------------------------------------


def test_a_compilation_album_mix_finds_its_seeds_and_add_more_reuses_it(db_session, root, user):
    for n in range(4):
        _track(db_session, root, f"Comp {n}", f"Comp Artist {n}", genres=["Disco"], album="Disco Hits", album_artist="Various Artists")
    for n in range(40):
        _track(db_session, root, f"Other {n}", f"Disco Act {n % 10}", genres=["Disco"], album=f"LP {n % 10}", commit=False)
    db_session.commit()
    rebuild_all_credits(db_session)

    result = generate_playlist_from_collection(db_session, user, "Disco Hits", artist="Various Artists", album="Disco Hits")
    assert result is not None and result["track_count"] > 1
    playlist = db_session.get(Playlist, result["playlist_id"])
    titles = {db_session.get(MediaItem, item.media_item_id).title for item in playlist.items}
    assert not any(title.startswith("Comp ") for title in titles)
    assert playlist.mix_genre and playlist.mix_genre.startswith("collection:")

    before = {item.media_item_id for item in playlist.items}
    added = add_more_tracks_to_auto_playlist(db_session, user, playlist)
    after = [item.media_item_id for item in playlist.items]
    assert added > 0 and len(after) == len(set(after))
    new_titles = {db_session.get(MediaItem, i).title for i in set(after) - before}
    assert not any(title.startswith("Comp ") for title in new_titles)


def test_an_album_without_an_artist_means_that_album_only(db_session, root, user):
    mine = _track(db_session, root, "Untagged", None, genres=["Ambient"], album="Untitled")
    _track(db_session, root, "Other album same title", "Somebody", genres=["Polka"], album="Untitled")
    for n in range(4):
        _track(db_session, root, f"Ambient {n}", f"Drone {n}", genres=["Ambient"])
    for n in range(4):
        _track(db_session, root, f"Polka {n}", f"Oompah {n}", genres=["Polka"])
    assert mine.artist is None

    result = generate_playlist_from_collection(db_session, user, "Untitled", album="Untitled")
    artists = _artists(db_session, [i.media_item_id for i in db_session.get(Playlist, result["playlist_id"]).items])
    assert artists and all(artist.startswith("Drone") for artist in artists)


def test_a_song_mix_after_an_artist_mix_forgets_the_collection(db_session, root, user):
    seed = _prolific_library(db_session, root, user)
    generate_playlist_from_collection(db_session, user, "Seed Artist", artist="Seed Artist")
    similar_tracks_for_seed(db_session, user, seed.id)
    from app.discovery import generate_playlist_from_track

    playlist = db_session.get(Playlist, generate_playlist_from_track(db_session, user, seed.id)["playlist_id"])
    assert playlist.mix_genre is None


# --- 6. autoplay ------------------------------------------------------------------


def test_autoplay_skips_the_queue_recent_plays_and_the_seeds_other_versions(db_session, root, user):
    seed = _track(db_session, root, "Seed", "Seed", genres=["Rock"])
    seed_again = _track(db_session, root, "Seed (Remastered)", "Seed", genres=["Rock"])
    queued = _track(db_session, root, "Queued", "Other", genres=["Rock"])
    recent = _track(db_session, root, "Recent", "Other", genres=["Rock"])
    old = _track(db_session, root, "Old", "Other", genres=["Rock"])
    fresh = _track(db_session, root, "Fresh", "Other", genres=["Rock"])
    db_session.add(PlaybackQueueItem(user_id=user.id, position=0, original_position=0, media_item_id=queued.id))
    db_session.commit()
    _play(db_session, user, recent, hours_ago=2)
    _play(db_session, user, old, hours_ago=settings.autoplay_recent_hours + 1)

    ids = set(_continuation_tracks(db_session, user, seed.id))
    assert ids == {old.id, fresh.id}
    assert not {seed.id, seed_again.id, queued.id, recent.id} & ids


def test_autoplay_fallbacks_also_skip_the_queue_and_ineligible_tracks(db_session, root, user):
    seed = _track(db_session, root, "Seed", "Lonely")  # no labels: no similar tier
    queued = _track(db_session, root, "Queued", "Lonely")
    short = _track(db_session, root, "Skit", "Lonely", duration_seconds=10)
    other = _track(db_session, root, "Other", "Lonely")
    db_session.add(PlaybackQueueItem(user_id=user.id, position=0, original_position=0, media_item_id=queued.id))
    db_session.commit()
    assert _continuation_tracks(db_session, user, seed.id) == [other.id]

    # Nothing by the artist left: the random tier, with the same rules.
    db_session.add(TrackDislike(user_id=user.id, media_item_id=other.id))
    stranger = _track(db_session, root, "Stranger", "Someone")
    assert _continuation_tracks(db_session, user, seed.id) == [stranger.id]
    assert short.id not in _continuation_tracks(db_session, user, seed.id)


def test_autoplay_lets_the_queue_back_only_when_nothing_else_is_left(db_session, root, user):
    seed = _track(db_session, root, "Seed", "Lonely")
    queued = _track(db_session, root, "Queued", "Lonely")
    db_session.add(PlaybackQueueItem(user_id=user.id, position=0, original_position=0, media_item_id=queued.id))
    db_session.commit()
    assert _continuation_tracks(db_session, user, seed.id) == [queued.id]


def test_autoplay_plays_short_tracks_rather_than_stop(db_session, root, user):
    seed = _track(db_session, root, "Seed", "Tiny", duration_seconds=5)
    other = _track(db_session, root, "Other", "Tiny", duration_seconds=5)
    assert _continuation_tracks(db_session, user, seed.id) == [other.id]


# --- 8. small consistency rules ------------------------------------------------


def test_mix_options_leave_out_disliked_tracks(client, db_session, root, user):
    tracks = [_track(db_session, root, f"Jazz {n}", f"J{n}", genres=["Jazz"]) for n in range(4)]
    db_session.add(TrackDislike(user_id=user.id, media_item_id=tracks[0].id))
    db_session.commit()
    bump_library_version()
    options = {o["name"]: o["track_count"] for o in client.get("/api/library/mixes/options").json()}
    assert options["Jazz"] == 3
    assert "Jazz" in client.get("/api/library/genres").json()


def test_a_legacy_genre_matches_its_mix_whatever_its_case(db_session, root, user):
    tracks = [_track(db_session, root, f"Legacy {n}", f"L{n}", genre="ROCK") for n in range(3)]
    mix = add_specified_mix_playlist(db_session, user, "genre", "rock")
    assert {item.media_item_id for item in mix.items} == {t.id for t in tracks}


def test_discovery_seed_songs_skip_disliked_and_ignored_tracks(db_session, root, user):
    from app.discovery.similar import _recent_top_tracks
    from app.models import IgnoredLibraryFolder

    liked = _track(db_session, root, "Kept", "A")
    hated = _track(db_session, root, "Hated", "A")
    hidden = MediaItem(library_root_id=root.id, kind=MediaKind.track, relative_path="Hidden/x.mp3", title="Hidden", artist="A")
    db_session.add_all([hidden, IgnoredLibraryFolder(library_root_id=root.id, relative_path="Hidden")])
    db_session.add(TrackDislike(user_id=user.id, media_item_id=hated.id))
    db_session.commit()
    for item in (liked, hated, hidden):
        _play(db_session, user, item, times=2)
    assert [item.id for item in _recent_top_tracks(db_session, user)] == [liked.id]


# --- performance -------------------------------------------------------------------


@contextmanager
def _statements(engine):
    seen: list[str] = []

    def record(_conn, _cursor, statement, *_args):
        seen.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        yield seen
    finally:
        event.remove(engine, "before_cursor_execute", record)


def test_song_mix_and_scoring_take_a_fixed_number_of_queries(db_session, root, user, session_factory):
    """No per-track or per-artist queries: the label index is two grouped
    queries per library version, and a Song Mix (or autoplay batch) a fixed
    handful however big the library or the pool."""
    engine = session_factory.kw["bind"]
    seed = _track(db_session, root, "Seed", "Seed", genres=["Rock", "Indie"])

    def grow(start, count):
        for n in range(start, start + count):
            _track(db_session, root, f"T{n}", f"Artist {n % 40}", genres=["Rock" if n % 2 else "Indie"], album=f"Album {n % 25}", commit=False)
        db_session.commit()
        bump_library_version()

    def run() -> tuple[int, int]:
        similar_tracks_for_seed(db_session, user, seed.id, rng=_seeded(1))  # builds the index
        with _statements(engine) as cold_index:
            bump_library_version()
            similar._label_index(db_session, {root.id})
        with _statements(engine) as warm:
            similar_tracks_for_seed(db_session, user, seed.id, rng=_seeded(1))
        return len(cold_index), len(warm)

    grow(0, 60)
    small = run()
    grow(60, 600)
    large = run()
    assert small == large
    assert small[0] <= 2


def test_title_keys_drop_version_suffixes_and_accents_but_keep_live():
    from app.title_match import normalize_title

    assert normalize_title(None) == normalize_title("") == ""
    assert normalize_title("Café Déjà Vu (2011 Remaster)") == normalize_title("cafe deja vu") == "cafe deja vu"
    assert normalize_title("Rock & Roll - Remastered 2009") == "rock and roll"
    assert normalize_title("Song (Live)") != normalize_title("Song")
    assert normalize_title("Song (Live at Wembley; 2011 Remaster)") != normalize_title("Song")


def test_recording_keys_keep_live_remasters_and_punctuation_titles_apart():
    from app.discovery.eligibility import recording_keys

    def same(a: str | None, b: str | None) -> bool:
        return bool(set(recording_keys(1, "artist", a, None)) & set(recording_keys(2, "artist", b, None)))

    assert same("Song (2011 Remaster)", "Song")
    assert not same("Song (Live at Wembley; 2011 Remaster)", "Song")
    assert not same("Song (Clean Bandit Remix)", "Song (Clean)")
    assert same("Song - Live - 2011 Remaster", "Song - Live")
    assert not same("+", "÷") and same("+", "+")
    assert not same("  ", None)  # no usable title: only ever itself
