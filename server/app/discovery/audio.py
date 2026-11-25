""""Sounds like": the local audio analysis (app.audio_similarity) as an
optional extra for mixes, autoplay and their ordering.

Analysis fills in over days (most played tracks first, after the loudness
pass), so every use here is coverage-aware per call: only the tracks that
have a vector take part, everything else is chosen and placed by labels
alone. With no vectors at all (a fresh install, analysis turned off) no
index is loaded and labels decide everything - `available` is one indexed
query.

Three uses:

1. Candidates. Next to the label-similar artists, the AUDIO_NEIGHBOURS
   tracks closest in sound to the seed(s) (the nearest seed counts, so
   seeds of different styles each pull their own neighbours) become
   candidates - at most AUDIO_MAX_SHARE of a mix, one per artist, only
   those within AUDIO_NEAR_DISTANCE ("related" in the analysis' scale),
   drawn more often the closer they are, and through the same eligibility,
   dislikes, dedupe and draw weights as every other candidate.
2. Scoring. A candidate whose distance to the seed(s) is known gets
   AUDIO_WEIGHT_RANGE: x0.7 at AUDIO_FAR_DISTANCE (about a random pair's
   median) or beyond, up to x1.3 for the same sound.
3. Ordering. When AUDIO_ORDER_MIN_COVERAGE of a mix's tracks have vectors,
   the cohesion ordering (app.discovery.labels._order_by_cohesion) also
   prefers the next track closest in sound - AUDIO_ORDER_WEIGHT per unit of
   distance against one shared label - within the same artist and album
   spacing rules; pairs with a missing vector count as the median distance,
   so labels place those tracks.

Distances: about 0.1-0.6 the same recording, 1-2.5 related, ~4 the median
random pair, 5-6 very different."""

from __future__ import annotations

import logging
from collections.abc import Collection, Mapping, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

logger = logging.getLogger("app.discovery")

try:  # numpy and the index are optional extras for discovery
    from app import audio_similarity
    from app.models import AudioFeatures
except Exception:  # pragma: no cover - only without numpy  # noqa: BLE001
    audio_similarity = None  # type: ignore[assignment]

AUDIO_NEIGHBOURS = 150
AUDIO_MAX_SHARE = 1 / 3
AUDIO_NEAR_DISTANCE = 2.5
AUDIO_FAR_DISTANCE = 4.0
AUDIO_CANDIDATE_BOOST = 1.0
AUDIO_WEIGHT_RANGE = (0.7, 1.3)
AUDIO_ORDER_MIN_COVERAGE = 0.6


def available(db: Session) -> bool:
    """Whether any track has a usable vector (one indexed query); loads
    nothing."""
    if audio_similarity is None:
        return False
    row = db.execute(
        select(AudioFeatures.media_item_id).where(audio_similarity._current()).limit(1)
    ).first()
    return row is not None


def closeness(distance: float) -> float:
    """1 for the same sound, falling linearly to 0 at AUDIO_FAR_DISTANCE."""
    return max(0.0, 1.0 - distance / AUDIO_FAR_DISTANCE)


def multiplier(distance: float) -> float:
    low, high = AUDIO_WEIGHT_RANGE
    return low + (high - low) * closeness(distance)


def neighbours(
    db: Session, seed_ids: Sequence[int], exclude_ids: Collection[int] = ()
) -> dict[int, float]:
    """{media id: distance} of the tracks nearest in sound to the seeds
    within AUDIO_NEAR_DISTANCE, nearest seed counting. Empty when no seed
    has a vector. The caller filters them for eligibility."""
    if not seed_ids or not available(db):
        return {}
    assert audio_similarity is not None
    found = audio_similarity.similar_tracks(
        db, list(seed_ids), AUDIO_NEIGHBOURS, exclude_ids=exclude_ids or None, combine="min"
    )
    return {media_id: distance for media_id, distance in found if distance <= AUDIO_NEAR_DISTANCE}


def distances_to(db: Session, seed_ids: Sequence[int], ids: Collection[int]) -> dict[int, float]:
    """{media id: distance to the nearest seed} for those of `ids` with a
    vector; empty when no seed has one."""
    if not seed_ids or not ids or not available(db):
        return {}
    assert audio_similarity is not None
    found = audio_similarity.similar_tracks(
        db, list(seed_ids), len(ids), restrict_to_ids=list(ids), combine="min"
    )
    return dict(found)


def apply_scores(weights: dict[int, float], distances: Mapping[int, float], ids: Collection[int]) -> None:
    """Multiply the draw weights of `ids` by their audio multiplier, in
    place, where the distance is known."""
    for media_id in ids:
        distance = distances.get(media_id)
        if distance is not None:
            weights[media_id] = weights.get(media_id, 1.0) * multiplier(distance)


def candidate_weights(distances: Mapping[int, float]) -> dict[int, float]:
    """Draw weights of audio-only candidates: closer is likelier."""
    return {media_id: 1.0 + AUDIO_CANDIDATE_BOOST * closeness(d) for media_id, d in distances.items()}


def max_audio_picks(limit: int) -> int:
    return int(limit * AUDIO_MAX_SHARE)


def order_distances(db: Session, ids: Sequence[int]) -> dict[tuple[int, int], float] | None:
    """Pairwise distances among `ids` for ordering, or None when fewer than
    AUDIO_ORDER_MIN_COVERAGE of them have a vector. Pairs involving a track
    without one are absent (the caller treats them as the median)."""
    if len(ids) < 3 or not available(db):
        return None
    assert audio_similarity is not None
    known, matrix, _unknown = audio_similarity.distance_matrix(db, list(ids))
    if len(known) < AUDIO_ORDER_MIN_COVERAGE * len(set(ids)):
        return None
    values = matrix.tolist()
    return {
        (a, b): values[i][j]
        for i, a in enumerate(known)
        for j, b in enumerate(known)
        if i != j
    }
