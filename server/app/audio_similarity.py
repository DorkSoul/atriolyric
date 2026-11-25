"""Fast "sounds like" queries over the stored audio feature vectors.

Every current vector (app.audio_features.ANALYSIS_VERSION, analysis worked)
is held in one numpy float32 matrix, loaded lazily on the first query and
then refreshed incrementally: a refresh (at most every REFRESH_SECONDS)
asks SQLite for the row count and newest ``analysed_at`` and reads only the
rows analysed since; a count that no longer adds up (a track deleted, a
re-analysis that failed) reloads everything.

Normalisation is library-wide and computed here, never stored: each
feature is first transformed (log for frequencies and rates, tempo folded
into one octave, 70-140 BPM, so a half- or double-time estimate of the same
groove still matches), then centred on the library median and divided by
its scaled median absolute deviation (a robust z-score, clipped to ±5),
then weighted. The statistics are recomputed when the number of vectors has
moved by more than RESTAT_FRACTION since they were last computed, so the
first tracks analysed do not fix the scale for the whole library.

Weights (squared weights per group sum to the group's share): timbre (MFCC
means and spreads) 1.8, brightness and noisiness (centroid, roll-off,
flatness, zero-crossing rate) 1.7, rhythm (tempo, pulse strength, onset
strength) 1.25, loudness and dynamics 1.0, tonality (mode, key strength,
key-relative chroma) 0.6. The key itself has no weight: C major and A major
songs do not differ in how they sound for this purpose. Distances are
Euclidean in that weighted space; about 0 is "the same recording", typical
pairs in one library sit around 3-6.

Memory: two float32 matrices of n x FEATURE_COUNT (about 24 MB each at
135,000 tracks) plus an id array and an id -> row dict.

Thread-safe: a lock serialises refreshes, and queries work on an immutable
snapshot that a refresh replaces whole.
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, cast

from sqlalchemy import Table, and_, func, select
from sqlalchemy.orm import Session

# numpy through app.audio_features, which limits its BLAS threads before
# the first numpy import (see there).
from app.audio_features import (
    ANALYSIS_VERSION,
    FEATURE_COUNT,
    FEATURE_INDEX,
    FEATURE_NAMES,
    VECTOR_DTYPE,
    np,
)
from app.models import AudioFeatures, MediaItem, MediaKind

REFRESH_SECONDS = 30.0
LOAD_SLICE = 10_000
RESTAT_FRACTION = 0.1
Z_CLIP = 5.0
# 2-opt is O(n^2) per pass: for longer lists the greedy path is kept as is.
TWO_OPT_MAX_TRACKS = 200
TWO_OPT_MAX_PASSES = 20

_features = cast(Table, AudioFeatures.__table__)
_items = cast(Table, MediaItem.__table__)

# Squared weight per feature; see the module docstring for the groups.
_SQUARED_WEIGHTS: dict[str, float] = {
    "tempo_bpm": 0.5,
    "tempo_strength": 0.25,
    "onset_strength": 0.5,
    "loudness_db": 0.5,
    "loudness_std_db": 0.25,
    "dynamic_range_db": 0.25,
    "centroid_hz": 0.5,
    "centroid_std_hz": 0.15,
    "rolloff_hz": 0.35,
    "flatness_db": 0.3,
    "flatness_std_db": 0.1,
    "zcr": 0.2,
    "zcr_std": 0.1,
    "key": 0.0,
    "mode": 0.1,
    "key_strength": 0.1,
    "mode_score": 0.1,
    **{f"chroma_rel_{i}": 0.3 / 12 for i in range(12)},
    **{f"mfcc_{i}": 1.5 / 12 for i in range(1, 13)},
    **{f"mfcc_std_{i}": 0.3 / 4 for i in range(1, 5)},
}
assert set(_SQUARED_WEIGHTS) == set(FEATURE_NAMES)
WEIGHTS = np.sqrt(np.array([_SQUARED_WEIGHTS[name] for name in FEATURE_NAMES], dtype=np.float32))

_LOG_FEATURES = [FEATURE_INDEX[n] for n in ("onset_strength", "centroid_hz", "centroid_std_hz", "rolloff_hz")]
_SMALL_LOG_FEATURES = [FEATURE_INDEX[n] for n in ("zcr", "zcr_std")]
_TEMPO = FEATURE_INDEX["tempo_bpm"]


def transform(raw: np.ndarray) -> np.ndarray:
    """Per-feature transform before normalisation (rows x FEATURE_COUNT).
    A missing tempo (0) becomes NaN, which normalises to the median."""
    out = np.array(raw, dtype=np.float32, copy=True)
    tempo = out[:, _TEMPO]
    with np.errstate(divide="ignore", invalid="ignore"):
        octaves = np.floor(np.log2(tempo / 70.0))
        folded = tempo / np.exp2(octaves)
        out[:, _TEMPO] = np.where(tempo > 0, np.log2(folded), np.nan)
    out[:, _LOG_FEATURES] = np.log1p(np.maximum(out[:, _LOG_FEATURES], 0.0))
    out[:, _SMALL_LOG_FEATURES] = np.log(np.maximum(out[:, _SMALL_LOG_FEATURES], 0.0) + 1e-4)
    return out


def _statistics(transformed: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    if len(transformed) == 0:
        return np.zeros(FEATURE_COUNT, np.float32), np.ones(FEATURE_COUNT, np.float32)
    centre = np.nanmedian(transformed, axis=0)
    mad = np.nanmedian(np.abs(transformed - centre), axis=0) * 1.4826
    spread = np.nanstd(transformed, axis=0)
    scale = np.where(mad > 1e-6, mad, np.where(spread > 1e-6, spread, 1.0))
    centre = np.nan_to_num(centre)
    return centre.astype(np.float32), scale.astype(np.float32)


def _normalise(transformed: np.ndarray, centre: np.ndarray, scale: np.ndarray) -> np.ndarray:
    z = np.clip((transformed - centre) / scale, -Z_CLIP, Z_CLIP)
    return (np.nan_to_num(z) * WEIGHTS).astype(np.float32)


@dataclass(frozen=True)
class _Snapshot:
    ids: np.ndarray  # int64 (n,)
    transformed: np.ndarray  # float32 (n, F)
    matrix: np.ndarray  # float32 (n, F), normalised and weighted
    sq_norms: np.ndarray  # float32 (n,)
    row_of: dict[int, int]
    centre: np.ndarray
    scale: np.ndarray
    stats_rows: int
    db_count: int
    newest: datetime | None


def _empty() -> _Snapshot:
    centre, scale = _statistics(np.zeros((0, FEATURE_COUNT), np.float32))
    return _Snapshot(
        ids=np.zeros(0, np.int64),
        transformed=np.zeros((0, FEATURE_COUNT), np.float32),
        matrix=np.zeros((0, FEATURE_COUNT), np.float32),
        sq_norms=np.zeros(0, np.float32),
        row_of={},
        centre=centre,
        scale=scale,
        stats_rows=0,
        db_count=0,
        newest=None,
    )


def _current():
    return and_(_features.c.version == ANALYSIS_VERSION, _features.c.vector.is_not(None))


def _decode(blobs: list[bytes]) -> np.ndarray:
    size = FEATURE_COUNT * VECTOR_DTYPE.itemsize
    good = [b if len(b) == size else bytes(size) for b in blobs]
    return np.frombuffer(b"".join(good), dtype=VECTOR_DTYPE).reshape(len(blobs), FEATURE_COUNT)


class AudioIndex:
    """The in-memory matrix. One per process (``audio_index``); tests make
    their own."""

    def __init__(self, refresh_seconds: float = REFRESH_SECONDS) -> None:
        self.refresh_seconds = refresh_seconds
        self._lock = threading.Lock()
        self._snapshot: _Snapshot | None = None
        self._checked_at = -math.inf

    def invalidate(self) -> None:
        with self._lock:
            self._snapshot = None
            self._checked_at = -math.inf

    def snapshot(self, db: Session, force: bool = False) -> _Snapshot:
        """The current snapshot, refreshed first when REFRESH_SECONDS have
        passed since the last check (or `force`)."""
        with self._lock:
            now = time.monotonic()
            if self._snapshot is not None and not force and now - self._checked_at < self.refresh_seconds:
                return self._snapshot
            self._snapshot = self._refresh(db, self._snapshot)
            self._checked_at = time.monotonic()
            return self._snapshot

    def _refresh(self, db: Session, old: _Snapshot | None) -> _Snapshot:
        count, newest = db.execute(
            select(func.count(), func.max(_features.c.analysed_at)).where(_current())
        ).one()
        if old is not None and count == old.db_count and newest == old.newest:
            return old
        if old is None or old.newest is None or count < old.db_count:
            return self._load_all(db)
        rows = db.execute(
            select(_features.c.media_item_id, _features.c.vector, _features.c.analysed_at).where(
                _current(), _features.c.analysed_at >= old.newest
            )
        ).all()
        ids = np.array([r.media_item_id for r in rows], dtype=np.int64)
        transformed = transform(_decode([r.vector for r in rows])) if rows else np.zeros((0, FEATURE_COUNT), np.float32)
        replace = [(i, old.row_of[int(item_id)]) for i, item_id in enumerate(ids) if int(item_id) in old.row_of]
        fresh = [i for i, item_id in enumerate(ids) if int(item_id) not in old.row_of]
        if len(old.ids) + len(fresh) != count:
            return self._load_all(db)  # something was deleted or failed on re-analysis
        all_transformed = np.concatenate([old.transformed, transformed[fresh]])
        for i, row in replace:
            all_transformed[row] = transformed[i]
        all_ids = np.concatenate([old.ids, ids[fresh]])
        row_of = dict(old.row_of)
        row_of.update({int(item_id): len(old.ids) + j for j, item_id in enumerate(ids[fresh])})
        newest_seen = max([r.analysed_at for r in rows], default=old.newest)
        return self._build(all_ids, all_transformed, row_of, count, newest_seen, old)

    def _load_all(self, db: Session) -> _Snapshot:
        # Streamed in slices, so the whole table's rows never sit in memory
        # as Python objects at once (the load's peak stays near the result).
        result = db.execute(
            select(_features.c.media_item_id, _features.c.vector, _features.c.analysed_at)
            .where(_current())
            .order_by(_features.c.media_item_id)
            .execution_options(yield_per=LOAD_SLICE)
        )
        id_parts, value_parts, newest = [], [], None
        for part in result.partitions():
            id_parts.append(np.array([r.media_item_id for r in part], dtype=np.int64))
            value_parts.append(transform(_decode([r.vector for r in part])))
            part_newest = max(r.analysed_at for r in part)
            newest = part_newest if newest is None else max(newest, part_newest)
        if not id_parts:
            return _empty()
        ids = np.concatenate(id_parts)
        transformed = np.concatenate(value_parts)
        row_of = {int(item_id): i for i, item_id in enumerate(ids)}
        return self._build(ids, transformed, row_of, len(ids), newest, None)

    @staticmethod
    def _build(ids, transformed, row_of, count, newest, old: _Snapshot | None) -> _Snapshot:
        n = len(ids)
        if old is None or old.stats_rows == 0 or abs(n - old.stats_rows) > RESTAT_FRACTION * old.stats_rows:
            centre, scale = _statistics(transformed)
            stats_rows = n
            matrix = _normalise(transformed, centre, scale)
        else:
            centre, scale, stats_rows = old.centre, old.scale, old.stats_rows
            # Only the rows that are new or replaced need normalising; the
            # rest keep their row in the old matrix.
            matrix = np.empty_like(transformed)
            kept = len(old.ids)
            matrix[:kept] = old.matrix
            changed = np.flatnonzero(np.any(transformed[:kept] != old.transformed, axis=1))
            matrix[changed] = _normalise(transformed[changed], centre, scale)
            matrix[kept:] = _normalise(transformed[kept:], centre, scale)
        return _Snapshot(
            ids=ids,
            transformed=transformed,
            matrix=matrix,
            sq_norms=np.einsum("ij,ij->i", matrix, matrix),
            row_of=row_of,
            centre=centre,
            scale=scale,
            stats_rows=stats_rows,
            db_count=count,
            newest=newest,
        )


audio_index = AudioIndex()


def coverage(db: Session) -> tuple[int, int]:
    """(tracks with a current vector, all tracks)."""
    analysed = db.execute(
        select(func.count())
        .select_from(_features.join(_items, _items.c.id == _features.c.media_item_id))
        .where(_current(), _items.c.kind == MediaKind.track.value)
    ).scalar_one()
    total = db.execute(
        select(func.count()).select_from(_items).where(_items.c.kind == MediaKind.track.value)
    ).scalar_one()
    return analysed, total


def _rows(snapshot: _Snapshot, ids) -> tuple[list[int], list[int], list[int]]:
    """(known ids, their rows, unknown ids), in the given order, no repeats."""
    known, rows, unknown, seen = [], [], [], set()
    for item_id in ids:
        item_id = int(item_id)
        if item_id in seen:
            continue
        seen.add(item_id)
        row = snapshot.row_of.get(item_id)
        if row is None:
            unknown.append(item_id)
        else:
            known.append(item_id)
            rows.append(row)
    return known, rows, unknown


def similar_tracks(
    db: Session,
    seeds: int | list[int] | tuple[int, ...],
    k: int = 50,
    *,
    exclude_ids=None,
    restrict_to_ids=None,
    combine: Literal["min", "centroid"] = "min",
    index: AudioIndex | None = None,
) -> list[tuple[int, float]]:
    """The `k` tracks that sound most like the seed(s), nearest first, as
    (media_item_id, distance). Seeds are never returned, nor `exclude_ids`;
    with `restrict_to_ids` only those are candidates (pass the user's
    accessible tracks). Seeds without a vector are ignored; with none left
    the result is empty, so the caller falls back to its other signals.

    Several seeds: "min" (the default) scores a candidate by its distance to
    the nearest seed, so seeds of different styles each pull in their own
    neighbours; "centroid" measures from the seeds' mean vector, which suits
    seeds that already sound alike but, for seeds that do not, lands in the
    empty space between them and returns music that resembles none."""
    snapshot = (index or audio_index).snapshot(db)
    seed_list = [seeds] if isinstance(seeds, int) else list(seeds)
    _, seed_rows, _ = _rows(snapshot, seed_list)
    if not seed_rows or k <= 0:
        return []
    if restrict_to_ids is None:
        candidates = None
        matrix, sq_norms, ids = snapshot.matrix, snapshot.sq_norms, snapshot.ids
    else:
        _, rows, _ = _rows(snapshot, restrict_to_ids)
        if not rows:
            return []
        candidates = np.array(rows, dtype=np.int64)
        matrix, sq_norms, ids = (
            snapshot.matrix[candidates],
            snapshot.sq_norms[candidates],
            snapshot.ids[candidates],
        )
    queries = snapshot.matrix[seed_rows]
    if combine == "centroid":
        queries = queries.mean(axis=0, keepdims=True)
    elif combine != "min":
        raise ValueError(f"unknown combine mode {combine!r}")
    squared = sq_norms[:, None] - 2.0 * (matrix @ queries.T) + np.einsum("ij,ij->i", queries, queries)[None, :]
    distances = np.sqrt(np.maximum(squared.min(axis=1), 0.0))
    excluded = {int(i) for i in seed_list}
    if exclude_ids is not None:
        excluded.update(int(i) for i in exclude_ids)
    if excluded:
        distances[np.isin(ids, np.fromiter(excluded, dtype=np.int64))] = np.inf
    count = min(k, int(np.isfinite(distances).sum()))
    if count == 0:
        return []
    top = np.argpartition(distances, count - 1)[:count]
    top = top[np.lexsort((ids[top], distances[top]))]
    return [(int(ids[i]), float(distances[i])) for i in top]


def distance_matrix(
    db: Session, ids, index: AudioIndex | None = None
) -> tuple[list[int], np.ndarray, list[int]]:
    """(ids with a vector, their pairwise distances as a float32 matrix in
    that order, ids without one). For ordering a small set: n x n."""
    snapshot = (index or audio_index).snapshot(db)
    known, rows, unknown = _rows(snapshot, ids)
    vectors = snapshot.matrix[rows] if rows else np.zeros((0, FEATURE_COUNT), np.float32)
    norms = np.einsum("ij,ij->i", vectors, vectors)
    squared = norms[:, None] - 2.0 * (vectors @ vectors.T) + norms[None, :]
    matrix = np.sqrt(np.maximum(squared, 0.0)).astype(np.float32)
    np.fill_diagonal(matrix, 0.0)
    return known, matrix, unknown


def path_length(order: list[int], distances: np.ndarray) -> float:
    """Sum of consecutive distances along `order` (indices into `distances`)."""
    return float(sum(distances[a, b] for a, b in zip(order, order[1:], strict=False)))


def _greedy(distances: np.ndarray, start: int) -> list[int]:
    n = len(distances)
    order = [start]
    left = np.ones(n, dtype=bool)
    left[start] = False
    for _ in range(n - 1):
        row = np.where(left, distances[order[-1]], np.inf)
        nxt = int(np.argmin(row))
        order.append(nxt)
        left[nxt] = False
    return order


def _two_opt(order: list[int], distances: np.ndarray) -> list[int]:
    """Reverse stretches of an open path while that shortens it; the first
    element stays first."""
    n = len(order)
    path = list(order)
    for _ in range(TWO_OPT_MAX_PASSES):
        improved = False
        for i in range(n - 2):
            a, b = path[i], path[i + 1]
            for j in range(i + 2, n):
                c = path[j]
                e = path[j + 1] if j + 1 < n else None
                before = distances[a, b] + (distances[c, e] if e is not None else 0.0)
                after = distances[a, c] + (distances[b, e] if e is not None else 0.0)
                if after < before - 1e-6:
                    path[i + 1 : j + 1] = reversed(path[i + 1 : j + 1])
                    a, b = path[i], path[i + 1]
                    improved = True
        if not improved:
            break
    return path


def order_by_smoothness(
    db: Session,
    ids,
    *,
    start: int | None = None,
    two_opt: bool = True,
    index: AudioIndex | None = None,
) -> tuple[list[int], list[int]]:
    """(ids with a vector in a smooth listening order, ids without one).

    A greedy nearest-neighbour path from `start` (or, when it is missing or
    has no vector, from the first id that has one), improved by 2-opt for
    up to TWO_OPT_MAX_TRACKS tracks: each next track is the one closest in
    sound, so tempo, loudness and timbre change gradually. The unplaced ids
    are left for the caller to put where its own logic says."""
    known, distances, unknown = distance_matrix(db, ids, index=index)
    if len(known) <= 2:
        if start is not None and start in known:
            known.remove(start)
            known.insert(0, start)
        return known, unknown
    first = known.index(start) if start is not None and start in known else 0
    order = _greedy(distances, first)
    if two_opt and len(order) <= TWO_OPT_MAX_TRACKS:
        order = _two_opt(order, distances)
    return [known[i] for i in order], unknown
