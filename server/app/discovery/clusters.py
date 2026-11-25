"""Daily mixes built from clusters of artists the user plays together -
roughly three-quarters familiar and the rest new to them. No live look-up: only
the user's own play history, the library's labels and (for users who allow
it) ListenBrainz similar artists already cached.

1. Sessions. The user's latest CLUSTER_HISTORY_PLAYS plays are split into
   listening sessions wherever SESSION_GAP_MINUTES pass without a play
   (`started_at` when the client reported it, else `played_at`); a session
   reaching SESSION_MAX_ARTISTS artists is cut there, so a shuffle of the
   whole library does not tie everything to everything.
2. Artists. Each play counts for its main credited artist
   (app.discovery.eligibility.main_credits), decayed by age with a
   CLUSTER_HALF_LIFE_DAYS half-life; the CLUSTER_TOP_ARTISTS heaviest
   artists are the graph's nodes.
3. Edges. Co-listening is the cosine of two artists' session-incidence
   vectors (an artist's entry for a session is the session's decay over the
   square root of the number of other artists in it: one marathon session
   counts once, and a crowded one less than a focused one). Label likeness
   is the cosine of their IDF-weighted label profiles (app.discovery.similar)
   so a thinly co-listened pair of alike artists still clusters. The edge is
   SESSION_WEIGHT x co-listening + the rest x label likeness, dropped under
   MIN_EDGE.
4. Clusters. Louvain modularity clustering (local moving then aggregation,
   resolution 1, node order from the refresh's random generator - plain
   Python, about 200 nodes). Clusters of fewer than MIN_CLUSTER_ARTISTS
   artists or under MIN_CLUSTER_SHARE of the listening are dropped, and two
   whose label centroids are CLUSTER_MERGE_SIMILARITY alike are merged.
   At most MAX_CLUSTER_MIXES, heaviest first; none without
   MIN_HISTORY_PLAYS plays.
5. Tracks (CLUSTER_FAMILIAR_SHARE familiar). "Familiar" are the cluster's
   artists' tracks at app.discovery.familiarity.FAMILIAR_MIN or more
   familiarity that are not fatigued; "new to you" are their tracks below it,
   then - when the local audio analysis has vectors - tracks that sound like
   the familiar picks (app.discovery.audio, at most a third of the mix), then
   tracks by library artists alike the cluster by labels; both of the last
   only by artists the user has barely played (artist familiarity under
   NEW_ARTIST_MAX_FAMILIARITY) and outside the cluster.
   Both are drawn with the shared sampler (artist caps, one version per
   recording, likes, listen outcomes, fatigue); a short "new" share is
   logged and filled with familiar tracks.
6. Name. The two most played artists ("A, B & more"), or "<Label> mix: A, B"
   when one specific label (idf at least DOMINANT_LABEL_MIN_IDF, not
   blocked) is carried by DOMINANT_LABEL_COVERAGE of the cluster's artists.

A cluster mix is an ordinary automatic mix (auto_kind "mix"); its
`mix_genre` is "cluster:" plus JSON {"name", "artists"} (normalised artist
identities), so "Add more" rebuilds from the same artists."""

from __future__ import annotations

import json
import logging
import math
import random
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.access import accessible_root_ids
from app.discovery import audio
from app.discovery.blocklist import blocked_for_mixes
from app.discovery.common import MIX_MAX_PER_ARTIST, _utcnow
from app.discovery.eligibility import (
    _FEAT_RE,
    _cached,
    _collapse_duets,
    artist_identity,
    main_credits,
    mix_eligible,
)
from app.discovery.familiarity import (
    DISCOVERY_FATIGUE_EXCLUDE,
    FAMILIAR_MIN,
    Taste,
    draw_weights,
    log_mix_build,
    user_taste,
)
from app.discovery.labels import CLUSTER_PREFIX, parse_cluster_selector
from app.discovery.sampling import (
    _balanced_track_sample,
    _finish_mix,
    _group_key,
    _Pick,
    _picks,
    _picks_for_ids,
    _rng,
    recording_keys_of,
)
from app.discovery.similar import _artists_similar_by_genre, _label_index, _LabelIndex
from app.discovery.sounds_like import sounds_like
from app.folder_exclusions import not_in_ignored_folder
from app.library_cache import library_version
from app.listenbrainz_data import online_allowed
from app.models import MediaItem, MediaKind, PlayHistory, User
from app.preferences import liked_levels, not_disliked

logger = logging.getLogger("app.discovery")

SESSION_GAP_MINUTES = 30
SESSION_MAX_ARTISTS = 40
CLUSTER_HISTORY_PLAYS = 20_000
CLUSTER_HALF_LIFE_DAYS = 90.0
CLUSTER_TOP_ARTISTS = 200
SESSION_WEIGHT = 0.6
MIN_EDGE = 0.05
MIN_CLUSTER_ARTISTS = 3
MIN_CLUSTER_SHARE = 0.03
CLUSTER_MERGE_SIMILARITY = 0.85
MAX_CLUSTER_MIXES = 5
MIN_HISTORY_PLAYS = 30
CLUSTER_FAMILIAR_SHARE = 0.75
NEW_ARTIST_MAX_FAMILIARITY = 0.2
DOMINANT_LABEL_COVERAGE = 0.6
DOMINANT_LABEL_MIN_IDF = math.log(11.0)  # carried by at most ~10% of artists
# A cluster mix's selector keeps at most this many of its artists.
SELECTOR_MAX_ARTISTS = 60


@dataclass
class Cluster:
    """A group of artists the user plays together: normalised identities,
    heaviest first, with their decayed play mass."""

    artists: list[str]
    mass: dict[str, float]
    display: dict[str, str] = field(default_factory=dict)

    @property
    def total(self) -> float:
        return sum(self.mass.get(artist, 0.0) for artist in self.artists)


# --- history -----------------------------------------------------------------


@dataclass
class _History:
    mass: dict[str, float]
    sessions: list[tuple[float, set[str]]]
    display: dict[str, str]
    plays: int


def _history(db: Session, user: User) -> _History:
    """The user's latest plays as sessions of artist identities (one
    statement)."""
    when = func.coalesce(PlayHistory.started_at, PlayHistory.played_at)
    rows = db.execute(
        select(when, MediaItem.artist)
        .join(MediaItem, MediaItem.id == PlayHistory.media_item_id)
        .where(PlayHistory.user_id == user.id, MediaItem.kind == MediaKind.track)
        .order_by(PlayHistory.played_at.desc())
        .limit(CLUSTER_HISTORY_PLAYS)
    ).all()
    if not rows:
        return _History({}, [], {}, 0)
    credits = main_credits(db)
    identities: dict[str, str | None] = {}
    for _when, artist in rows:
        if artist is not None and artist not in identities:
            identities[artist] = artist_identity(credits.get(artist), artist)
    collapse = _collapse_duets(identities.values())
    now = _utcnow()
    mass: dict[str, float] = {}
    tag_plays: dict[str, dict[str, int]] = {}
    sessions: list[tuple[float, set[str]]] = []
    current: set[str] = set()
    current_weight = 0.0
    last = None
    for played, artist in sorted(((w, a) for w, a in rows if w is not None), key=lambda row: row[0]):
        if artist is None or (identity := identities.get(artist)) is None:
            continue
        identity = collapse[identity]
        weight = 0.5 ** (max((now - played).total_seconds(), 0.0) / 86400.0 / CLUSTER_HALF_LIFE_DAYS)
        mass[identity] = mass.get(identity, 0.0) + weight
        plays = tag_plays.setdefault(identity, {})
        plays[artist] = plays.get(artist, 0) + 1
        gap = last is not None and played - last > timedelta(minutes=SESSION_GAP_MINUTES)
        if current and (gap or (identity not in current and len(current) >= SESSION_MAX_ARTISTS)):
            sessions.append((current_weight, current))
            current = set()
        current.add(identity)
        current_weight = weight
        last = played
    if current:
        sessions.append((current_weight, current))
    display = {
        identity: _FEAT_RE.split(max(tags, key=lambda tag: (tags[tag], -len(tag))), maxsplit=1)[0].strip()
        for identity, tags in tag_plays.items()
    }
    return _History(mass, sessions, display, len(rows))


# --- the graph ---------------------------------------------------------------


def _identity_profiles(index: _LabelIndex, credits: Mapping[str, str]) -> dict[str, dict[str, float]]:
    """Each artist identity's label profile: the unit profiles of its artist
    tags summed and normalised (so "A" and "A feat. B" pool)."""
    identities = {tag: artist_identity(credits.get(tag), tag) for tag in index.profiles}
    collapse = _collapse_duets(identities.values())
    summed: dict[str, dict[str, float]] = {}
    for tag, profile in index.profiles.items():
        identity = identities[tag]
        if identity is None:
            continue
        target = summed.setdefault(collapse[identity], {})
        norm = index.norms[tag]
        for label, weight in profile.items():
            target[label] = target.get(label, 0.0) + weight / norm
    result: dict[str, dict[str, float]] = {}
    for identity, profile in summed.items():
        norm = math.sqrt(sum(w * w for w in profile.values()))
        if norm > 0:
            result[identity] = {label: w / norm for label, w in profile.items()}
    return result


def _session_cosines(nodes: Sequence[str], sessions: Iterable[tuple[float, set[str]]]) -> dict[tuple[str, str], float]:
    wanted = set(nodes)
    norms: dict[str, float] = {}
    dots: dict[tuple[str, str], float] = {}
    for weight, members in sessions:
        present = sorted(members & wanted)
        if not present:
            continue
        value = weight * weight / max(len(present) - 1, 1)
        for artist in present:
            norms[artist] = norms.get(artist, 0.0) + value
        for i, a in enumerate(present):
            for b in present[i + 1:]:
                dots[(a, b)] = dots.get((a, b), 0.0) + value
    return {
        pair: dot / math.sqrt(norms[pair[0]] * norms[pair[1]])
        for pair, dot in dots.items()
        if norms[pair[0]] > 0 and norms[pair[1]] > 0
    }


def _label_cosines(nodes: Sequence[str], profiles: Mapping[str, Mapping[str, float]]) -> dict[tuple[str, str], float]:
    order = {node: position for position, node in enumerate(sorted(nodes))}
    by_label: dict[str, list[tuple[str, float]]] = {}
    for node in nodes:
        for label, weight in profiles.get(node, {}).items():
            by_label.setdefault(label, []).append((node, weight))
    dots: dict[tuple[str, str], float] = {}
    for carriers in by_label.values():
        carriers.sort(key=lambda entry: order[entry[0]])
        for i, (a, wa) in enumerate(carriers):
            for b, wb in carriers[i + 1:]:
                dots[(a, b)] = dots.get((a, b), 0.0) + wa * wb
    return dots  # profiles are unit vectors: the dot is the cosine


def _graph(
    nodes: Sequence[str],
    sessions: Iterable[tuple[float, set[str]]],
    profiles: Mapping[str, Mapping[str, float]],
) -> dict[str, dict[str, float]]:
    co = _session_cosines(nodes, sessions)
    labels = _label_cosines(nodes, profiles)
    graph: dict[str, dict[str, float]] = {node: {} for node in nodes}
    for pair in co.keys() | labels.keys():
        a, b = sorted(pair)
        weight = SESSION_WEIGHT * co.get((a, b), 0.0) + (1.0 - SESSION_WEIGHT) * labels.get((a, b), 0.0)
        if weight >= MIN_EDGE:
            graph[a][b] = weight
            graph[b][a] = weight
    return graph


# --- Louvain -----------------------------------------------------------------


def louvain(graph: Mapping[str, Mapping[str, float]], rng: random.Random) -> list[set[str]]:
    """Communities of an undirected weighted graph ({node: {neighbour:
    weight}}, symmetric, no self-loops) by Louvain modularity optimisation
    (Blondel et al. 2008): move single nodes to the neighbouring community
    with the best modularity gain until nothing moves, merge each community
    into one node, and repeat until a level changes nothing. Isolated nodes
    come back as their own communities. Deterministic for a given `rng`."""
    members: list[set[str]] = [{node} for node in graph]
    index = {node: position for position, node in enumerate(graph)}
    adjacency: list[dict[int, float]] = [
        {index[other]: weight for other, weight in graph[node].items() if other != node} for node in graph
    ]
    loops = [0.0] * len(members)
    while True:
        community = _local_moving(adjacency, loops, rng)
        if len(set(community)) == len(community):
            break
        labels = {c: position for position, c in enumerate(dict.fromkeys(community))}
        size = len(labels)
        new_members: list[set[str]] = [set() for _ in range(size)]
        new_loops = [0.0] * size
        new_adjacency: list[dict[int, float]] = [{} for _ in range(size)]
        for node, c in enumerate(community):
            target = labels[c]
            new_members[target] |= members[node]
            new_loops[target] += loops[node]
            for other, weight in adjacency[node].items():
                other_target = labels[community[other]]
                if other_target == target:
                    new_loops[target] += weight
                else:
                    new_adjacency[target][other_target] = new_adjacency[target].get(other_target, 0.0) + weight
        members, loops, adjacency = new_members, new_loops, new_adjacency
    return members


def _local_moving(adjacency: list[dict[int, float]], loops: list[float], rng: random.Random) -> list[int]:
    count = len(adjacency)
    degree = [loops[node] + sum(adjacency[node].values()) for node in range(count)]
    total = sum(degree)
    community = list(range(count))
    if total <= 0:
        return community
    community_degree = list(degree)
    order = list(range(count))
    rng.shuffle(order)
    moved = True
    while moved:
        moved = False
        for node in order:
            current = community[node]
            community_degree[current] -= degree[node]
            links: dict[int, float] = {}
            for other, weight in adjacency[node].items():
                links[community[other]] = links.get(community[other], 0.0) + weight
            scale = degree[node] / total

            def gain(c: int) -> float:
                return links.get(c, 0.0) - community_degree[c] * scale  # noqa: B023 - used within this iteration

            best, best_gain = current, gain(current)
            for c in links:
                candidate = gain(c)
                if candidate > best_gain + 1e-12:
                    best, best_gain = c, candidate
            community[node] = best
            community_degree[best] += degree[node]
            if best != current:
                moved = True
    return community


# --- finding the clusters ---------------------------------------------------


def find_clusters(db: Session, user: User, rng: random.Random | None = None) -> list[Cluster]:
    """The user's artist clusters for cluster mixes, heaviest first (see the
    module docstring); empty with too little history."""
    rng = _rng(rng)
    history = _history(db, user)
    if history.plays < MIN_HISTORY_PLAYS or not history.mass:
        return []
    nodes = sorted(history.mass, key=lambda artist: (-history.mass[artist], artist))[:CLUSTER_TOP_ARTISTS]
    accessible = accessible_root_ids(db, user)
    index = _label_index(db, accessible)
    profiles = _identity_profiles(index, main_credits(db))
    communities = louvain(_graph(nodes, history.sessions, profiles), rng)
    total = sum(history.mass[node] for node in nodes) or 1.0
    clusters = [
        Cluster(
            sorted(group, key=lambda artist: (-history.mass[artist], artist)),
            {artist: history.mass[artist] for artist in group},
            {artist: history.display.get(artist, artist) for artist in group},
        )
        for group in communities
        if len(group) >= MIN_CLUSTER_ARTISTS
    ]
    clusters = [cluster for cluster in clusters if cluster.total / total >= MIN_CLUSTER_SHARE]
    clusters.sort(key=lambda cluster: -cluster.total)
    return _merge_alike(clusters, profiles)


def _centroid(cluster: Cluster, profiles: Mapping[str, Mapping[str, float]]) -> dict[str, float]:
    centroid: dict[str, float] = {}
    for artist in cluster.artists:
        for label, weight in profiles.get(artist, {}).items():
            centroid[label] = centroid.get(label, 0.0) + weight
    return centroid


def _cosine(a: Mapping[str, float], b: Mapping[str, float]) -> float:
    dot = sum(weight * b.get(label, 0.0) for label, weight in a.items())
    norm = math.sqrt(sum(w * w for w in a.values())) * math.sqrt(sum(w * w for w in b.values()))
    return dot / norm if norm > 0 else 0.0


def _merge_alike(clusters: list[Cluster], profiles: Mapping[str, Mapping[str, float]]) -> list[Cluster]:
    """Clusters whose label centroids are CLUSTER_MERGE_SIMILARITY alike
    folded into the heavier one (the list is heaviest first)."""
    kept: list[tuple[Cluster, dict[str, float]]] = []
    for cluster in clusters:
        centroid = _centroid(cluster, profiles)
        twin = next(
            (entry for entry in kept if centroid and _cosine(entry[1], centroid) >= CLUSTER_MERGE_SIMILARITY), None
        )
        if twin is None:
            kept.append((cluster, centroid))
            continue
        target = twin[0]
        target.artists = sorted(
            [*target.artists, *cluster.artists], key=lambda artist: (-({**target.mass, **cluster.mass}[artist]), artist)
        )
        target.mass.update(cluster.mass)
        target.display.update(cluster.display)
        for label, weight in centroid.items():
            twin[1][label] = twin[1].get(label, 0.0) + weight
    return [cluster for cluster, _centroid_ in kept]


# --- naming -----------------------------------------------------------------


def cluster_name(
    cluster: Cluster,
    index: _LabelIndex,
    profiles: Mapping[str, Mapping[str, float]],
    label_names: Mapping[str, str],
) -> str:
    """"A, B & more", or "<Label> mix: A, B" when one specific label
    dominates the cluster (module docstring)."""
    top = [cluster.display.get(artist, artist) for artist in cluster.artists[:2]]
    coverage: dict[str, int] = {}
    for artist in cluster.artists:
        for label in profiles.get(artist, {}):
            coverage[label] = coverage.get(label, 0) + 1
    candidates = [
        label
        for label, count in coverage.items()
        if count >= DOMINANT_LABEL_COVERAGE * len(cluster.artists)
        and index.idf.get(label, 0.0) >= DOMINANT_LABEL_MIN_IDF
        and not blocked_for_mixes(label)
    ]
    if candidates:
        label = max(candidates, key=lambda name: (coverage[name] * index.idf[name], name))
        return f"{label_names.get(label, label)} mix: {', '.join(top)}"
    return f"{', '.join(top)} & more" if len(cluster.artists) > 2 else " & ".join(top)


def cluster_selector(name: str, cluster: Cluster) -> str:
    payload = {"name": name, "artists": cluster.artists[:SELECTOR_MAX_ARTISTS]}
    return CLUSTER_PREFIX + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def is_cluster_selector(selector: str | None) -> bool:
    return bool(selector) and parse_cluster_selector(selector or "") is not None


# --- the tracks -------------------------------------------------------------

_TAG_BATCH = 500


def tags_by_identity(db: Session, accessible: Collection[int]) -> dict[str, list[str]]:
    """{artist identity: every library artist tag that is it} for the tags
    the label index or the credits know, built once per library version."""

    def build() -> dict[str, list[str]]:
        credits = main_credits(db)
        index = _label_index(db, accessible)
        found: dict[str, list[str]] = {}
        for tag in sorted(set(credits) | set(index.profiles)):
            identity = artist_identity(credits.get(tag), tag)
            if identity is not None:
                found.setdefault(identity, []).append(tag)
        return found

    return _cached(("tags_by_identity", library_version(), frozenset(accessible)), build)


def _tags_of(db: Session, accessible: Collection[int], identities: Collection[str], taste: Taste) -> list[str]:
    """Every library artist tag that is one of `identities`: those the label
    index or credits know, and those the user has played."""
    by_identity = tags_by_identity(db, accessible)
    found: set[str] = set()
    for identity in identities:
        found.update(by_identity.get(identity, ()))
        found.update(taste.artist_tags.get(identity, {}))
    return sorted(found)


def _track_pool(db: Session, user: User, accessible: Collection[int], tags: Sequence[str]) -> list[_Pick]:
    pool: list[_Pick] = []
    for start in range(0, len(tags), _TAG_BATCH):
        pool += _picks(
            db,
            select(MediaItem).where(
                MediaItem.library_root_id.in_(accessible),
                MediaItem.kind == MediaKind.track,
                MediaItem.artist.in_(tags[start : start + _TAG_BATCH]),
                not_in_ignored_folder(),
                not_disliked(user.id),
                mix_eligible(),
            ),
        )
    return pool


def cluster_tracks(
    db: Session,
    user: User,
    name: str,
    identities: Sequence[str],
    limit: int,
    *,
    exclude_ids: Collection[int] = (),
    rng: random.Random | None = None,
) -> list[MediaItem]:
    """A cluster mix's tracks (module docstring, step 5), ordered."""
    accessible = accessible_root_ids(db, user)
    taste = user_taste(db, user)
    members = set(identities)
    skip = set(exclude_ids)
    pool = [
        item for item in _track_pool(db, user, accessible, _tags_of(db, accessible, members, taste))
        if item.id not in skip and _group_key(item) in members
    ]
    used = recording_keys_of(_picks_for_ids(db, skip)) if skip else set()
    familiar = [
        item for item in pool
        if taste.familiarity(item.id) >= FAMILIAR_MIN and taste.fatigue(item.id) < DISCOVERY_FATIGUE_EXCLUDE
    ]
    new_own = [item for item in pool if taste.familiarity(item.id) < FAMILIAR_MIN]
    liked = liked_levels(db, user, [item.id for item in pool])
    versions: dict[int, list[_Pick]] = {}
    weights = draw_weights(taste, pool, "mix")
    familiar_target = round(limit * CLUSTER_FAMILIAR_SHARE)
    chosen = _balanced_track_sample(
        familiar, familiar_target, liked, weights, MIX_MAX_PER_ARTIST, rng=rng, used=used, versions=versions
    )
    per_artist: dict[str, int] = {}
    for item in chosen:
        per_artist[_group_key(item)] = per_artist.get(_group_key(item), 0) + 1
    new_target = limit - len(chosen)
    plain = draw_weights(taste, new_own, "plain")
    new = _balanced_track_sample(
        [item for item in new_own if per_artist.get(_group_key(item), 0) < MIX_MAX_PER_ARTIST],
        new_target, liked, plain, 1, rng=rng, used=used, versions=versions,
    )
    if len(new) < new_target and audio.available(db):
        # Sounds like the cluster's familiar picks, by artists the user
        # barely knows (at most AUDIO_MAX_SHARE of the mix).
        new += sounds_like(
            db, user, accessible, [item.id for item in chosen], pool, {}, skip, taste, "plain",
            min(new_target - len(new), audio.max_audio_picks(limit)), used, versions, liked, rng,
            accept=lambda pick: _barely_known(taste, pick, members),
        )
    if len(new) < new_target:
        new += _similar_new(db, user, accessible, taste, pool, members, new_target - len(new), used, versions, liked, rng)
    if len(new) < new_target:
        logger.debug(
            "Cluster mix %r: only %d new-to-you tracks for %d slots; filled with familiar ones",
            name, len(new), new_target,
        )
        chosen += _balanced_track_sample(
            familiar, limit - len(chosen) - len(new), liked, weights, MIX_MAX_PER_ARTIST,
            rng=rng, used=used, versions=versions,
        )
    weights.update(plain)
    log_mix_build("cluster", name, len(pool), chosen + new, weights, familiar=len(chosen), new=len(new))
    return _finish_mix(db, user, chosen + new, versions, liked, rng)


def _barely_known(taste: Taste, item: _Pick, members: Collection[str]) -> bool:
    """New to the user, by an artist outside the cluster they rarely play."""
    return (
        _group_key(item) not in members
        and taste.artist_familiarity(_group_key(item)) < NEW_ARTIST_MAX_FAMILIARITY
        and taste.familiarity(item.id) < FAMILIAR_MIN
    )


def _similar_new(
    db: Session,
    user: User,
    accessible: Collection[int],
    taste: Taste,
    pool: Sequence[_Pick],
    members: Collection[str],
    count: int,
    used: set[tuple],
    versions: dict[int, list[_Pick]],
    liked: dict[int, str],
    rng: random.Random | None,
) -> list[_Pick]:
    """Tracks the user has barely played by library artists alike the
    cluster by labels (app.discovery.similar), that they rarely play."""
    seed_tags = sorted({item.artist for item in pool if item.artist})
    if not seed_tags:
        return []
    similar = _artists_similar_by_genre(
        db, accessible, seed_tags, allow_fetch=False, online=online_allowed(user), skip_disliked_for=user.id, rng=rng
    )
    candidates = [
        item for item in _track_pool(db, user, accessible, sorted(similar)) if _barely_known(taste, item, members)
    ]
    if not candidates:
        return []
    candidate_liked = liked_levels(db, user, [item.id for item in candidates])
    liked.update(candidate_liked)
    return _balanced_track_sample(
        candidates, count, candidate_liked, draw_weights(taste, candidates, "plain"), 1,
        rng=rng, used=used, versions=versions,
    )


def cluster_add_more(
    db: Session,
    user: User,
    selector: str,
    existing_ids: Collection[int],
    count: int,
    rng: random.Random | None = None,
) -> list[MediaItem]:
    """"Add more" for a cluster mix: the same artists, the same split."""
    parsed = parse_cluster_selector(selector)
    if parsed is None:
        return []
    name, identities = parsed
    return cluster_tracks(db, user, name, identities, count, exclude_ids=existing_ids, rng=rng)


def cluster_label_profiles(db: Session, user: User) -> tuple[_LabelIndex, dict[str, dict[str, float]]]:
    """The label index and identity profiles naming uses."""
    index = _label_index(db, accessible_root_ids(db, user))
    return index, _identity_profiles(index, main_credits(db))
