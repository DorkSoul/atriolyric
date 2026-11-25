"""Mixes from the user's own listening: ACT-R familiarity and fatigue, listen
outcomes as feedback, daily label mixes chosen by listening, cluster mixes,
anchored autoplay and Song Mix, the build log - all without outside services."""

from __future__ import annotations

import logging
import random
from collections import Counter
from contextlib import contextmanager
from datetime import timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import event

from app.discovery import (
    add_more_tracks_to_auto_playlist,
    blocklist,
    clusters,
    familiarity,
    generate_playlist_from_track,
    list_genre_mix_playlists,
    pools,
    refresh_all_genre_mixes,
    refresh_discovery_playlist,
    similar_tracks_for_seed,
)
from app.discovery.familiarity import (
    FAMILIAR_MIN,
    draw_weights,
    familiarity_of,
    fatigue_of,
    preference_multiplier,
    strength_of,
    user_taste,
)
from app.discovery.labels import parse_cluster_selector, selector_display_name
from app.discovery.sampling import _picks
from app.library_cache import bump_library_version
from app.models import (
    LibraryRoot,
    ListenEvent,
    MediaItem,
    MediaKind,
    PlaybackQueueItem,
    PlayHistory,
    Playlist,
    User,
    utcnow,
)
from app.playback import _continuation_for_seeds, _recently_played_seeds


def _seeded(seed: int) -> random.Random:
    return random.Random(seed)  # noqa: S311 - reproducible test draws


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
    root = LibraryRoot(path="/taste", display_name="Taste")
    db_session.add(root)
    db_session.commit()
    return root


_counter = iter(range(10**7))


def _track(db_session, root, title, artist, genre=None, **values) -> MediaItem:
    item = MediaItem(
        library_root_id=root.id,
        kind=MediaKind.track,
        relative_path=f"{next(_counter)}.mp3",
        title=title,
        artist=artist,
        genre=genre,
        **values,
    )
    db_session.add(item)
    return item


def _play(db_session, user, item, *, days_ago=0.0, times=1, at=None):
    for n in range(times):
        when = at or utcnow() - timedelta(days=days_ago, minutes=n)
        db_session.add(PlayHistory(user_id=user.id, media_item_id=item.id, played_at=when, started_at=when))


def _listen(db_session, user, item, reason, *, heard=200.0, days_ago=1.0, n=0):
    start = utcnow() - timedelta(days=days_ago, seconds=n)
    db_session.add(
        ListenEvent(
            user_id=user.id, media_item_id=item.id, started_at=start, ended_at=start + timedelta(seconds=heard),
            listened_seconds=heard, duration_seconds=220.0, end_reason=reason, start_kind="auto",
        )
    )


@contextmanager
def _statements(engine):
    seen: list[str] = []

    def record(_conn, _cursor, statement, *_args):
        seen.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        yield seen
    finally:
        event.remove(engine, "before_cursor_execute", record)


# --- the synthetic listening history (F) -------------------------------------

GENRES = ("Shoegaze", "Hip Hop", "Metal", "Folk")


def _taste_library(db_session, root, user, *, sessions=True, artists_per_genre=6, tracks=6):
    """Four genres of `artists_per_genre` artists with `tracks` songs each
    (and two unplayed artists per genre). With `sessions`, a year of
    listening sessions, each mostly one genre: Shoegaze most, then Hip Hop,
    Metal; Folk never played. Returns {genre: [artist names]}."""
    rng = _seeded(3)
    by_genre: dict[str, list[str]] = {}
    items: dict[str, list[MediaItem]] = {}
    for genre in GENRES:
        names = [f"{genre} {n}" for n in range(artists_per_genre + 2)]
        by_genre[genre] = names
        for name in names:
            items[name] = [_track(db_session, root, f"{name} song {t}", name, genre) for t in range(tracks)]
    db_session.commit()
    bump_library_version()
    if sessions:
        start = utcnow() - timedelta(days=300)
        for day in range(0, 300, 2):
            genre = rng.choices(GENRES[:3], weights=[5, 3, 2])[0]
            when = start + timedelta(days=day, hours=20)
            for _ in range(12):
                name = rng.choice(by_genre[genre][:artists_per_genre])
                # Only the first half of each artist's songs is ever played.
                item = rng.choice(items[name][: tracks // 2])
                when += timedelta(minutes=4)
                _play(db_session, user, item, at=when)
        db_session.commit()
    return by_genre


# --- A. familiarity and fatigue ---------------------------------------------


def test_activation_rises_with_plays_and_recency_and_caps_old_plays():
    one_yesterday = strength_of([(1, 1)], 50)
    one_last_year = strength_of([(365, 1)], 50)
    assert one_yesterday == pytest.approx(1.5 ** -0.5)
    assert one_last_year < 0.06 < one_yesterday
    # A play seconds ago counts as half a day: bounded.
    assert strength_of([(0, 1)], 50) == pytest.approx(0.5 ** -0.5)
    # Only the most recent plays are summed.
    assert strength_of([(1, 10), (300, 1000)], 10) == strength_of([(1, 10)], 10)
    assert familiarity_of(0.0) == 0.0
    assert familiarity_of(one_last_year) < 0.05 < familiarity_of(one_yesterday) < familiarity_of(4.0)
    assert familiarity_of(2.0) == pytest.approx(0.5)
    # Fatigue: one or two plays lately stay low, a binge is high.
    assert fatigue_of(strength_of([(0, 1)], 50)) < 0.2
    assert fatigue_of(strength_of([(0, 3), (1, 3), (2, 3)], 50)) > 0.7


# (days ago, plays that day) -> fatigue. Ages are whole days; "today" is 0.
# The intent the constants are tuned to: a play or two today barely touch a
# track, about five over three days make it half-fatigued (Discovery leaves
# it out from 0.5), ten over three days make it clearly worn out.
@pytest.mark.parametrize(
    ("plays", "low", "high"),
    [
        pytest.param([(0, 1)], 0.0, 0.1, id="one play today"),
        pytest.param([(1, 1)], 0.0, 0.05, id="one play yesterday"),
        pytest.param([(0, 2)], 0.2, 0.25, id="two plays today"),
        pytest.param([(0, 2), (1, 2), (2, 1)], 0.45, 0.55, id="five plays over three days"),
        pytest.param([(0, 4), (1, 3), (2, 3)], 0.8, 1.0, id="ten plays over three days"),
        pytest.param([(6, 5)], 0.0, 0.2, id="five plays six days ago"),
    ],
)
def test_track_fatigue_matches_its_stated_intent(plays, low, high):
    recent = strength_of(plays, familiarity.MAX_PLAYS_PER_TRACK)
    assert low <= fatigue_of(recent) <= high


@pytest.mark.parametrize(
    ("plays", "low", "high"),
    [
        # An afternoon of one artist: clearly fatigued today, never zeroed.
        pytest.param([(0, 15)], 0.7, 0.8, id="fifteen plays today"),
        pytest.param([(3, 15)], 0.25, 0.35, id="the same fifteen three days later"),
        pytest.param([(0, 2)], 0.0, 0.06, id="two plays today"),
    ],
)
def test_artist_fatigue_matches_its_stated_intent(plays, low, high):
    recent = strength_of(plays, familiarity.MAX_PLAYS_PER_ARTIST)
    value = fatigue_of(recent, familiarity.ARTIST_FATIGUE_HALF_STRENGTH)
    assert low <= value <= high
    # The most an artist's fatigue takes off a mix weight.
    assert 1.0 - familiarity.ARTIST_FATIGUE_PENALTY * value >= 0.7


def test_artist_fatigue_is_back_to_nothing_once_the_plays_are_a_week_old(db_session, root, user):
    tracks = [_track(db_session, root, f"Binge {n}", "Binge Band", "Rock") for n in range(15)]
    db_session.commit()
    for item in tracks:
        _play(db_session, user, item, days_ago=familiarity.FATIGUE_WINDOW_DAYS)
    db_session.commit()
    taste = user_taste(db_session, user)
    assert taste.artist_strength  # still familiar
    assert all(taste.artist_fatigue(key) == 0.0 for key in taste.artist_strength)
    assert all(taste.fatigue(item.id) == 0.0 for item in tracks)


def test_taste_is_one_grouped_query_and_cached_until_a_play(db_session, root, user, session_factory):
    engine = session_factory.kw["bind"]
    songs = [_track(db_session, root, f"S{n}", f"A{n % 7}", "Rock") for n in range(60)]
    db_session.commit()

    def history(count):
        for n in range(count):
            _play(db_session, user, songs[n % 60], days_ago=n % 200)
        db_session.commit()

    def cold() -> int:
        bump_library_version()
        with _statements(engine) as seen:
            user_taste(db_session, user)
        return len(seen)

    history(50)
    small = cold()
    history(2000)
    assert cold() == small <= 5
    with _statements(engine) as warm:
        taste = user_taste(db_session, user)
    assert len(warm) == 1  # the watermark only
    assert taste.familiarity(songs[0].id) > taste.familiarity(songs[59].id) >= 0
    _play(db_session, user, songs[59], times=5)
    db_session.commit()
    assert user_taste(db_session, user).fatigue(songs[59].id) > 0.5


def test_discovery_brings_back_forgotten_songs_and_skips_worn_out_ones(db_session, root, user):
    seed = _track(db_session, root, "Seed", "Seed Band", "Dream Pop")
    forgotten = _track(db_session, root, "Forgotten", "Alike", "Dream Pop")
    worn = _track(db_session, root, "Worn", "Alike", "Dream Pop")
    db_session.commit()
    _play(db_session, user, seed, times=3)
    _play(db_session, user, forgotten, days_ago=400, times=4)  # four plays, long ago
    _play(db_session, user, worn, times=6)  # six today: fatigued
    db_session.commit()
    # (Played lately, "Alike" is now a seed artist itself; draw from it directly.)
    from app.discovery.discovery_playlist import _discovery_tracks

    ids = {item.id for item in _discovery_tracks(db_session, user, ["Alike"], {}, 25, rng=_seeded(1))}
    assert forgotten.id in ids and worn.id not in ids
    result = refresh_discovery_playlist(db_session, user, rng=_seeded(1))
    assert worn.id not in {item.media_item_id for item in db_session.get(Playlist, result["playlist_id"]).items}


def test_discovery_prefers_unplayed_tracks_over_familiar_ones(db_session, root, user):
    seed = _track(db_session, root, "Seed", "Seed Band", "Dream Pop")
    unplayed = [_track(db_session, root, f"New {n}", f"Fresh {n}", "Dream Pop") for n in range(20)]
    known = [_track(db_session, root, f"Known {n}", f"Known {n}", "Dream Pop") for n in range(20)]
    db_session.commit()
    _play(db_session, user, seed)
    for n, item in enumerate(known):
        _play(db_session, user, item, days_ago=10 + n, times=1)
        _play(db_session, user, item, days_ago=20 + n, times=1)
    db_session.commit()
    taste = user_taste(db_session, user)
    weights = draw_weights(taste, _picks(db_session, _select_ids([*unplayed, *known])), "discovery")
    assert all(item.id not in weights for item in unplayed)
    assert all(0.05 <= weights[item.id] < 0.9 for item in known)


def _select_ids(items):
    from sqlalchemy import select

    return select(MediaItem).where(MediaItem.id.in_([item.id for item in items]))


def test_mixes_down_weight_fatigued_tracks_but_like_familiar_ones(db_session, root, user):
    fresh = _track(db_session, root, "Fresh", "A", "Rock")
    familiar = _track(db_session, root, "Familiar", "B", "Rock")
    binged = _track(db_session, root, "Binged", "C", "Rock")
    db_session.commit()
    for n in range(8):
        _play(db_session, user, familiar, days_ago=7 + 10 * n)
    _play(db_session, user, binged, times=8)
    db_session.commit()
    taste = user_taste(db_session, user)
    pool = _picks(db_session, _select_ids([fresh, familiar, binged]))
    weights = draw_weights(taste, pool, "mix")
    assert fresh.id not in weights  # 1: untouched
    assert weights[familiar.id] > 1.0
    assert weights[binged.id] < 0.5
    plain = draw_weights(taste, pool, "plain")
    assert familiar.id not in plain and plain[binged.id] < 0.5  # autoplay: fatigue only


# --- B. listen outcomes ------------------------------------------------------


def test_preference_multiplier_is_neutral_at_half_and_bounded():
    assert preference_multiplier(0.5, 0.5) == pytest.approx(1.0)
    assert preference_multiplier(0.0, 0.0) == pytest.approx(0.25)
    assert preference_multiplier(1.0, 1.0) == pytest.approx(1.75)
    assert preference_multiplier(0.5, 0.0) == pytest.approx(0.6)


def test_skips_lower_and_finishes_raise_the_draw_weight(db_session, root, user, session_factory):
    skipped = _track(db_session, root, "Skipped", "A", "Rock")
    loved = _track(db_session, root, "Loved", "B", "Rock")
    sibling = _track(db_session, root, "Sibling", "A", "Rock")
    neutral = _track(db_session, root, "Neutral", "C", "Rock")
    db_session.commit()
    from app.artist_credits import rebuild_all_credits

    rebuild_all_credits(db_session)
    db_session.commit()
    for n in range(8):
        _listen(db_session, user, skipped, "skipped", heard=5.0, n=n)
        _listen(db_session, user, loved, "finished", n=n)
    db_session.commit()
    taste = user_taste(db_session, user)
    pool = _picks(db_session, _select_ids([skipped, loved, sibling, neutral]))
    weights = draw_weights(taste, pool, "mix")
    assert weights[skipped.id] < 0.5
    assert weights[loved.id] > 1.3
    # The skipped artist's other song is lowered too, moderately.
    assert 0.6 <= weights[sibling.id] < 1.0
    assert neutral.id not in weights


def test_no_plays_and_no_listens_change_nothing(db_session, root, user):
    items = [_track(db_session, root, f"T{n}", f"A{n}", "Rock") for n in range(5)]
    db_session.commit()
    taste = user_taste(db_session, user)
    pool = _picks(db_session, _select_ids(items))
    assert not taste.has_evidence
    assert draw_weights(taste, pool, "mix", {items[0].id: 2.0}) == {items[0].id: 2.0}
    assert draw_weights(taste, pool, "discovery") == {}


def test_likes_preference_and_weights_multiply_within_a_cap():
    from app.discovery.common import MAX_TRACK_WEIGHT
    from app.discovery.sampling import _balanced_track_sample

    class Item:
        def __init__(self, item_id, artist):
            self.id, self.artist = item_id, artist

    # One heavy liked artist against nine ordinary ones: its share is capped
    # at MAX_TRACK_WEIGHT picks per round, not LIKE_BOOST x 10.
    items = [Item(n, "Heavy") for n in range(30)] + [Item(100 + n, f"Other {n}") for n in range(9)]
    picks = Counter()
    for run in range(200):
        chosen = _balanced_track_sample(
            items, 10, {n: "artist" for n in range(30)}, dict.fromkeys(range(30), 10.0), rng=_seeded(run)
        )
        picks["Heavy"] += sum(item.artist == "Heavy" for item in chosen)
    assert picks["Heavy"] / 200 <= MAX_TRACK_WEIGHT + 0.5


# --- C. label mixes from listening, the blocklist -------------------------


def test_blocklist_names_patterns_decades_and_setting(monkeypatch):
    for name in ("Seen Live", "favourites", "albums I own", "under 2000 listeners", "1994", "Love", "My Favorite Songs"):
        assert blocklist.blocked_for_mixes(name) and blocklist.blocked_for_similarity(name), name
    for name in ("80s", "1990s", "'70s"):
        assert blocklist.blocked_for_mixes(name) and not blocklist.blocked_for_similarity(name), name
    for name in ("Shoegaze", "female vocalists", "Lovers Rock", "Rock", "Hip Hop", "post-rock"):
        assert not blocklist.blocked_for_mixes(name), name
    monkeypatch.setattr("app.config.settings.mix_blocked_labels", ("Car Music",))
    assert blocklist.blocked_for_mixes("car  music") and blocklist.blocked_for_similarity("CAR MUSIC")


def test_blocked_labels_never_become_mixes_but_specify_mix_offers_them(client, db_session, root, user):
    for n in range(20):
        _track(db_session, root, f"S{n}", f"Seen {n}", "seen live")
        _track(db_session, root, f"E{n}", f"Era {n}", "80s")
        _track(db_session, root, f"R{n}", f"Rock {n}", "Rock")
    db_session.commit()
    for run in range(5):
        names = {mix.mix_genre for mix in refresh_all_genre_mixes(db_session, user, rng=_seeded(run))}
        assert names == {"Rock"}
    options = {option["name"] for option in client.get("/api/library/mixes/options").json()}
    assert {"seen live", "80s", "Rock"} <= options


def test_blocked_labels_are_not_similarity_evidence(db_session, root):
    from app.discovery import similar

    _track(db_session, root, "Seed", "Seed", "seen live")
    _track(db_session, root, "Other", "Other", "seen live")
    _track(db_session, root, "Era", "Era", "80s")
    _track(db_session, root, "Era2", "Era2", "80s")
    db_session.commit()
    index = similar._label_index(db_session, {root.id})
    assert "seen live" not in index.idf and "80s" in index.idf


def test_daily_label_mixes_follow_listening_and_keep_exploration_slots(db_session, root, user):
    _taste_library(db_session, root, user)
    # More labels than slots: twenty small unplayed ones beside the four.
    for n in range(20):
        for t in range(16):
            _track(db_session, root, f"X{n}-{t}", f"Extra {n}-{t % 4}", f"Extra {n}")
    db_session.commit()
    bump_library_version()
    with patch.object(clusters, "MAX_CLUSTER_MIXES", 0):
        counts = Counter()
        explored = Counter()
        for run in range(12):
            names = {m.mix_genre for m in refresh_all_genre_mixes(db_session, user, rng=_seeded(run))}
            counts.update(names & set(GENRES))
            explored[run] = len([name for name in names if name.startswith("Extra")])
    # The genres listened to come up in (nearly) every refresh; Folk, never
    # played, does not get a listened slot.
    assert counts["Shoegaze"] == 12 and counts["Hip Hop"] >= 10
    assert all(count >= 3 for count in explored.values())


def test_listened_labels_beat_bigger_unlistened_ones():
    options = [("genre", "Huge", 5000), ("genre", "Niche", 30), *[("tag", f"t{n}", 40) for n in range(20)]]
    shares = {("genre", "Niche"): 0.5, **{("tag", f"t{n}"): 0.1 for n in range(5)}}
    listened, explored = pools.listening_mix_selection(
        options, 8, set(), shares, {"niche": 4.0, "huge": 0.7}, {}, _seeded(1)
    )
    assert listened[0] == ("genre", "Niche", 30)
    assert len(listened) == 6 and len(explored) == 2
    assert not {name for _k, name, _c in explored} & {"Niche", "t0", "t1", "t2", "t3", "t4"}
    # Only one label listened to: the rest of the slots explore.
    listened, explored = pools.listening_mix_selection(
        options, 8, set(), {("genre", "Niche"): 0.9}, {}, {}, _seeded(1)
    )
    assert len(listened) == 1 and len(explored) == 7


def test_a_user_without_plays_gets_the_size_based_draw(db_session, root, user):
    for n in range(20):
        _track(db_session, root, f"R{n}", f"Rock {n}", "Rock")
    db_session.commit()
    with patch("app.discovery.mixes._weighted_mix_selection", wraps=pools._weighted_mix_selection) as spy:
        refresh_all_genre_mixes(db_session, user)
    spy.assert_called_once()
    assert spy.call_args.args[1] == 13


# --- D. cluster mixes --------------------------------------------------------


def test_louvain_finds_two_cliques_joined_by_one_weak_edge():
    graph: dict[str, dict[str, float]] = {}

    def link(a, b, w):
        graph.setdefault(a, {})[b] = w
        graph.setdefault(b, {})[a] = w

    for group in ("a", "b"):
        members = [f"{group}{n}" for n in range(5)]
        for i, x in enumerate(members):
            for y in members[i + 1:]:
                link(x, y, 1.0)
    link("a0", "b0", 0.1)
    graph["lonely"] = {}
    found = sorted(sorted(c) for c in clusters.louvain(graph, _seeded(1)))
    assert found == [["a0", "a1", "a2", "a3", "a4"], ["b0", "b1", "b2", "b3", "b4"], ["lonely"]]


def test_cluster_mixes_come_first_three_quarters_familiar(db_session, root, user, client):
    by_genre = _taste_library(db_session, root, user, artists_per_genre=6, tracks=8)
    mixes = refresh_all_genre_mixes(db_session, user, rng=_seeded(4))
    cluster_mixes = [m for m in mixes if (m.mix_genre or "").startswith("cluster:")]
    assert 2 <= len(cluster_mixes) <= clusters.MAX_CLUSTER_MIXES
    assert mixes[: len(cluster_mixes)] == cluster_mixes  # first in the rail
    taste = user_taste(db_session, user)
    for mix in cluster_mixes:
        name, members = parse_cluster_selector(mix.mix_genre)
        genres = {next(g for g, names in by_genre.items() if any(n.lower() == m for n in names)) for m in members}
        assert len(genres) == 1, (name, members)  # artists played together, one taste
        tracks = [item.media_item for item in mix.items]
        assert len(tracks) >= 15
        familiar = sum(taste.familiarity(t.id) >= FAMILIAR_MIN for t in tracks)
        assert 0.6 <= familiar / len(tracks) <= 0.85, (name, familiar, len(tracks))
        # The name is the top artists, as the user knows them.
        assert name.endswith("& more") and name.split(",")[0] in by_genre[genres.pop()]
    rail = client.get("/api/library/mixes").json()
    assert rail[0]["genre"] == parse_cluster_selector(cluster_mixes[0].mix_genre)[0]
    assert rail[0]["is_specified"] is False

    # "Add more" grows the same cluster with new tracks.
    mix = cluster_mixes[0]
    before = {item.media_item_id for item in mix.items}
    _name, members = parse_cluster_selector(mix.mix_genre)
    added = add_more_tracks_to_auto_playlist(db_session, user, mix, rng=_seeded(2))
    after = [item.media_item for item in db_session.get(Playlist, mix.id).items]
    new_items = [item for item in after if item.id not in before]
    assert added == len(new_items) > 0
    assert sum(item.artist.lower() in members for item in new_items) >= len(new_items) * 0.6


def test_no_history_means_no_cluster_mixes(db_session, root, user):
    _taste_library(db_session, root, user, sessions=False)
    mixes = refresh_all_genre_mixes(db_session, user, rng=_seeded(1))
    assert mixes and not any((m.mix_genre or "").startswith("cluster:") for m in mixes)


def test_a_cluster_is_named_after_a_dominant_specific_label(db_session):
    index = type("I", (), {})()
    index.idf = {"shoegaze": 4.0, "rock": 0.7, "seen live": 5.0}
    profiles = {
        "a": {"shoegaze": 1.0, "rock": 0.2},
        "b": {"shoegaze": 1.0, "seen live": 0.5},
        "c": {"shoegaze": 0.8, "seen live": 0.5},
    }
    cluster = clusters.Cluster(["a", "b", "c"], {"a": 3, "b": 2, "c": 1}, {"a": "Alvvays", "b": "Beach House", "c": "C"})
    assert clusters.cluster_name(cluster, index, profiles, {"shoegaze": "Shoegaze"}) == "Shoegaze mix: Alvvays, Beach House"
    profiles = {"a": {"rock": 1.0}, "b": {"rock": 1.0}, "c": {"rock": 1.0}}
    assert clusters.cluster_name(cluster, index, profiles, {}) == "Alvvays, Beach House & more"
    selector = clusters.cluster_selector("Alvvays, Beach House & more", cluster)
    assert selector_display_name(selector) == "Alvvays, Beach House & more"
    assert parse_cluster_selector(selector) == ("Alvvays, Beach House & more", ["a", "b", "c"])
    assert parse_cluster_selector("cluster:not json") is None


def test_cluster_mixes_take_a_fixed_number_of_queries(db_session, root, user, session_factory):
    engine = session_factory.kw["bind"]
    _taste_library(db_session, root, user)
    found = clusters.find_clusters(db_session, user, _seeded(1))  # warms the label index
    with _statements(engine) as seen:
        again = clusters.find_clusters(db_session, user, _seeded(1))
    assert len(seen) <= 3 and [c.artists for c in again] == [c.artists for c in found]


def test_every_mix_build_is_logged(db_session, root, user, caplog):
    _taste_library(db_session, root, user)
    with caplog.at_level(logging.INFO, logger="app.discovery"):
        refresh_all_genre_mixes(db_session, user, rng=_seeded(1))
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("Mix built:")]
    assert any(line.startswith("Mix built: cluster") and "familiar" in line for line in lines)
    assert any(line.startswith("Mix built: label") for line in lines)


# --- E. anchored autoplay and Song Mix -----------------------------------------


def _bridged_library(db_session, root):
    """Four styles in a chain - each artist carries its own style and the
    "bridge" to the next - so following only the last track drifts."""
    chain = ["Kraut", "Ambient", "Techno", "House", "Disco", "Funk"]
    seeds = {}
    for style in chain:
        for artist in range(8):
            for n in range(6):
                item = _track(db_session, root, f"{style} {artist}-{n}", f"{style} Band {artist}", style)
                seeds.setdefault(style, item)
    db_session.commit()
    from app.metadata_taxonomy import replace_item_genres

    for item in db_session.query(MediaItem).all():
        style = item.genre
        position = chain.index(style)
        labels = [style]
        artist_n = int(item.artist.rsplit(" ", 1)[1])
        if position + 1 < len(chain) and artist_n % 2 == 0:
            labels.append(f"{style}-{chain[position + 1]}")
        if position > 0 and artist_n % 2 == 1:
            labels.append(f"{chain[position - 1]}-{style}")
        replace_item_genres(db_session, item, labels, "embedded")
    db_session.commit()
    bump_library_version()
    return seeds["Kraut"]


def _chain(db_session, user, seed, *, with_context: bool, rounds: int = 5) -> Counter:
    """Styles of the tracks played over `rounds` chained autoplay batches
    (five tracks of each played), starting from `seed`."""
    db_session.query(PlaybackQueueItem).delete()
    db_session.query(PlayHistory).delete()
    played = [seed.id]
    _play(db_session, user, seed)
    db_session.add(PlaybackQueueItem(user_id=user.id, position=0, original_position=0, media_item_id=seed.id))
    db_session.commit()
    styles: Counter = Counter()
    labels = {g.name for g in seed.genres}
    for _round in range(rounds):
        seeds = _recently_played_seeds(db_session, user, played[-1]) if with_context else [played[-1]]
        for media_id in _continuation_for_seeds(db_session, user, seeds)[:5]:
            position = len(played)
            db_session.add(PlaybackQueueItem(user_id=user.id, position=position, original_position=position, media_item_id=media_id))
            item = db_session.get(MediaItem, media_id)
            _play(db_session, user, item)
            played.append(media_id)
            styles["near" if {g.name for g in item.genres} & labels else "far"] += 1
            styles[f"far in round {_round}"] += not {g.name for g in item.genres} & labels
        db_session.commit()
    return styles


def test_chained_autoplay_stays_near_its_start(db_session, root, user):
    """Five chained batches from a Kraut song in a library where every style
    bridges into the next: most of what plays still shares a label with the
    start (observed ~85% anchored, ~76% following the last track only), and
    the last round has not run off."""
    seed = _bridged_library(db_session, root)
    totals = {True: Counter(), False: Counter()}
    for run in range(6):
        for mode in (True, False):
            random.seed(run)
            totals[mode] += _chain(db_session, user, seed, with_context=mode)
    anchored = totals[True]
    assert anchored["near"] / (anchored["near"] + anchored["far"]) >= 0.7, totals
    assert anchored["far in round 4"] <= 0.5 * 6 * 5, totals


def test_autoplay_is_seeded_by_tracks_actually_played(db_session, root, user):
    tracks = [_track(db_session, root, f"Q{n}", f"Q{n}", "Rock") for n in range(8)]
    db_session.commit()
    for n, item in enumerate(tracks):
        db_session.add(PlaybackQueueItem(user_id=user.id, position=n, original_position=n, media_item_id=item.id))
    # Played: 1, 2, 4, 5, 6; 3 was skipped; 7 is the one ending now.
    for n in (1, 2, 4, 5, 6):
        _play(db_session, user, tracks[n])
    db_session.commit()
    assert _recently_played_seeds(db_session, user, tracks[7].id) == [t.id for t in (tracks[2], tracks[4], tracks[5], tracks[6], tracks[7])]
    lonely = _track(db_session, root, "Lonely", "Lonely", "Rock")
    db_session.commit()
    assert _recently_played_seeds(db_session, user, lonely.id)[-1] == lonely.id


def test_song_mix_brings_the_seed_artist_back_about_every_five_tracks(db_session, root, user):
    seed = _track(db_session, root, "Seed", "Seed Band", "Dream Pop")
    for n in range(10):
        _track(db_session, root, f"Own {n}", "Seed Band", "Dream Pop")
    for artist in range(20):
        for n in range(3):
            _track(db_session, root, f"Other {artist}-{n}", f"Other {artist}", "Dream Pop")
    db_session.commit()
    result = generate_playlist_from_track(db_session, user, seed.id, rng=_seeded(2))
    items = [item.media_item for item in db_session.get(Playlist, result["playlist_id"]).items]
    assert items[0].id == seed.id
    assert [n for n, item in enumerate(items) if item.artist == "Seed Band"] == [0, 5, 10, 15, 20]
    assert Counter(item.artist for item in items if item.artist != "Seed Band").most_common(1)[0][1] <= 2


def test_autoplay_has_no_seed_artist_interleave(db_session, root, user):
    seed = _track(db_session, root, "Seed", "Seed Band", "Dream Pop")
    for n in range(5):
        _track(db_session, root, f"Own {n}", "Seed Band", "Dream Pop")
    for artist in range(10):
        _track(db_session, root, f"Other {artist}", f"Other {artist}", "Dream Pop")
    db_session.commit()
    ids = similar_tracks_for_seed(db_session, user, seed.id, rng=_seeded(1), kind="plain")
    assert all(db_session.get(MediaItem, i).artist != "Seed Band" for i in ids[1:])


def test_list_genre_mix_playlists_orders_clusters_labels_then_specified(db_session, user):
    for selector in ("specified:genre:Zed", "Rock", 'cluster:{"name":"A, B & more","artists":["a"]}', "Ambient"):
        db_session.add(Playlist(user_id=user.id, name=selector, is_auto_generated=True, auto_kind="mix", mix_genre=selector))
    db_session.commit()
    order = [p.mix_genre for p in list_genre_mix_playlists(db_session, user)]
    assert order[0].startswith("cluster:") and order[1:3] == ["Ambient", "Rock"] and order[3].startswith("specified:")


def test_familiarity_constants_are_documented():
    doc = familiarity.__doc__ or ""
    for name in ("ACTR_DECAY", "FAMILIARITY_HALF_STRENGTH", "FATIGUE_WINDOW_DAYS", "FATIGUE_HALF_STRENGTH"):
        assert name in doc
