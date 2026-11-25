"""Candidate pools for a mix and the label statistics that choose and fill automatic mixes."""

import random
from collections.abc import Collection, Mapping
from datetime import timedelta
from itertools import combinations

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.access import accessible_root_ids
from app.discovery.blocklist import blocked_for_mixes
from app.discovery.eligibility import mix_eligible
from app.discovery.familiarity import RECENT_WINDOW_DAYS
from app.discovery.labels import Label, _item_labels, _label_filter
from app.discovery.sampling import _Pick, _picks
from app.folder_exclusions import not_in_ignored_folder
from app.library_query import label_track_rows
from app.metadata_taxonomy import normalise_name
from app.models import (
    Genre,
    MediaItem,
    MediaItemGenre,
    MediaItemMusicBrainzTag,
    MediaItemTag,
    MediaKind,
    MusicBrainzTag,
    PlayHistory,
    Tag,
    User,
    utcnow,
)
from app.preferences import not_disliked

# An automatic mix is made of tracks carrying its own label. Only when too few
# do, the shortfall is filled from the labels that most often appear alongside
# it in the library.
MIX_CO_TAG_COUNT = 5
# A label counts as an umbrella over a narrower one when at least this share
# of the narrower label's tracks also carry the broad one ("rock" over
# "shoegaze"); such a broad label is down-weighted as an automatic mix's primary.
MIX_UMBRELLA_OVERLAP = 0.8
# A selector's chance of being drawn is proportional to its track count, so a
# label covering most of the library comes up about as often as the share of
# the library it covers; small ones still turn up now and then.
MIX_SELECTOR_WEIGHT_EXPONENT = 1.0
# A broad "umbrella" label keeps this share of its weight when a narrower
# label could stand in for it (a nudge toward specific mixes, never a ban -
# the biggest labels are the ones the user has most to listen to).
MIX_UMBRELLA_WEIGHT_FACTOR = 0.5


def _mix_pool(
    db: Session,
    user: User,
    includes: list[Label],
    excludes: list[Label],
    skip_ids: Collection[int] = frozenset(),
) -> tuple[list[_Pick], dict[int, float]]:
    """Accessible, non-disliked, mix-eligible (app.discovery.eligibility)
    tracks matching at least one include and no exclude, plus each one's match count (how many includes it carries) so
    callers can fill from the tracks matching the most includes first.

    Tracks are (id, artist) picks; which of the include/exclude labels each
    carries comes from a few id/name queries instead of loading every track
    with its labels. A label counts exactly as
    _label_sets reads it."""
    query = select(MediaItem).where(
        MediaItem.library_root_id.in_(accessible_root_ids(db, user)),
        MediaItem.kind == MediaKind.track,
        not_in_ignored_folder(),
        not_disliked(user.id),
        mix_eligible(),
        or_(*[_label_filter(kind, name) for kind, name in includes]),
    )
    carried = _labels_carried(db, query.with_only_columns(MediaItem.id), [*includes, *excludes])

    def matches(media_id: int, label: Label) -> bool:
        kind = "genre" if label[0] == "genre" else "tag"
        return (kind, normalise_name(label[1])) in carried.get(media_id, ())

    pool: list[_Pick] = []
    weights: dict[int, float] = {}
    for item in _picks(db, query):
        if item.id in skip_ids:
            continue
        if any(matches(item.id, label) for label in excludes):
            continue
        pool.append(item)
        weights[item.id] = float(max(1, sum(matches(item.id, label) for label in includes)))
    return pool, weights


def _labels_carried(db: Session, ids, labels: list[Label]) -> dict[int, set[Label]]:
    """For the tracks `ids` (a subquery of media ids) selects: which of
    `labels` each carries, as (kind, normalised name), with _label_sets'
    rules - a genre from the legacy column or the normalised genres, a tag
    from the library's own tags or MusicBrainz/ListenBrainz ones."""
    genre_keys = {normalise_name(name) for kind, name in labels if kind == "genre"}
    tag_keys = {normalise_name(name) for kind, name in labels if kind != "genre"}
    carried: dict[int, set[Label]] = {}

    def add(rows, kind: str, normalised: bool) -> None:
        for media_id, name in rows:
            key = name if normalised else normalise_name(name)
            if key in (genre_keys if kind == "genre" else tag_keys):
                carried.setdefault(media_id, set()).add((kind, key))

    if genre_keys:
        add(db.execute(
            select(MediaItem.id, MediaItem.genre).where(MediaItem.id.in_(ids), MediaItem.genre.is_not(None))
        ), "genre", False)
        add(db.execute(
            select(MediaItemGenre.media_item_id, Genre.normalized_name)
            .join(Genre, Genre.id == MediaItemGenre.genre_id)
            .where(MediaItemGenre.media_item_id.in_(ids), Genre.normalized_name.in_(genre_keys))
        ), "genre", True)
    if tag_keys:
        # Custom tags have no stored normalised form; the tag table is small.
        tag_ids = [
            tag_id for tag_id, name in db.execute(select(Tag.id, Tag.name))
            if normalise_name(name) in tag_keys
        ]
        if tag_ids:
            add(db.execute(
                select(MediaItemTag.media_item_id, Tag.name)
                .join(Tag, Tag.id == MediaItemTag.tag_id)
                .where(MediaItemTag.media_item_id.in_(ids), MediaItemTag.tag_id.in_(tag_ids))
            ), "tag", False)
        add(db.execute(
            select(MediaItemMusicBrainzTag.media_item_id, MusicBrainzTag.normalized_name)
            .join(MusicBrainzTag, MusicBrainzTag.id == MediaItemMusicBrainzTag.musicbrainz_tag_id)
            .where(
                MediaItemMusicBrainzTag.media_item_id.in_(ids),
                MusicBrainzTag.normalized_name.in_(tag_keys),
            )
        ), "tag", True)
    return carried


def _co_occurring_labels(items: list[MediaItem], primary: str) -> list[str]:
    """The labels that most often appear on the same tracks as `primary`
    (never a non-genre tag, app.discovery.blocklist)."""
    counts: dict[str, int] = {}
    for item in items:
        for label in _item_labels(item) - {normalise_name(primary)}:
            if not blocked_for_mixes(label):
                counts[label] = counts.get(label, 0) + 1
    ranked = sorted(counts, key=lambda label: (-counts[label], label))
    return ranked[:MIX_CO_TAG_COUNT]


def _umbrella_labels(
    qualifying: list[tuple[str, str, int]],
    overlap: Mapping[frozenset[tuple[str, str]], int],
) -> set[tuple[str, str]]:
    """The qualifying labels that are an umbrella over a narrower one: a
    strictly smaller qualifying label (a different name) has most of its
    tracks inside it. Selection down-weights these rather than dropping them.
    `overlap` is how many tracks each pair of labels shares."""
    umbrellas: set[tuple[str, str]] = set()
    for kind, name, count in qualifying:
        key = normalise_name(name)
        if any(
            other_count < count
            and normalise_name(other_name) != key
            and overlap.get(frozenset(((kind, name), (other_kind, other_name))), 0)
            >= MIX_UMBRELLA_OVERLAP * other_count
            for other_kind, other_name, other_count in qualifying
        ):
            umbrellas.add((kind, name))
    return umbrellas


def _label_overlaps(
    db: Session, rows, qualifying: list[tuple[str, str, int]]
) -> dict[frozenset[tuple[str, str]], int]:
    """How many tracks each pair of qualifying labels shares, counted in one
    pass over the (kind, name, media id) rows in track order, rather than
    holding every label's track set and intersecting all pairs (slow on a
    100k-track library)."""
    index = {(kind, name): position for position, (kind, name, _count) in enumerate(qualifying)}
    pairs: dict[tuple[int, int], int] = {}
    current: int | None = None
    labels: set[int] = set()

    def count_pairs() -> None:
        for pair in combinations(sorted(labels), 2):
            pairs[pair] = pairs.get(pair, 0) + 1

    for kind, name, media_id in db.execute(
        select(rows.c.kind, rows.c.name, rows.c.media_item_id).order_by(rows.c.media_item_id)
    ):
        if media_id != current:
            count_pairs()
            current, labels = media_id, set()
        position = index.get((kind, name))
        if position is not None:
            labels.add(position)
    count_pairs()
    names = [(kind, name) for kind, name, _count in qualifying]
    return {frozenset((names[a], names[b])): shared for (a, b), shared in pairs.items()}


def _weighted_mix_selection(
    options: list[tuple[str, str, int]],
    limit: int,
    umbrellas: Collection[tuple[str, str]] = (),
) -> list[tuple[str, str, int]]:
    """Pick selectors without replacement, favoring those covering more tracks.

    The weight is the track count (MIX_SELECTOR_WEIGHT_EXPONENT), halved for
    an umbrella label, so the labels the library is mostly made of are the
    ones that come up most while a small selector still has a non-zero chance
    on every draw where it remains.
    """
    remaining = list(options)
    chosen: list[tuple[str, str, int]] = []
    while remaining and len(chosen) < limit:
        weights = [
            count ** MIX_SELECTOR_WEIGHT_EXPONENT
            * (MIX_UMBRELLA_WEIGHT_FACTOR if (kind, name) in umbrellas else 1.0)
            for kind, name, count in remaining
        ]
        selected = random.choices(remaining, weights=weights, k=1)[0]  # noqa: S311 - ordering/variety only, not security
        chosen.append(selected)
        remaining.remove(selected)
    return chosen


# --- choosing the daily label mixes from the user's listening ---------------

# Of the label mixes, about this share of the slots (3 of 12, 2 of 8) go to
# labels the user has not been listening to, so the Home rail is not a pure
# echo of the last weeks.
EXPLORE_SHARE = 0.25
# A label counts as "not listened to much" (eligible for those slots) below
# this share of the user's recent listening.
EXPLORE_MAX_SHARE = 0.05
# An exploration label's chance is its size (as the size-based selection) times
# 1 + this times its adjacency: the share of its tracks that also carry one
# of the user's TOP_LISTENED_LABELS labels. A label fully inside the user's
# taste neighbourhood is four times as likely as an unrelated one of the
# same size.
EXPLORE_ADJACENCY_BOOST = 3.0
TOP_LISTENED_LABELS = 5
# A label's specificity is its rarity over the library's artists (the label
# index's idf, about 0.7 for a label on every artist, 9 for one on a single
# artist of 10,000); one never seen there counts as this.
DEFAULT_LABEL_IDF = 1.0

LabelOption = tuple[str, str, int]


def label_listening(
    db: Session, accessible: Collection[int], user: User, window: Mapping[int, float]
) -> dict[tuple[str, str], float]:
    """{(kind, name): share} of the user's recent listening that carries each
    label: `window` is each recently played track's ACT-R strength over the
    last RECENT_WINDOW_DAYS (app.discovery.familiarity - recent and often
    played tracks count most), and a label's share is the strength of the
    tracks carrying it over the strength of all. One statement; the label
    names are exactly those the qualifying counts use."""
    total = sum(window.values())
    if total <= 0:
        return {}
    played = select(PlayHistory.media_item_id).where(
        PlayHistory.user_id == user.id,
        PlayHistory.played_at >= utcnow() - timedelta(days=RECENT_WINDOW_DAYS),
    )
    rows = label_track_rows(accessible, MediaItem.id.in_(played)).subquery()
    mass: dict[tuple[str, str], float] = {}
    for kind, name, media_id in db.execute(select(rows.c.kind, rows.c.name, rows.c.media_item_id)):
        strength = window.get(media_id)
        if strength:
            mass[(kind, name)] = mass.get((kind, name), 0.0) + strength
    return {label: value / total for label, value in mass.items()}


def _adjacency(
    option: LabelOption,
    top: Collection[tuple[str, str]],
    overlap: Mapping[frozenset[tuple[str, str]], int],
) -> float:
    key = (option[0], option[1])
    shared = sum(overlap.get(frozenset((key, other)), 0) for other in top if other != key)
    return min(1.0, shared / max(option[2], 1))


def listening_mix_selection(
    options: list[LabelOption],
    limit: int,
    umbrellas: Collection[tuple[str, str]],
    shares: Mapping[tuple[str, str], float],
    idf: Mapping[str, float],
    overlap: Mapping[frozenset[tuple[str, str]], int],
    rng: random.Random,
) -> tuple[list[LabelOption], list[LabelOption]]:
    """(listened, exploration) labels for the daily mixes, `limit` in all.

    Listened: drawn without replacement with a chance proportional to the
    label's share of the user's recent listening (label_listening) times its
    specificity (idf: "rock" on half the library counts little, "shoegaze"
    a lot), halved for an umbrella label. Exploration: about EXPLORE_SHARE
    of the slots (and any the listened labels cannot fill), drawn from the
    labels under EXPLORE_MAX_SHARE of the listening by size, boosted by how
    much they overlap the user's TOP_LISTENED_LABELS
    (EXPLORE_ADJACENCY_BOOST)."""
    def specificity(name: str) -> float:
        return idf.get(normalise_name(name), DEFAULT_LABEL_IDF)

    def umbrella(option: LabelOption) -> float:
        return MIX_UMBRELLA_WEIGHT_FACTOR if (option[0], option[1]) in umbrellas else 1.0

    explore_slots = min(limit, max(1, round(limit * EXPLORE_SHARE))) if limit > 1 else 0
    scored = {
        option: shares.get((option[0], option[1]), 0.0) * specificity(option[1]) * umbrella(option)
        for option in options
    }
    listened = _draw(
        [option for option in options if scored[option] > 0], scored, limit - explore_slots, rng
    )
    top = sorted(
        ((o[0], o[1]) for o in options if shares.get((o[0], o[1]), 0.0) > 0),
        key=lambda key: -shares[key],
    )[:TOP_LISTENED_LABELS]
    rest = [
        option for option in options
        if option not in listened and shares.get((option[0], option[1]), 0.0) < EXPLORE_MAX_SHARE
    ]
    explore_weight = {
        option: option[2] ** MIX_SELECTOR_WEIGHT_EXPONENT
        * umbrella(option)
        * (1.0 + EXPLORE_ADJACENCY_BOOST * _adjacency(option, top, overlap))
        for option in rest
    }
    explored = _draw(rest, explore_weight, limit - len(listened), rng)
    if len(listened) + len(explored) < limit:
        # Too few unlistened labels: the remaining listened ones fill in.
        left = [option for option in options if option not in listened and option not in explored]
        listened += _draw(left, {o: max(scored[o], 1e-9) for o in left}, limit - len(listened) - len(explored), rng)
    return listened, explored


def _draw(
    options: list[LabelOption], weights: Mapping[LabelOption, float], count: int, rng: random.Random
) -> list[LabelOption]:
    """`count` of `options` without replacement, each draw by weight."""
    remaining = list(options)
    chosen: list[LabelOption] = []
    while remaining and len(chosen) < count:
        selected = rng.choices(remaining, weights=[weights[o] for o in remaining], k=1)[0]
        chosen.append(selected)
        remaining.remove(selected)
    return chosen
