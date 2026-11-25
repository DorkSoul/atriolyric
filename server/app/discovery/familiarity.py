"""How established each track and artist is for a user, how worn out they
are right now, and how the user's listens judge them - folded into the one
draw weight every mix kind and autoplay sample by.

Familiarity and fatigue (ACT-R base-level activation)
-----------------------------------------------------
Following the music relistening work of Reiter-Haas et al. (2021,
"Predicting Music Relistening Behavior Using the ACT-R Framework"), the
memory strength of a track is the sum over its past plays of the play's
age to the power -d:

    strength = sum over plays of age ** -ACTR_DECAY        B = ln(strength)

with d = ACTR_DECAY = 0.5 (the standard ACT-R value the paper uses). Ages are
in days; plays are grouped by whole day of age and each counts at the middle
of its day, so a play a minute ago counts as half a day old (the floor: it
cannot explode). Only the MAX_PLAYS_PER_TRACK most recent plays of a track
(MAX_PLAYS_PER_ARTIST of an artist) are summed - the oldest add almost
nothing anyway. Examples: one play yesterday ~ 0.8, one a year ago ~ 0.05,
twenty spread over the last year ~ 2.

* familiarity (0..1) = strength / (strength + half), a logistic curve in B
  centred on ln(half): FAMILIARITY_HALF_STRENGTH for a track (2.0: about
  twenty plays over a year, or one play today and a few last month, make a
  track half-established), ARTIST_FAMILIARITY_HALF_STRENGTH for an artist
  (all its tracks' plays together). Never played is 0.
* fatigue (0..1) is the same sum over the last FATIGUE_WINDOW_DAYS only,
  through a steeper curve: f^2 / (f^2 + half^2) with FATIGUE_HALF_STRENGTH
  (5.0) for a track: one play today 0.07, two today 0.24, five over the last
  three days (two today, two yesterday, one the day before) 0.51, ten (four,
  three, three) 0.80. An artist's uses ARTIST_FATIGUE_HALF_STRENGTH (12.0,
  over all its tracks' plays): an afternoon of one artist, fifteen plays
  today, 0.76; the same fifteen three days later 0.31, and 0 once they are
  a week old.

Both come from one grouped query over the user's play_history (plays per
track per day of age), cached per user until a play is added or removed, the
day changes or the library changes.

Listen outcomes
---------------
app.listening's smoothed preference (0..1, 0.5 = no evidence) per track and
per artist becomes a multiplier: a track's 0.25 + 1.5 p (x0.25 .. x1.75),
its artist's 0.6 + 0.8 p (x0.6 .. x1.4: an artist the user keeps skipping
lowers all its tracks moderately), the two multiplied and kept within
x0.25 .. x1.75. Explicit dislikes stay hard excludes (the pools' SQL) and
explicit likes keep the sampler's LIKE_BOOST.

The draw weight
---------------
    weight = base x preference x familiarity term x fatigue term
    (then x LIKE_BOOST in the sampler for a liked track), kept within
    MIN_TRACK_WEIGHT .. MAX_TRACK_WEIGHT

base is the mix's own weight (a ListenBrainz match, 1 otherwise). The terms by
`kind`:

* "mix" (daily, cluster, specified, Song and artist/album mixes): familiarity
  1 + FAMILIAR_BOOST x familiarity (a familiar track is liked a little more,
  up to x1.5); fatigue (1 - FATIGUE_PENALTY x track fatigue) x
  (1 - ARTIST_FATIGUE_PENALTY x artist fatigue).
* "plain" (autoplay, and a cluster mix's "new to you" share): familiarity 1,
  the same fatigue term.
* "discovery": familiarity (1 - familiarity) ** DISCOVERY_FAMILIARITY_EXPONENT
  (an unplayed track 1, a half-established one 0.25), no fatigue term -
  tracks at DISCOVERY_FATIGUE_EXCLUDE or above are left out altogether.

With no plays and no listens every term is 1, so a fresh install draws by
the mix's own weights alone.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from sqlalchemy import Integer, cast, func, select
from sqlalchemy.orm import Session

from app.discovery.common import MAX_TRACK_WEIGHT
from app.discovery.eligibility import (
    _cached,
    _collapse_duets,
    artist_identity,
    main_credits,
)
from app.library_cache import library_version
from app.listening import listen_stats_for_artists, listen_stats_for_items
from app.models import Artist, ListenEvent, MediaItem, PlayHistory, User, utcnow

logger = logging.getLogger("app.discovery")

ACTR_DECAY = 0.5
MAX_PLAYS_PER_TRACK = 50
MAX_PLAYS_PER_ARTIST = 250
FAMILIARITY_HALF_STRENGTH = 2.0
ARTIST_FAMILIARITY_HALF_STRENGTH = 4.0
FATIGUE_WINDOW_DAYS = 7
# A play today counts 0.5 ** -0.5 = 1.41 towards the recent strength, so
# with 5.0 two plays today stay at fatigue 0.24 and it takes about five plays
# over three days to reach 0.5 (Discovery's cut-off; see the docstring table).
FATIGUE_HALF_STRENGTH = 5.0
# An artist sums every track's plays: fifteen today make 0.76 (the mix weight
# x0.77 at most, never zero), gone back to 0.31 three days later.
ARTIST_FATIGUE_HALF_STRENGTH = 12.0
# The window whose plays say what the user listens to now (label choice for
# the daily mixes, app.discovery.mixes).
RECENT_WINDOW_DAYS = 90

FAMILIAR_BOOST = 0.5
FATIGUE_PENALTY = 0.8
ARTIST_FATIGUE_PENALTY = 0.3
DISCOVERY_FAMILIARITY_EXPONENT = 2.0
DISCOVERY_FATIGUE_EXCLUDE = 0.5

ITEM_PREFERENCE_RANGE = (0.25, 1.75)
ARTIST_PREFERENCE_RANGE = (0.6, 1.4)
PREFERENCE_BOUNDS = (0.25, 1.75)
MIN_TRACK_WEIGHT = 0.05

Kind = Literal["mix", "plain", "discovery"]


def strength_of(day_counts: Iterable[tuple[int, int]], cap: int) -> float:
    """sum of age ** -d over the `cap` most recent plays, from (whole days
    of age, plays that day) pairs; each day's plays count at its middle."""
    total = 0.0
    left = cap
    for age, count in sorted(day_counts):
        if left <= 0:
            break
        used = min(count, left)
        left -= used
        total += used * (max(age, 0) + 0.5) ** -ACTR_DECAY
    return total


def familiarity_of(strength: float, half: float = FAMILIARITY_HALF_STRENGTH) -> float:
    """Logistic in B = ln(strength), 0.5 at strength == half; 0 when never played."""
    return strength / (strength + half) if strength > 0 else 0.0


def fatigue_of(recent_strength: float, half: float = FATIGUE_HALF_STRENGTH) -> float:
    square = recent_strength * recent_strength
    return square / (square + half * half) if square > 0 else 0.0


def preference_multiplier(item_preference: float, artist_preference: float) -> float:
    """The listen-outcome multiplier: 1 for neutral (0.5) evidence."""
    low, high = ITEM_PREFERENCE_RANGE
    item = low + (high - low) * item_preference
    low, high = ARTIST_PREFERENCE_RANGE
    artist = low + (high - low) * artist_preference
    return min(PREFERENCE_BOUNDS[1], max(PREFERENCE_BOUNDS[0], item * artist))


@dataclass
class Taste:
    """One user's listening, summarised for drawing mixes (see the module
    docstring). Track maps are keyed by media id, artist maps by the
    normalised artist identity the samplers balance under."""

    track_strength: dict[int, float] = field(default_factory=dict)
    track_recent: dict[int, float] = field(default_factory=dict)
    track_window: dict[int, float] = field(default_factory=dict)
    artist_strength: dict[str, float] = field(default_factory=dict)
    artist_recent: dict[str, float] = field(default_factory=dict)
    artist_tags: dict[str, dict[str, float]] = field(default_factory=dict)
    item_preference: dict[int, float] = field(default_factory=dict)
    artist_preference: dict[str, float] = field(default_factory=dict)

    @property
    def has_plays(self) -> bool:
        return bool(self.track_strength)

    @property
    def has_evidence(self) -> bool:
        return bool(self.track_strength or self.item_preference or self.artist_preference)

    def familiarity(self, media_id: int) -> float:
        return familiarity_of(self.track_strength.get(media_id, 0.0))

    def fatigue(self, media_id: int) -> float:
        return fatigue_of(self.track_recent.get(media_id, 0.0))

    def artist_familiarity(self, artist_key: str | None) -> float:
        if artist_key is None:
            return 0.0
        return familiarity_of(self.artist_strength.get(artist_key, 0.0), ARTIST_FAMILIARITY_HALF_STRENGTH)

    def artist_fatigue(self, artist_key: str | None) -> float:
        if artist_key is None:
            return 0.0
        return fatigue_of(self.artist_recent.get(artist_key, 0.0), ARTIST_FATIGUE_HALF_STRENGTH)

    def weight(self, media_id: int, artist_key: str | None, kind: Kind, base: float = 1.0) -> float:
        """The draw weight of one track (module docstring), before likes."""
        preference = preference_multiplier(
            self.item_preference.get(media_id, 0.5),
            self.artist_preference.get(artist_key, 0.5) if artist_key else 0.5,
        )
        if kind == "discovery":
            term = (1.0 - self.familiarity(media_id)) ** DISCOVERY_FAMILIARITY_EXPONENT
        else:
            term = (1.0 - FATIGUE_PENALTY * self.fatigue(media_id)) * (
                1.0 - ARTIST_FATIGUE_PENALTY * self.artist_fatigue(artist_key)
            )
            if kind == "mix":
                term *= 1.0 + FAMILIAR_BOOST * self.familiarity(media_id)
        return min(MAX_TRACK_WEIGHT, max(MIN_TRACK_WEIGHT, base * preference * term))


# --- loading ----------------------------------------------------------------

def _watermark(db: Session, user_id: int) -> tuple:
    """What a cached Taste is valid for: the user's play and listen counts
    and latest times (one statement) and the day. The discovery cache it
    lives in (app.discovery.eligibility._cached) adds the library version."""
    plays = select(func.count(PlayHistory.id)).where(PlayHistory.user_id == user_id).scalar_subquery()
    last_play = select(func.max(PlayHistory.played_at)).where(PlayHistory.user_id == user_id).scalar_subquery()
    listens = select(func.count(ListenEvent.id)).where(ListenEvent.user_id == user_id).scalar_subquery()
    last_listen = (
        select(func.max(ListenEvent.started_at)).where(ListenEvent.user_id == user_id).scalar_subquery()
    )
    row = db.execute(select(plays, last_play, listens, last_listen)).one()
    return (user_id, date.today().isoformat(), *row)


def user_taste(db: Session, user: User) -> Taste:
    """The user's Taste: cached until their plays or listens change, the day
    turns or the library changes; otherwise one statement to check."""
    mark = _watermark(db, user.id)
    return _cached(
        ("taste", library_version(), mark),
        lambda: _load_taste(db, user.id, has_plays=bool(mark[2]), has_listens=bool(mark[4])),
    )


def _load_taste(db: Session, user_id: int, *, has_plays: bool, has_listens: bool) -> Taste:
    taste = Taste()
    credits = main_credits(db) if has_plays or has_listens else {}
    if has_plays:
        _load_plays(db, user_id, taste, credits)
    if has_listens:
        taste.item_preference = {
            media_id: stats.preference
            for media_id, stats in listen_stats_for_items(db, user_id).items()
            if stats.total
        }
        by_artist = listen_stats_for_artists(db, user_id)
        if by_artist:
            artist_names = select(Artist.id, Artist.normalized_name)
            if len(by_artist) <= 900:
                artist_names = artist_names.where(Artist.id.in_(list(by_artist)))
            names = {artist_id: name for artist_id, name in db.execute(artist_names)}
            for artist_id, stats in by_artist.items():
                name = names.get(artist_id)
                if name and stats.total:
                    taste.artist_preference[name] = stats.preference
    return taste


def _load_plays(db: Session, user_id: int, taste: Taste, credits: Mapping[str, str]) -> None:
    now = utcnow()
    age = cast(func.julianday(now) - func.julianday(PlayHistory.played_at), Integer).label("age")
    rows = db.execute(
        select(PlayHistory.media_item_id, MediaItem.artist, age, func.count())
        .join(MediaItem, MediaItem.id == PlayHistory.media_item_id)
        .where(PlayHistory.user_id == user_id)
        .group_by(PlayHistory.media_item_id, age)
    ).all()
    identities: dict[str | None, str | None] = {}
    for _media_id, artist, _age, _count in rows:
        if artist not in identities:
            identities[artist] = artist_identity(credits.get(artist) if artist else None, artist)
    collapse = _collapse_duets(identities.values())
    by_track: dict[int, list[tuple[int, int]]] = {}
    by_artist: dict[str, list[tuple[int, int]]] = {}
    for media_id, artist, days, count in rows:
        days = max(int(days or 0), 0)
        by_track.setdefault(media_id, []).append((days, count))
        key = identities[artist]
        if key:
            key = collapse[key]
            by_artist.setdefault(key, []).append((days, count))
            if artist:
                tags = taste.artist_tags.setdefault(key, {})
                tags[artist] = tags.get(artist, 0.0) + count
    for media_id, counts in by_track.items():
        taste.track_strength[media_id] = strength_of(counts, MAX_PLAYS_PER_TRACK)
        recent = [(a, c) for a, c in counts if a < FATIGUE_WINDOW_DAYS]
        if recent:
            taste.track_recent[media_id] = strength_of(recent, MAX_PLAYS_PER_TRACK)
        window = [(a, c) for a, c in counts if a < RECENT_WINDOW_DAYS]
        if window:
            taste.track_window[media_id] = strength_of(window, MAX_PLAYS_PER_TRACK)
    for key, counts in by_artist.items():
        taste.artist_strength[key] = strength_of(counts, MAX_PLAYS_PER_ARTIST)
        recent = [(a, c) for a, c in counts if a < FATIGUE_WINDOW_DAYS]
        if recent:
            taste.artist_recent[key] = strength_of(recent, MAX_PLAYS_PER_ARTIST)


# --- applying ---------------------------------------------------------------


def draw_weights(
    taste: Taste,
    pool: Sequence,
    kind: Kind,
    base: Mapping[int, float] | None = None,
) -> dict[int, float]:
    """{media id: weight} for the pool's tracks whose weight is not 1 (the
    sampler's default), from `base` (a mix's own weights) and the user's
    Taste. With no evidence this is `base` unchanged."""
    base = base or {}
    if not taste.has_evidence:
        return dict(base)
    weights: dict[int, float] = {}
    for item in pool:
        weight = taste.weight(item.id, item.artist_key, kind, base.get(item.id, 1.0))
        if weight != 1.0:
            weights[item.id] = weight
    return weights


def fatigued_ids(taste: Taste, threshold: float = DISCOVERY_FATIGUE_EXCLUDE) -> set[int]:
    """The tracks whose fatigue is at `threshold` or above."""
    return {media_id for media_id in taste.track_recent if taste.fatigue(media_id) >= threshold}


def log_mix_build(
    kind: str,
    name: str,
    pool_size: int,
    chosen: Sequence,
    weights: Mapping[int, float],
    taste: Taste | None = None,
    *,
    familiar: int | None = None,
    new: int | None = None,
) -> None:
    """One INFO line per mix build: what it was drawn from and how."""
    artists = len({getattr(item, "artist_key", None) or getattr(item, "artist", None) for item in chosen})
    if familiar is None and taste is not None:
        familiar = sum(1 for item in chosen if taste.familiarity(item.id) >= FAMILIAR_MIN)
    if new is None and familiar is not None:
        new = len(chosen) - familiar
    values = [weights[item.id] for item in chosen if item.id in weights]
    low = min(values, default=1.0)
    high = max(values, default=1.0)
    logger.info(
        "Mix built: %s %r from %d candidates: %d tracks by %d artists, %s familiar / %s new, weights %.2f-%.2f",
        kind, name, pool_size, len(chosen), artists,
        "?" if familiar is None else familiar, "?" if new is None else new,
        min(low, 1.0) if len(values) < len(chosen) else low,
        max(high, 1.0) if len(values) < len(chosen) else high,
    )


# A track at or above this familiarity counts as "familiar" in a cluster mix
# and in the build log (about three plays in the last two months, or one in
# the last few days); below it, it is "new to you".
FAMILIAR_MIN = 0.15
