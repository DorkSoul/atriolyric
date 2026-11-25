"""Audio "sounds like" candidates for seeded mixes (Song Mix, autoplay,
artist/album mixes, a cluster mix's "new to you" share) - see
app.discovery.audio for the rules."""

from __future__ import annotations

import random
from collections.abc import Callable, Collection, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.discovery import audio
from app.discovery.eligibility import mix_eligible
from app.discovery.familiarity import Kind, Taste, draw_weights
from app.discovery.sampling import _balanced_track_sample, _Pick, _picks
from app.folder_exclusions import not_in_ignored_folder
from app.models import MediaItem, MediaKind, User
from app.preferences import liked_levels, not_disliked


def sounds_like(
    db: Session,
    user: User,
    accessible: Collection[int],
    seed_ids: Sequence[int],
    pool: Sequence[_Pick],
    weights: dict[int, float],
    skip: Collection[int],
    taste: Taste,
    kind: Kind,
    limit: int,
    used: set[tuple],
    versions: dict[int, list[_Pick]],
    liked: dict[int, str],
    rng: random.Random | None,
    accept: Callable[[_Pick], bool] | None = None,
) -> list[_Pick]:
    """The audio extras of a seeded mix (app.discovery.audio): scales the
    pool's `weights` by how close each track sounds to the seeds (in place),
    then draws up to `limit` tracks - one per artist - from the tracks
    nearest in sound that are not in the pool, eligible, not disliked, not
    in `skip` and (when given) `accept`ed. Empty when no seed has a
    vector."""
    audio.apply_scores(weights, audio.distances_to(db, seed_ids, [item.id for item in pool]), [i.id for i in pool])
    in_pool = {item.id for item in pool}
    near = audio.neighbours(db, seed_ids, exclude_ids=set(skip) | in_pool)
    if not near or limit <= 0:
        return []
    candidates = [
        pick
        for pick in _picks(
            db,
            select(MediaItem).where(
                MediaItem.id.in_(list(near)),
                MediaItem.library_root_id.in_(accessible),
                MediaItem.kind == MediaKind.track,
                not_in_ignored_folder(),
                not_disliked(user.id),
                mix_eligible(),
            ),
        )
        if accept is None or accept(pick)
    ]
    if not candidates:
        return []
    candidate_liked = liked_levels(db, user, [pick.id for pick in candidates])
    liked.update(candidate_liked)
    candidate_weights = draw_weights(taste, candidates, kind, audio.candidate_weights(near))
    weights.update(candidate_weights)
    return _balanced_track_sample(
        candidates, limit, candidate_liked, candidate_weights, 1, rng=rng, used=used, versions=versions
    )
