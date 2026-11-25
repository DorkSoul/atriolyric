"""The in-memory similarity index (app.audio_similarity)."""

from __future__ import annotations

import itertools
import threading

import numpy as np
import pytest
from sqlalchemy import event, text

from app import audio_similarity as sim
from app.audio_analysis import store_results
from app.audio_features import ANALYSIS_VERSION, FEATURE_COUNT, FEATURE_INDEX
from app.models import LibraryRoot, MediaItem, MediaKind, utcnow

BASE = np.zeros(FEATURE_COUNT, np.float32)
for _name, _value in {
    "tempo_bpm": 120,
    "tempo_strength": 0.5,
    "onset_strength": 1.0,
    "loudness_db": -14,
    "loudness_std_db": 4,
    "dynamic_range_db": 12,
    "centroid_hz": 2000,
    "centroid_std_hz": 500,
    "rolloff_hz": 4000,
    "flatness_db": -25,
    "flatness_std_db": 3,
    "zcr": 0.05,
    "zcr_std": 0.02,
    "key_strength": 0.6,
}.items():
    BASE[FEATURE_INDEX[_name]] = _value


def random_vectors(n: int, seed: int = 0, spread: float = 0.3) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return (BASE * (1 + spread * rng.standard_normal((n, FEATURE_COUNT)))).astype(np.float32)


@pytest.fixture()
def library(db_session, tmp_path):
    """add(vectors) -> ids: one track per vector, its features stored."""
    root = LibraryRoot(path=str(tmp_path), display_name="Music")
    db_session.add(root)
    db_session.commit()
    names = itertools.count()

    def add(vectors) -> list[int]:
        items = [
            MediaItem(library_root_id=root.id, relative_path=f"{next(names)}.flac", kind=MediaKind.track, title="t")
            for _ in vectors
        ]
        db_session.add_all(items)
        db_session.commit()
        store(db_session, [item.id for item in items], vectors)
        return [item.id for item in items]

    return add


def store(db, ids, vectors) -> None:
    now = utcnow()
    store_results(
        db,
        [
            {
                "media_item_id": item_id,
                "version": ANALYSIS_VERSION,
                "analysed_at": now,
                "source_mtime": 0.0,
                "source_size": None,
                "error": None if v is not None else "failed",
                "vector": np.asarray(v, "<f4").tobytes() if v is not None else None,
                "tempo_bpm": None,
                "loudness_db": None,
                "brightness_hz": None,
                "musical_key": None,
                "mode": None,
                "analysed_seconds": None,
            }
            for item_id, v in zip(ids, vectors)
        ],
    )


@pytest.fixture()
def index():
    return sim.AudioIndex(refresh_seconds=0)


def test_nearest_is_the_near_duplicate_and_seeds_are_left_out(db_session, library, index):
    vectors = random_vectors(50)
    twin = vectors[7] * 1.001
    ids = library([*vectors, twin])
    result = sim.similar_tracks(db_session, ids[7], 5, index=index)
    assert result[0][0] == ids[-1]
    assert ids[7] not in [i for i, _ in result]
    distances = [d for _, d in result]
    assert distances == sorted(distances) and distances[0] < 0.1
    # exclude and restrict
    assert ids[-1] not in [i for i, _ in sim.similar_tracks(db_session, ids[7], 5, exclude_ids=[ids[-1]], index=index)]
    restricted = sim.similar_tracks(db_session, ids[7], 50, restrict_to_ids=ids[:10], index=index)
    assert {i for i, _ in restricted} == set(ids[:10]) - {ids[7]}
    assert sim.similar_tracks(db_session, 999_999, 5, index=index) == []
    assert sim.similar_tracks(db_session, ids[7], 0, index=index) == []
    assert sim.similar_tracks(db_session, ids[7], 5, restrict_to_ids=[999_999], index=index) == []


def test_several_seeds_min_keeps_each_seeds_neighbours(db_session, library, index):
    rng = np.random.default_rng(7)
    slow_base, fast_base = random_vectors(1, seed=1)[0], random_vectors(1, seed=2)[0]

    def cluster(base):
        return base * (1 + 0.01 * rng.standard_normal((10, FEATURE_COUNT))).astype(np.float32)

    slow, fast, middle = cluster(slow_base), cluster(fast_base), cluster((slow_base + fast_base) / 2)
    ids = library([*slow, *fast, *middle])
    slow_ids, fast_ids, middle_ids = set(ids[:10]), set(ids[10:20]), set(ids[20:])
    seeds = [ids[0], ids[10]]
    nearest = {i for i, _ in sim.similar_tracks(db_session, seeds, 10, index=index)}
    assert nearest & slow_ids and nearest & fast_ids and not nearest & middle_ids
    central = {i for i, _ in sim.similar_tracks(db_session, seeds, 5, combine="centroid", index=index)}
    assert central <= middle_ids  # between the two styles, like neither
    with pytest.raises(ValueError):
        sim.similar_tracks(db_session, seeds, 5, combine="max", index=index)  # type: ignore[arg-type]


def test_tempo_octaves_fold_together():
    rows = np.tile(BASE, (4, 1))
    rows[:, FEATURE_INDEX["tempo_bpm"]] = [60, 120, 240, 0]
    t = sim.transform(rows)[:, FEATURE_INDEX["tempo_bpm"]]
    assert t[0] == pytest.approx(t[1]) == pytest.approx(t[2])
    assert np.isnan(t[3])
    centre, scale = sim._statistics(sim.transform(random_vectors(20)))
    normalised = sim._normalise(sim.transform(rows), centre, scale)
    assert normalised[3, FEATURE_INDEX["tempo_bpm"]] == 0  # unknown tempo sits at the median
    assert np.all(normalised[:, FEATURE_INDEX["key"]] == 0)  # the key carries no weight


def test_refresh_is_incremental_and_reloads_after_deletes(db_session, library, index):
    engine = db_session.get_bind()
    ids = library(random_vectors(20))
    snapshot = index.snapshot(db_session)
    assert len(snapshot.ids) == 20

    statements: list[str] = []
    record = lambda conn, cursor, statement, *a: statements.append(statement)
    event.listen(engine, "before_cursor_execute", record)
    try:
        assert index.snapshot(db_session) is snapshot  # nothing new: one cheap query
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert len(statements) == 1

    # New rows and a re-analysed one are read without reloading the rest.
    new_ids = library(random_vectors(3, seed=5))
    changed = random_vectors(1, seed=9)[0]
    store(db_session, [ids[0]], [changed])
    after = index.snapshot(db_session)
    assert len(after.ids) == 23 and set(new_ids) <= set(after.row_of)
    np.testing.assert_allclose(after.transformed[after.row_of[ids[0]]], sim.transform(changed[None])[0])
    assert after.row_of[ids[5]] == snapshot.row_of[ids[5]]

    # A re-analysis that failed, and a deleted track, leave the index.
    store(db_session, [ids[1]], [None])
    db_session.execute(text("DELETE FROM audio_features WHERE media_item_id = :id"), {"id": ids[2]})
    db_session.commit()
    reloaded = index.snapshot(db_session)
    assert ids[1] not in reloaded.row_of and ids[2] not in reloaded.row_of
    assert len(reloaded.ids) == 21


def test_refresh_waits_for_its_interval(db_session, library):
    index = sim.AudioIndex(refresh_seconds=3600)
    library(random_vectors(5))
    assert len(index.snapshot(db_session).ids) == 5
    library(random_vectors(5, seed=1))
    assert len(index.snapshot(db_session).ids) == 5
    assert len(index.snapshot(db_session, force=True).ids) == 10
    index.invalidate()
    assert len(index.snapshot(db_session).ids) == 10


def test_statistics_follow_the_library_as_it_grows(db_session, library, index):
    library(random_vectors(10))
    first = index.snapshot(db_session)
    library(random_vectors(1, seed=1))  # 10 % or less: same scale
    assert index.snapshot(db_session).stats_rows == first.stats_rows == 10
    library(random_vectors(30, seed=2))
    grown = index.snapshot(db_session)
    assert grown.stats_rows == 41
    centre, scale = sim._statistics(grown.transformed)
    np.testing.assert_allclose(grown.matrix, sim._normalise(grown.transformed, centre, scale), rtol=1e-5)


def test_distance_matrix_and_unknown_ids(db_session, library, index):
    ids = library(random_vectors(6))
    known, matrix, unknown = sim.distance_matrix(db_session, [ids[2], 999, ids[0], ids[2], ids[4]], index=index)
    assert known == [ids[2], ids[0], ids[4]] and unknown == [999]
    assert matrix.shape == (3, 3)
    np.testing.assert_allclose(matrix, matrix.T, atol=1e-5)
    assert np.all(np.diag(matrix) == 0) and np.all(matrix[~np.eye(3, dtype=bool)] > 0)
    nearest = dict(sim.similar_tracks(db_session, ids[2], 5, index=index))
    assert matrix[0, 1] == pytest.approx(nearest[ids[0]], rel=1e-4)


def test_smooth_order_is_shorter_than_random_and_keeps_the_start(db_session, library, index):
    ids = library(random_vectors(60, seed=4))
    order, unknown = sim.order_by_smoothness(db_session, [*ids, 12345], start=ids[30], index=index)
    assert unknown == [12345]
    assert sorted(order) == sorted(ids) and order[0] == ids[30]
    known, matrix, _ = sim.distance_matrix(db_session, ids, index=index)
    position = {item_id: n for n, item_id in enumerate(known)}
    length = sim.path_length([position[i] for i in order], matrix)
    rng = np.random.default_rng(0)
    random_lengths = [sim.path_length(list(rng.permutation(len(ids))), matrix) for _ in range(50)]
    assert length <= min(random_lengths)
    greedy, _ = sim.order_by_smoothness(db_session, ids, start=ids[30], two_opt=False, index=index)
    assert length <= sim.path_length([position[i] for i in greedy], matrix) + 1e-4


def test_smooth_order_small_and_missing_cases(db_session, library, index):
    ids = library(random_vectors(2))
    assert sim.order_by_smoothness(db_session, [], index=index) == ([], [])
    assert sim.order_by_smoothness(db_session, [ids[0], ids[1]], start=ids[1], index=index) == ([ids[1], ids[0]], [])
    # A start without a vector: begin with the first id that has one.
    more = library(random_vectors(4, seed=3))
    order, unknown = sim.order_by_smoothness(db_session, [77, *more], start=77, index=index)
    assert order[0] == more[0] and unknown == [77]


def test_coverage_counts_tracks_with_a_current_vector(db_session, library):
    ids = library(random_vectors(3))
    store(db_session, [ids[0]], [None])  # failed
    assert sim.coverage(db_session) == (2, 3)


def test_queries_from_several_threads_while_rows_arrive(session_factory, library, db_session):
    index = sim.AudioIndex(refresh_seconds=0)
    ids = library(random_vectors(30))
    errors: list[BaseException] = []

    def query():
        db = session_factory()
        try:
            for _ in range(20):
                assert sim.similar_tracks(db, ids[0], 5, index=index)
        except BaseException as exc:  # noqa: BLE001 - reported below
            errors.append(exc)
        finally:
            db.close()

    threads = [threading.Thread(target=query) for _ in range(4)]
    for thread in threads:
        thread.start()
    for seed in range(5):
        library(random_vectors(5, seed=10 + seed))
    for thread in threads:
        thread.join(30)
    assert errors == []
    assert len(index.snapshot(db_session, force=True).ids) == 55
