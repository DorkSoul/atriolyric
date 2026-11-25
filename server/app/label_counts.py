"""How many tracks carry each genre and tag, cached per library version.

Counting every label is a whole-library query: on a large library it takes
longer than the web app waits for an answer, so app.cache_warmup recounts
in the background at startup and after every library change."""

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import library_cache
from app.access import accessible_root_ids
from app.library_query import label_track_rows
from app.models import CollectionPreference, TrackDislike, User
from app.preferences import not_disliked

KINDS = ("genre", "tag")


def label_counts(
    db: Session, user: User, kind: str, *, mixable: bool = False
) -> dict[tuple[str, str], int]:
    """Accessible tracks per (kind, name) genre and tag, outside ignored
    folders, in name order: the shared app.library_query.label_track_rows
    (so /genres and /mixes/options always agree on what a label is) cached
    per library version and root set, so whole-library UNIONs do not run
    on every request.

    `mixable` leaves out the tracks this user dislikes, as the automatic
    mixes' qualification counts and the mixes themselves do, and is cached
    per user. (The length/album-type rules of app.discovery.eligibility are
    not applied here: they are server settings, and a label's count is what
    the user browses by.)"""
    roots = accessible_root_ids(db, user)
    # Without any dislikes the per-user count is the shared one.
    mixable = mixable and _has_dislikes(db, user)

    def compute() -> dict[tuple[str, str], int]:
        filters = (not_disliked(user.id),) if mixable else ()
        rows = label_track_rows(roots, *filters, kinds=(kind,)).subquery()
        return {
            (kind, name): count
            for kind, name, count in db.execute(
                select(rows.c.kind, rows.c.name, func.count(rows.c.media_item_id))
                .group_by(rows.c.kind, rows.c.name)
                .order_by(rows.c.kind, rows.c.name)
            )
        }

    key = ("label_counts", kind, tuple(sorted(roots)), user.id if mixable else None)
    return library_cache.get_or_compute(key, compute)


def _has_dislikes(db: Session, user: User) -> bool:
    return (
        db.execute(select(TrackDislike.media_item_id).where(TrackDislike.user_id == user.id).limit(1)).first()
        is not None
        or db.execute(
            select(CollectionPreference.id).where(
                CollectionPreference.user_id == user.id, CollectionPreference.state == "disliked"
            ).limit(1)
        ).first()
        is not None
    )
