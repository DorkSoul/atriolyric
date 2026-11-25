""""Sounds like" in mixes (app.discovery.audio): extra candidates, a bounded
weight, smoother ordering - each only where vectors exist, and nothing
changes without them."""

from __future__ import annotations

import random
from collections import Counter
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pytest

from app import audio_similarity
from app.audio_analysis import store_results
from app.audio_features import ANALYSIS_VERSION, FEATURE_COUNT
from app.discovery import (
    audio,
    generate_playlist_from_collection,
    similar_tracks_for_seed,
)
from app.discovery.labels import _order_by_cohesion
from app.library_cache import bump_library_version
from app.models import LibraryRoot, MediaItem, MediaKind, Playlist, User, utcnow
from app.playback import _continuation_tracks


def _seeded(seed: int) -> random.Random:
    return random.Random(seed)  # noqa: S311 - reproducible test draws


@pytest.fixture(autouse=True)
def _fresh_index():
    """The process-wide index caches a snapshot for 30 s: start and leave
    every test with none, so no other test database's vectors leak in."""
    audio_similarity.audio_index.invalidate()
    with patch.object(audio_similarity.audio_index, "refresh_seconds", 0):
        yield
    audio_similarity.audio_index.invalidate()


@pytest.fixture(autouse=True)
def _no_lastfm():
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
    root = LibraryRoot(path="/audio", display_name="Audio")
    db_session.add(root)
    db_session.commit()
    return root


_counter = iter(range(10**7))
BASE = np.linspace(1.0, 3.0, FEATURE_COUNT).astype(np.float32)


def _track(db_session, root, title, artist, genre=None) -> MediaItem:
    item = MediaItem(
        library_root_id=root.id, kind=MediaKind.track, relative_path=f"{next(_counter)}.mp3",
        title=title, artist=artist, genre=genre,
    )
    db_session.add(item)
    return item


def _vectors(db_session, items, vectors) -> None:
    now = utcnow()
    store_results(
        db_session,
        [
            {
                "media_item_id": item.id, "version": ANALYSIS_VERSION, "analysed_at": now,
                "source_mtime": 0.0, "source_size": None, "error": None,
                "vector": np.asarray(vector, "<f4").tobytes(), "tempo_bpm": None, "loudness_db": None,
                "brightness_hz": None, "musical_key": None, "mode": None, "analysed_seconds": None,
            }
            for item, vector in zip(items, vectors, strict=True)
        ],
    )


def _random(n, seed, spread=0.3):
    rng = np.random.default_rng(seed)
    return BASE * (1 + spread * rng.standard_normal((n, FEATURE_COUNT)))


def _library(db_session, root, *, with_vectors: bool):
    """A seed with ten label-alike artists, plus "Twins": ten artists with
    no labels in common with the seed (another genre) whose songs sound
    almost exactly like it, and forty random-sounding others."""
    seed = _track(db_session, root, "Seed", "Seed Band", "Dream Pop")
    alike = [_track(db_session, root, f"Alike {n}", f"Alike {n % 10}", "Dream Pop") for n in range(30)]
    twins = [_track(db_session, root, f"Twin {n}", f"Twin {n}", "Polka") for n in range(10)]
    others = [_track(db_session, root, f"Other {n}", f"Other {n % 20}", "Polka") for n in range(40)]
    db_session.commit()
    bump_library_version()
    if with_vectors:
        base = _random(1, 1)[0]
        _vectors(db_session, [seed], [base])
        _vectors(db_session, twins, [base * (1 + 0.01 * np.random.default_rng(n).standard_normal(FEATURE_COUNT)) for n in range(10)])
        _vectors(db_session, alike + others, _random(70, 2))
    return seed, alike, twins


@pytest.mark.parametrize("with_vectors", [True, False])
def test_song_mix_adds_tracks_that_sound_like_the_seed_up_to_a_third(db_session, root, user, with_vectors):
    seed, _alike, twins = _library(db_session, root, with_vectors=with_vectors)
    twin_ids = {t.id for t in twins}
    for run in range(3):
        ids = similar_tracks_for_seed(db_session, user, seed.id, boost_likes=True, rng=_seeded(run))
        assert ids[0] == seed.id
        heard = twin_ids & set(ids)
        if with_vectors:
            # Polka shares no label with Dream Pop: only the sound brings them.
            assert 3 <= len(heard) <= audio.max_audio_picks(25)
        else:
            assert not heard
    # The same for autoplay and an artist mix.
    autoplay = set(_continuation_tracks(db_session, user, seed.id))
    result = generate_playlist_from_collection(db_session, user, "Seed Band", artist="Seed Band", rng=_seeded(1))
    artist_mix = {item.media_item_id for item in db_session.get(Playlist, result["playlist_id"]).items}
    assert bool(twin_ids & autoplay) == with_vectors
    assert bool(twin_ids & artist_mix) == with_vectors


def test_without_vectors_no_index_is_loaded(db_session, root, user):
    seed, *_ = _library(db_session, root, with_vectors=False)
    with patch.object(audio_similarity.audio_index, "snapshot", side_effect=AssertionError("index loaded")):
        assert not audio.available(db_session)
        ids = similar_tracks_for_seed(db_session, user, seed.id, rng=_seeded(1))
    assert len(ids) > 10


def test_closer_sounding_candidates_weigh_more(db_session, root, user):
    seed = _track(db_session, root, "Seed", "Seed Band", "Dream Pop")
    near = [_track(db_session, root, f"Near {n}", f"Near {n}", "Dream Pop") for n in range(10)]
    far = [_track(db_session, root, f"Far {n}", f"Far {n}", "Dream Pop") for n in range(10)]
    background = [_track(db_session, root, f"B {n}", f"B {n}", "Polka") for n in range(50)]
    db_session.commit()
    bump_library_version()
    base = _random(1, 1)[0]
    _vectors(db_session, [seed], [base])
    _vectors(db_session, near, [base * (1 + 0.01 * np.random.default_rng(n).standard_normal(FEATURE_COUNT)) for n in range(10)])
    _vectors(db_session, far + background, _random(60, 9))
    distances = audio.distances_to(db_session, [seed.id], [t.id for t in near + far])
    near_weight = np.mean([audio.multiplier(distances[t.id]) for t in near])
    far_weight = np.mean([audio.multiplier(distances[t.id]) for t in far])
    assert near_weight > 1.2 and far_weight < near_weight - 0.2
    assert audio.multiplier(100.0) == pytest.approx(audio.AUDIO_WEIGHT_RANGE[0])
    counts = Counter()
    for run in range(30):
        ids = similar_tracks_for_seed(db_session, user, seed.id, max_tracks=6, rng=_seeded(run))
        counts["near"] += len({t.id for t in near} & set(ids))
        counts["far"] += len({t.id for t in far} & set(ids))
    assert counts["near"] > counts["far"]


def _fake(item_id, artist, *labels):
    return SimpleNamespace(
        id=item_id, artist=artist, genre=None, genres=[], album_id=None, album=None,
        tags=[SimpleNamespace(name=label) for label in labels], musicbrainz_tags=[],
    )


def _path(order, distances):
    return sum(distances[(a.id, b.id)] for a, b in zip(order, order[1:], strict=False))


def test_ordering_flows_in_sound_while_artists_stay_apart():
    # Forty tracks by eight artists, one label each, on a line in sound.
    rng = np.random.default_rng(3)
    positions = {n: float(rng.uniform(0, 10)) for n in range(40)}
    items = [_fake(n, f"Artist {n % 8}", "rock") for n in range(40)]
    distances = {(a, b): abs(positions[a] - positions[b]) for a in positions for b in positions if a != b}
    label_only = [_path(_order_by_cohesion(items, rng=_seeded(run)), distances) for run in range(5)]
    for run in range(5):
        ordered = _order_by_cohesion(items, rng=_seeded(run), distances=distances)
        assert _path(ordered, distances) < 0.5 * min(label_only)
        artists = [item.artist for item in ordered]
        assert all(a != b for a, b in zip(artists, artists[1:], strict=False))
    # A seed stays first.
    ordered = _order_by_cohesion(items, rng=_seeded(1), first=items[7], distances=distances)
    assert ordered[0] is items[7]


def test_ordering_uses_sound_only_when_most_tracks_have_it(db_session, root):
    items = [_track(db_session, root, f"T{n}", f"A{n}", "Rock") for n in range(10)]
    db_session.commit()
    assert audio.order_distances(db_session, [t.id for t in items]) is None  # none analysed
    _vectors(db_session, items[:5], _random(5, 4))
    audio_similarity.audio_index.invalidate()
    assert audio.order_distances(db_session, [t.id for t in items]) is None  # half: labels order
    _vectors(db_session, items[5:8], _random(3, 5))
    audio_similarity.audio_index.invalidate()
    found = audio.order_distances(db_session, [t.id for t in items])
    assert found is not None and (items[0].id, items[1].id) in found
    assert (items[0].id, items[9].id) not in found  # no vector: the median is used


@pytest.mark.parametrize("with_vectors", [True, False])
def test_a_cluster_mix_new_share_includes_sound_alikes(db_session, root, user, with_vectors):
    from datetime import timedelta

    from app.discovery import clusters
    from app.models import PlayHistory

    members = [f"Band {n}" for n in range(4)]
    played = []
    for name in members:
        songs = [_track(db_session, root, f"{name} {t}", name, "Dream Pop") for t in range(6)]
        played += songs[:4]
    twins = [_track(db_session, root, f"Twin {n}", f"Twin {n}", "Polka") for n in range(10)]
    background = [_track(db_session, root, f"B {n}", f"B {n}", "Polka") for n in range(40)]
    db_session.commit()
    for n, item in enumerate(played):
        for days in (3, 10, 20):
            when = utcnow() - timedelta(days=days, minutes=n)
            db_session.add(PlayHistory(user_id=user.id, media_item_id=item.id, played_at=when, started_at=when))
    db_session.commit()
    bump_library_version()
    if with_vectors:
        base = _random(1, 1)[0]
        _vectors(db_session, played, [base] * len(played))
        _vectors(db_session, twins, [base * 1.0001] * 10)
        _vectors(db_session, background, _random(40, 3))
    tracks = clusters.cluster_tracks(db_session, user, "Bands", [m.lower() for m in members], 40, rng=_seeded(1))
    twin_ids = {t.id for t in twins}
    heard = [t for t in tracks if t.id in twin_ids]
    if with_vectors:
        assert 1 <= len(heard) <= audio.max_audio_picks(40)
    else:
        assert not heard
