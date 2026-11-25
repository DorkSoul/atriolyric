"""How listens end, and what that says about a user's taste.

Recording
---------
The device producing the sound reports every track or music video it
started and then left (`POST /api/stream/{id}/listened`, see
`record_listen`), once, with how it ended:

* ``finished``    - it played to its end (the media element's `ended`).
* ``skipped``     - the user moved on while it was playing: Next or Previous,
  another item picked, the item removed from the queue.
* ``stopped``     - it was left while paused, or playback stopped / the queue
  was cleared.
* ``transferred`` - another device took over the playback.
* ``error``       - it could not be played (out of retries, missing or
  unsupported file). Never counts against the track.
* ``unknown``     - the page went away (closed, reloaded) while it was on.

and what started it when the client knows: ``auto`` (the queue moved on by
itself after the previous item ended, or Next took the queue's next item) or
``user`` (the user picked this item: started a list or album at it, jumped to
it in the queue, went back to it with Previous). The client does not report
a listen left with less than two seconds heard (except an error): that is
scanning past items (Next, Next, Next), not a judgement of them.

Plays (`play_history`) and scrobbles are untouched by this: a counted play
is still reported on its own, and its `started_at` is the listen's.

Reading
-------
`listen_stats_for_items` and `listen_stats_for_artists` sum a user's listens
in one grouped query each into `ListenStats`: counts, and a smoothed
preference in (0, 1) that consumers can use as a signal (0.5 is neutral, the
value for anything never heard - `NO_LISTENS`).
"""

from __future__ import annotations

from collections.abc import Collection, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Integer, and_, case, cast, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.artist_credits import ROLE_FEATURED, ROLE_MAIN
from app.models import ListenEvent, TrackArtist, utcnow
from app.schemas import ListenedReport

# --- what counts as what ----------------------------------------------------

# A skip with less than this heard is an early skip: rejected on first
# impression. There is no canonical number (Spotify's skip-prediction
# dataset only has "very brief / brief / most of it" classes); 30 s is the
# point at which the big services count a stream, and covers the intro of
# most songs - under it the listener did not give the song a chance.
EARLY_SKIP_SECONDS = 30.0
# A skip after this share of the track was heard is the outro or fade-out
# being cut short: counted as finished, not as a skip.
NEAR_END_FRACTION = 0.9
# Below this heard, a skip is scanning, not a judgement. The client does not
# report those; one that arrives anyway is ignored here too.
MIN_LISTEN_SECONDS = 2.0

# --- how much each outcome weighs ------------------------------------------

FINISHED_WEIGHT = 1.0
EARLY_SKIP_WEIGHT = 1.0
# Heard 30 s or more and then skipped: given a chance, still not wanted.
LATE_SKIP_WEIGHT = 0.4
# A skip of an item the user picked themselves is weaker evidence than one of
# an item the queue chose (sampling a list, a mis-tap, "not now"). Listens
# with no start kind count as queue-chosen.
USER_STARTED_SKIP_FACTOR = 0.5
# Recent behaviour matters more: a listen's weight halves every this many days.
HALF_LIFE_DAYS = 90.0
# The preference is a Beta-style estimate, (positive + A) / (positive +
# negative + A + B): with no listens it is 0.5, one early skip moves it to
# 0.4 and one finish to 0.6 - no single listen bans or crowns a track.
PRIOR_POSITIVE = 2.0
PRIOR_NEGATIVE = 2.0

# The reasons that say something about taste; transferred, error and unknown
# never do.
COUNTED_REASONS = ("finished", "skipped", "stopped")

# Up to this many ids go into the query as a list; a larger set (a whole
# library) reads all the user's listens and keeps the wanted ones, still in
# one statement (SQLite limits the number of bound parameters).
_MAX_BOUND_IDS = 500

# Report bounds (see record_listen), as for play reports.
LISTEN_MAX_AGE = timedelta(days=14)
LISTEN_MAX_FUTURE = timedelta(minutes=5)


@dataclass(frozen=True)
class ListenStats:
    """One item's (or artist's) listens, by how they ended.

    `finished` includes skips in the last 10% (NEAR_END_FRACTION);
    `total` is finished + skipped_early + skipped_late + stopped (errors,
    transfers, unknown ends and sub-2-second skips are left out).
    `positive` / `negative` are the decayed, weighted evidence and
    `preference` the smoothed estimate in (0, 1), 0.5 neutral."""

    finished: int = 0
    skipped_early: int = 0
    skipped_late: int = 0
    stopped: int = 0
    positive: float = 0.0
    negative: float = 0.0

    @property
    def total(self) -> int:
        return self.finished + self.skipped_early + self.skipped_late + self.stopped

    @property
    def preference(self) -> float:
        return preference(self.positive, self.negative)


NO_LISTENS = ListenStats()


def preference(positive: float, negative: float) -> float:
    """(positive + A) / (positive + negative + A + B): bounded in (0, 1)."""
    return (positive + PRIOR_POSITIVE) / (positive + negative + PRIOR_POSITIVE + PRIOR_NEGATIVE)


def decay(age_days: float) -> float:
    """A listen's weight after `age_days` (never more than 1)."""
    return 0.5 ** (max(0.0, age_days) / HALF_LIFE_DAYS)


def skip_weight(category: str, user_started: bool) -> float:
    """The negative weight of one undecayed skip of this category."""
    base = EARLY_SKIP_WEIGHT if category == "early" else LATE_SKIP_WEIGHT
    return base * (USER_STARTED_SKIP_FACTOR if user_started else 1.0)


# --- recording --------------------------------------------------------------


def _naive_utc(moment: datetime) -> datetime:
    if moment.tzinfo is not None:
        return moment.astimezone(UTC).replace(tzinfo=None)
    return moment


class ListenOutOfRange(ValueError):
    """started_at / ended_at outside what the server accepts."""


def record_listen(db: Session, user_id: int, media_item_id: int, report: ListenedReport) -> bool:
    """Stores one listen; False when it was already recorded (a retry of
    the same user, item and start). The caller has checked the item.
    Raises ListenOutOfRange for a start more than 14 days old or in the
    future, or an end before the start or in the future."""
    now = utcnow()
    started_at = _naive_utc(report.started_at)
    ended_at = _naive_utc(report.ended_at) if report.ended_at is not None else now
    if started_at < now - LISTEN_MAX_AGE or started_at > now + LISTEN_MAX_FUTURE:
        raise ListenOutOfRange("started_at is out of range")
    if ended_at < started_at or ended_at > now + LISTEN_MAX_FUTURE:
        raise ListenOutOfRange("ended_at is out of range")
    duplicate = db.execute(
        select(ListenEvent.id)
        .where(
            ListenEvent.user_id == user_id,
            ListenEvent.media_item_id == media_item_id,
            ListenEvent.started_at == started_at,
        )
        .limit(1)
    ).first()
    if duplicate is not None:
        return False
    db.add(
        ListenEvent(
            user_id=user_id,
            media_item_id=media_item_id,
            started_at=started_at,
            ended_at=ended_at,
            listened_seconds=report.listened_seconds,
            duration_seconds=report.duration_seconds,
            position_seconds=report.position_seconds,
            end_reason=report.end_reason,
            start_kind=report.start_kind,
            source_kind=report.source_kind,
        )
    )
    try:
        db.commit()
    except IntegrityError:
        # Two retries of the same listen racing each other.
        db.rollback()
        return False
    return True


# --- reading ----------------------------------------------------------------


def _category() -> Any:
    listened = ListenEvent.listened_seconds
    duration = ListenEvent.duration_seconds
    skipped = ListenEvent.end_reason == "skipped"
    return case(
        (ListenEvent.end_reason == "finished", "finished"),
        (and_(skipped, duration > 0, listened >= duration * NEAR_END_FRACTION), "finished"),
        (and_(skipped, listened < MIN_LISTEN_SECONDS), "ignored"),
        (and_(skipped, listened < EARLY_SKIP_SECONDS), "early"),
        (skipped, "late"),
        else_="stopped",
    )


def _aggregate(rows: Iterable[Any], wanted: Collection[int] | None) -> dict[int, ListenStats]:
    """Folds (key, category, user_started, age_days, count) rows."""
    sums: dict[int, list[float]] = {}
    for key, category, user_started, age_days, count in rows:
        if category == "ignored" or (wanted is not None and key not in wanted):
            continue
        entry = sums.setdefault(key, [0, 0, 0, 0, 0.0, 0.0])
        weight = decay(float(age_days or 0)) * count
        if category == "finished":
            entry[0] += count
            entry[4] += FINISHED_WEIGHT * weight
        elif category in ("early", "late"):
            entry[1 if category == "early" else 2] += count
            entry[5] += skip_weight(category, bool(user_started)) * weight
        else:
            entry[3] += count
    return {
        key: ListenStats(
            finished=int(e[0]),
            skipped_early=int(e[1]),
            skipped_late=int(e[2]),
            stopped=int(e[3]),
            positive=e[4],
            negative=e[5],
        )
        for key, e in sums.items()
    }


def _grouped(key_column: Any, user_id: int, now: datetime, from_clause: Any = ListenEvent) -> Any:
    category = _category().label("category")
    user_started = (ListenEvent.start_kind == "user").label("user_started")
    # Whole days: rows of the same day group together, and the decay of a
    # day is applied once per group.
    age_days = cast(
        func.julianday(now) - func.julianday(ListenEvent.started_at), Integer
    ).label("age_days")
    return (
        select(key_column, category, user_started, age_days, func.count())
        .select_from(from_clause)
        .where(ListenEvent.user_id == user_id, ListenEvent.end_reason.in_(COUNTED_REASONS))
        .group_by(key_column, category, user_started, age_days)
    )


def listen_stats_for_items(
    db: Session,
    user_id: int,
    media_item_ids: Collection[int] | None = None,
    *,
    now: datetime | None = None,
) -> dict[int, ListenStats]:
    """The user's listens per media item: every item with any (None), or
    only these. Items never heard are absent - use NO_LISTENS for them.
    One statement whatever the number of ids."""
    query = _grouped(ListenEvent.media_item_id, user_id, now or utcnow())
    wanted = None
    if media_item_ids is not None:
        wanted = set(media_item_ids)
        if not wanted:
            return {}
        if len(wanted) <= _MAX_BOUND_IDS:
            query = query.where(ListenEvent.media_item_id.in_(wanted))
    return _aggregate(db.execute(query), wanted)


def listen_stats_for_artists(
    db: Session,
    user_id: int,
    artist_ids: Collection[int] | None = None,
    *,
    now: datetime | None = None,
) -> dict[int, ListenStats]:
    """The same per artist (artists.id): a listen counts for every main or
    featured artist of its track, once each. One statement."""
    credits = (
        select(TrackArtist.media_item_id, TrackArtist.artist_id)
        .where(TrackArtist.role.in_((ROLE_MAIN, ROLE_FEATURED)))
        .distinct()
        .subquery()
    )
    joined = ListenEvent.__table__.join(
        credits, credits.c.media_item_id == ListenEvent.media_item_id
    )
    query = _grouped(credits.c.artist_id, user_id, now or utcnow(), joined)
    wanted = None
    if artist_ids is not None:
        wanted = set(artist_ids)
        if not wanted:
            return {}
        if len(wanted) <= _MAX_BOUND_IDS:
            query = query.where(credits.c.artist_id.in_(wanted))
    return _aggregate(db.execute(query), wanted)
