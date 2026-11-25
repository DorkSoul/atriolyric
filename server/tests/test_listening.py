"""How listens end (app.listening): the `/listened` report, its bounds and
de-duplication, cleanup and pruning, and the preference signal."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError

from app import cleanup, listening
from app.listening import (
    EARLY_SKIP_SECONDS,
    HALF_LIFE_DAYS,
    LATE_SKIP_WEIGHT,
    NO_LISTENS,
    USER_STARTED_SKIP_FACTOR,
    listen_stats_for_artists,
    listen_stats_for_items,
)
from app.models import (
    Artist,
    LibraryRoot,
    ListenEvent,
    MediaItem,
    MediaKind,
    PlayHistory,
    ScrobbleQueueItem,
    TrackArtist,
    User,
    utcnow,
)


def _item(db_session, title: str = "Song", duration: float | None = 200.0) -> MediaItem:
    root = db_session.query(LibraryRoot).first()
    if root is None:
        root = LibraryRoot(path="/fake", display_name="Fake")
        db_session.add(root)
        db_session.commit()
    item = MediaItem(
        library_root_id=root.id,
        kind=MediaKind.track,
        relative_path=f"{title}.mp3",
        title=title,
        artist="Queen",
        duration_seconds=duration,
    )
    db_session.add(item)
    db_session.commit()
    db_session.refresh(item)
    return item


def _user(db_session) -> User:
    user = db_session.query(User).first()
    assert user is not None
    return user


def _iso(moment) -> str:
    return moment.isoformat() + "Z"


def _body(**overrides) -> dict:
    now = utcnow().replace(microsecond=0)
    body = {
        "started_at": _iso(now - timedelta(minutes=2)),
        "ended_at": _iso(now),
        "listened_seconds": 100.0,
        "duration_seconds": 200.0,
        "position_seconds": 100.0,
        "end_reason": "skipped",
        "start_kind": "auto",
        "source_kind": "album",
    }
    body.update(overrides)
    return body


def _add(db_session, user_id, item_id, reason, listened, *, age_days=0.0, start_kind=None,
         duration=200.0, seconds_offset=0):
    started = utcnow() - timedelta(days=age_days, seconds=seconds_offset)
    db_session.add(
        ListenEvent(
            user_id=user_id,
            media_item_id=item_id,
            started_at=started,
            ended_at=started + timedelta(seconds=listened),
            listened_seconds=listened,
            duration_seconds=duration,
            position_seconds=listened,
            end_reason=reason,
            start_kind=start_kind,
        )
    )
    db_session.commit()


# --- POST /api/stream/{id}/listened ----------------------------------------


def test_a_listen_is_recorded_with_everything_it_says(client, db_session):
    item = _item(db_session)
    body = _body(end_reason="finished", listened_seconds=199.5, position_seconds=200.0, start_kind="user")
    with patch("app.routers.stream.scrobble_track") as scrobble:
        resp = client.post(f"/api/stream/{item.id}/listened", json=body)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": True, "recorded": True}
    row = db_session.query(ListenEvent).one()
    assert row.media_item_id == item.id
    assert row.end_reason == "finished"
    assert row.start_kind == "user"
    assert row.source_kind == "album"
    assert row.listened_seconds == 199.5
    assert row.duration_seconds == 200.0
    assert row.position_seconds == 200.0
    assert _iso(row.started_at) == body["started_at"]
    assert _iso(row.ended_at) == body["ended_at"]
    # Never a play and never a scrobble.
    assert db_session.query(PlayHistory).count() == 0
    scrobble.assert_not_called()


def test_only_the_required_fields_are_needed(client, db_session):
    item = _item(db_session)
    started = _iso(utcnow() - timedelta(seconds=30))
    resp = client.post(
        f"/api/stream/{item.id}/listened",
        json={"started_at": started, "listened_seconds": 12, "end_reason": "unknown"},
    )
    assert resp.status_code == 200, resp.text
    row = db_session.query(ListenEvent).one()
    assert row.start_kind is None and row.source_kind is None and row.duration_seconds is None
    # No end given: when the server received it.
    assert abs((row.ended_at - utcnow()).total_seconds()) < 60


def test_the_same_listen_reported_twice_is_recorded_once(client, db_session):
    item = _item(db_session)
    body = _body()
    first = client.post(f"/api/stream/{item.id}/listened", json=body)
    again = client.post(f"/api/stream/{item.id}/listened", json={**body, "end_reason": "unknown"})
    assert first.json()["recorded"] is True
    assert again.status_code == 200
    assert again.json()["recorded"] is False
    assert db_session.query(ListenEvent).one().end_reason == "skipped"


def test_the_unique_index_refuses_a_second_row_for_the_same_listen(db_session):
    item = _item(db_session)
    user_id = _user(db_session).id
    started = utcnow().replace(microsecond=0)
    for _ in range(2):
        db_session.add(
            ListenEvent(user_id=user_id, media_item_id=item.id, started_at=started, ended_at=started,
                        listened_seconds=3, end_reason="skipped")
        )
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_a_race_past_the_duplicate_check_is_not_an_error(db_session):
    item = _item(db_session)
    user_id = _user(db_session).id
    report = listening.ListenedReport.model_validate(_body())
    assert listening.record_listen(db_session, user_id, item.id, report) is True

    class _NothingFound:
        def first(self):
            return None

    # The check finds nothing (the other retry had not committed yet): the
    # unique index refuses the row and the report is still answered.
    with patch.object(db_session, "execute", return_value=_NothingFound()):
        assert listening.record_listen(db_session, user_id, item.id, report) is False
    assert db_session.query(ListenEvent).count() == 1


@pytest.mark.parametrize(
    "overrides",
    [
        {"started_at": None},
        {"end_reason": "bored"},
        {"end_reason": None},
        {"listened_seconds": -1},
        {"listened_seconds": 86_401},
        {"duration_seconds": -5},
        {"position_seconds": 8 * 86_400},
        {"start_kind": "remote"},
        {"source_kind": "radio"},
    ],
)
def test_a_malformed_report_is_refused(client, db_session, overrides):
    item = _item(db_session)
    resp = client.post(f"/api/stream/{item.id}/listened", json=_body(**overrides))
    assert resp.status_code == 422
    assert db_session.query(ListenEvent).count() == 0


def test_a_report_without_a_body_is_refused(client, db_session):
    item = _item(db_session)
    assert client.post(f"/api/stream/{item.id}/listened").status_code == 422
    assert client.post(f"/api/stream/{item.id}/listened", json={}).status_code == 422


def test_absurd_times_are_rejected(client, db_session):
    item = _item(db_session)
    now = utcnow().replace(microsecond=0)
    for overrides in (
        {"started_at": _iso(now - timedelta(days=15)), "ended_at": _iso(now - timedelta(days=15))},
        {"started_at": _iso(now + timedelta(minutes=10)), "ended_at": None},
        {"started_at": _iso(now - timedelta(minutes=1)), "ended_at": _iso(now - timedelta(minutes=2))},
        {"started_at": _iso(now - timedelta(minutes=1)), "ended_at": _iso(now + timedelta(minutes=10))},
    ):
        resp = client.post(f"/api/stream/{item.id}/listened", json=_body(**overrides))
        assert resp.status_code == 422, overrides
    # A little in the future (a fast client clock) is fine.
    resp = client.post(
        f"/api/stream/{item.id}/listened",
        json=_body(started_at=_iso(now + timedelta(minutes=2)), ended_at=_iso(now + timedelta(minutes=3))),
    )
    assert resp.status_code == 200
    assert db_session.query(ListenEvent).count() == 1


def test_a_report_for_an_unknown_or_inaccessible_item_is_404(client, db_session):
    assert client.post("/api/stream/999999/listened", json=_body()).status_code == 404
    # A private root nobody granted this user.
    private = LibraryRoot(path="/private", display_name="Private", is_shared=False)
    db_session.add(private)
    db_session.commit()
    item = MediaItem(library_root_id=private.id, kind=MediaKind.track, relative_path="p.mp3", title="P")
    db_session.add(item)
    db_session.commit()
    assert client.post(f"/api/stream/{item.id}/listened", json=_body()).status_code == 404
    assert db_session.query(ListenEvent).count() == 0


# --- cleanup and pruning ------------------------------------------------------


def test_listens_go_with_their_media_item_and_their_user(db_session):
    keep = _item(db_session, "Keep")
    gone = _item(db_session, "Gone")
    user_id = _user(db_session).id
    other = User(username="other", password_hash="x")
    db_session.add(other)
    db_session.commit()
    _add(db_session, user_id, keep.id, "finished", 200)
    _add(db_session, user_id, gone.id, "skipped", 10)
    _add(db_session, other.id, keep.id, "skipped", 10)

    cleanup.delete_media_records(db_session, [gone.id])
    db_session.commit()
    assert sorted((r.user_id, r.media_item_id) for r in db_session.query(ListenEvent)) == sorted(
        [(user_id, keep.id), (other.id, keep.id)]
    )
    cleanup.delete_user_records(db_session, other.id)
    db_session.commit()
    assert [(r.user_id, r.media_item_id) for r in db_session.query(ListenEvent)] == [(user_id, keep.id)]


def _listen_ages(db_session) -> list[int]:
    now = utcnow()
    return sorted(round((now - r.started_at) / timedelta(days=1)) for r in db_session.query(ListenEvent))


def test_listens_follow_the_play_history_retention(db_session, monkeypatch):
    from app import main
    from app.config import settings

    item = _item(db_session)
    user_id = _user(db_session).id
    for age in (0, 29, 31, 365):
        _add(db_session, user_id, item.id, "skipped", 10, age_days=age)
    # Off by default: all kept.
    main._run_store_maintenance(db_session)
    assert _listen_ages(db_session) == [0, 29, 31, 365]
    monkeypatch.setattr(settings, "play_history_retention_days", 30)
    main._run_store_maintenance(db_session)
    assert _listen_ages(db_session) == [0, 29]


def test_listens_are_pruned_in_batches(db_session):
    item = _item(db_session)
    user_id = _user(db_session).id
    for n in range(7):
        _add(db_session, user_id, item.id, "skipped", 10, age_days=100, seconds_offset=n)
    _add(db_session, user_id, item.id, "skipped", 10, age_days=1)
    assert cleanup.prune_listen_events(db_session, 10, batch_size=3) == 7
    assert _listen_ages(db_session) == [1]


def test_each_user_keeps_only_the_newest_listens(db_session, caplog):
    import logging

    item = _item(db_session)
    user_id = _user(db_session).id
    other = User(username="other", password_hash="x")
    db_session.add(other)
    db_session.commit()
    for age in range(6):
        _add(db_session, user_id, item.id, "skipped", 10, age_days=age)
    for age in range(2):
        _add(db_session, other.id, item.id, "skipped", 10, age_days=age)
    with caplog.at_level(logging.INFO, logger="app.cleanup"):
        assert cleanup.prune_listen_events(db_session, 0, max_per_user=3) == 3
    assert "Pruned 3 listen events" in caplog.text
    mine = sorted(
        round((utcnow() - r.started_at) / timedelta(days=1))
        for r in db_session.query(ListenEvent).filter_by(user_id=user_id)
    )
    assert mine == [0, 1, 2]
    assert db_session.query(ListenEvent).filter_by(user_id=other.id).count() == 2


def test_the_default_cap_is_fifty_thousand_and_nothing_to_prune_commits_nothing(db_session, monkeypatch):
    from unittest.mock import MagicMock

    assert cleanup.LISTEN_EVENTS_MAX_PER_USER == 50_000
    item = _item(db_session)
    _add(db_session, _user(db_session).id, item.id, "finished", 200)
    commits = MagicMock(wraps=db_session.commit)
    monkeypatch.setattr(db_session, "commit", commits)
    assert cleanup.prune_listen_events(db_session, 30) == 0
    assert commits.call_count == 0


def test_play_history_prune_leaves_the_scrobble_queue(db_session):
    item = _item(db_session)
    user = _user(db_session)
    old = utcnow() - timedelta(days=100)
    db_session.add(ScrobbleQueueItem(user_id=user.id, artist="A", title="T", started_at=old, created_at=old))
    db_session.commit()
    _add(db_session, user.id, item.id, "skipped", 10, age_days=100)
    assert cleanup.prune_play_history(db_session, retention_days=10) == 0
    assert db_session.query(ListenEvent).count() == 0
    assert db_session.query(ScrobbleQueueItem).count() == 1


# --- the signal ---------------------------------------------------------------


def test_nothing_heard_is_neutral(db_session):
    item = _item(db_session)
    assert listen_stats_for_items(db_session, _user(db_session).id, [item.id]) == {}
    assert NO_LISTENS.preference == 0.5
    assert NO_LISTENS.total == 0


def test_outcomes_are_counted_by_kind(db_session):
    item = _item(db_session)
    user_id = _user(db_session).id
    for n, (reason, listened) in enumerate([
        ("finished", 200),
        ("skipped", 185),  # the last 10%: an outro skipped, counted as finished
        ("skipped", 5),  # early
        ("skipped", EARLY_SKIP_SECONDS - 0.1),  # early
        ("skipped", EARLY_SKIP_SECONDS),  # late
        ("skipped", 1.5),  # scanning: ignored
        ("stopped", 50),
        ("transferred", 50),
        ("error", 0),
        ("unknown", 50),
    ]):
        _add(db_session, user_id, item.id, reason, listened, seconds_offset=n)
    stats = listen_stats_for_items(db_session, user_id)[item.id]
    assert (stats.finished, stats.skipped_early, stats.skipped_late, stats.stopped) == (2, 2, 1, 1)
    assert stats.total == 6
    assert stats.positive == pytest.approx(2.0)
    assert stats.negative == pytest.approx(2.0 + LATE_SKIP_WEIGHT)


def test_errors_transfers_and_unknown_ends_say_nothing(db_session):
    item = _item(db_session)
    user_id = _user(db_session).id
    for n, reason in enumerate(["error", "transferred", "unknown"]):
        _add(db_session, user_id, item.id, reason, 5, seconds_offset=n)
    assert listen_stats_for_items(db_session, user_id, [item.id]) == {}


def test_the_preference_is_smooth_and_bounded(db_session):
    liked, skipped, once = _item(db_session, "Liked"), _item(db_session, "Skipped"), _item(db_session, "Once")
    user_id = _user(db_session).id
    for n in range(50):
        _add(db_session, user_id, liked.id, "finished", 200, seconds_offset=n)
        _add(db_session, user_id, skipped.id, "skipped", 3, seconds_offset=n)
    _add(db_session, user_id, once.id, "skipped", 3)
    stats = listen_stats_for_items(db_session, user_id, [liked.id, skipped.id, once.id])
    assert 0.95 < stats[liked.id].preference < 1
    assert 0 < stats[skipped.id].preference < 0.05
    # One skip does not ban a track.
    assert stats[once.id].preference == pytest.approx(2 / 5)


def test_a_skip_of_a_track_the_user_picked_counts_less(db_session):
    auto, user_picked, unknown = _item(db_session, "Auto"), _item(db_session, "User"), _item(db_session, "Unknown")
    user_id = _user(db_session).id
    _add(db_session, user_id, auto.id, "skipped", 5, start_kind="auto")
    _add(db_session, user_id, user_picked.id, "skipped", 5, start_kind="user")
    _add(db_session, user_id, unknown.id, "skipped", 5, start_kind=None)
    stats = listen_stats_for_items(db_session, user_id)
    assert stats[auto.id].negative == pytest.approx(1.0)
    assert stats[user_picked.id].negative == pytest.approx(USER_STARTED_SKIP_FACTOR)
    assert stats[unknown.id].negative == pytest.approx(1.0)
    assert stats[user_picked.id].preference > stats[auto.id].preference
    # A finish weighs the same however it started.
    _add(db_session, user_id, user_picked.id, "finished", 200, start_kind="user", seconds_offset=1)
    assert listen_stats_for_items(db_session, user_id)[user_picked.id].positive == pytest.approx(1.0)


def test_older_listens_weigh_less(db_session):
    recent, old = _item(db_session, "Recent"), _item(db_session, "Old")
    user_id = _user(db_session).id
    _add(db_session, user_id, recent.id, "skipped", 5)
    _add(db_session, user_id, old.id, "skipped", 5, age_days=HALF_LIFE_DAYS + 0.5)
    _add(db_session, user_id, old.id, "finished", 200, age_days=2 * HALF_LIFE_DAYS + 0.5)
    stats = listen_stats_for_items(db_session, user_id)
    assert stats[recent.id].negative == pytest.approx(1.0)
    assert stats[old.id].negative == pytest.approx(0.5)
    assert stats[old.id].positive == pytest.approx(0.25)
    # Counts are not decayed.
    assert stats[old.id].skipped_early == 1 and stats[old.id].finished == 1
    # The same history evaluated later has decayed further.
    later = listen_stats_for_items(db_session, user_id, now=utcnow() + timedelta(days=HALF_LIFE_DAYS))
    assert later[recent.id].negative == pytest.approx(0.5)
    assert listening.decay(-3) == 1.0


def test_only_the_users_own_and_asked_for_listens_count(db_session):
    a, b = _item(db_session, "A"), _item(db_session, "B")
    user_id = _user(db_session).id
    other = User(username="other", password_hash="x")
    db_session.add(other)
    db_session.commit()
    _add(db_session, user_id, a.id, "finished", 200)
    _add(db_session, user_id, b.id, "finished", 200)
    _add(db_session, other.id, a.id, "skipped", 5)
    assert set(listen_stats_for_items(db_session, user_id, [a.id])) == {a.id}
    assert listen_stats_for_items(db_session, user_id, []) == {}
    assert listen_stats_for_items(db_session, other.id)[a.id].skipped_early == 1


def _count_statements(db_session):
    statements: list[str] = []

    def record(_conn, _cursor, statement, *_args):
        statements.append(statement)

    engine = db_session.get_bind()
    event.listen(engine, "before_cursor_execute", record)
    return statements, lambda: event.remove(engine, "before_cursor_execute", record)


def test_one_statement_for_any_number_of_items(db_session):
    items = [_item(db_session, f"T{n}") for n in range(5)]
    user_id = _user(db_session).id
    for n, item in enumerate(items):
        _add(db_session, user_id, item.id, "finished" if n % 2 else "skipped", 200 if n % 2 else 5)
    wanted = [item.id for item in items[:3]]
    # A large set (a whole library) is filtered after the query.
    huge = wanted + list(range(10_000_000, 10_135_000))
    statements, stop = _count_statements(db_session)
    try:
        small = listen_stats_for_items(db_session, user_id, wanted)
        large = listen_stats_for_items(db_session, user_id, huge)
        everything = listen_stats_for_items(db_session, user_id)
    finally:
        stop()
    assert len(statements) == 3
    assert small == large
    assert set(small) == set(wanted)
    assert set(everything) == {item.id for item in items}


def test_per_artist_every_credited_artist_counts_once(db_session):
    queen, bowie, va = Artist(name="Queen", normalized_name="queen"), Artist(name="Bowie", normalized_name="bowie"), Artist(name="VA", normalized_name="va")
    db_session.add_all([queen, bowie, va])
    db_session.commit()
    pressure, solo = _item(db_session, "Under Pressure"), _item(db_session, "Solo")
    db_session.add_all([
        TrackArtist(media_item_id=pressure.id, artist_id=queen.id, role="main"),
        TrackArtist(media_item_id=pressure.id, artist_id=queen.id, role="album_artist"),
        TrackArtist(media_item_id=pressure.id, artist_id=bowie.id, role="featured"),
        TrackArtist(media_item_id=pressure.id, artist_id=va.id, role="album_artist"),
        TrackArtist(media_item_id=solo.id, artist_id=queen.id, role="main"),
    ])
    db_session.commit()
    user_id = _user(db_session).id
    _add(db_session, user_id, pressure.id, "finished", 200)
    _add(db_session, user_id, solo.id, "skipped", 5, start_kind="user")
    statements, stop = _count_statements(db_session)
    try:
        stats = listen_stats_for_artists(db_session, user_id)
        only_bowie = listen_stats_for_artists(db_session, user_id, [bowie.id])
    finally:
        stop()
    assert len(statements) == 2
    assert set(stats) == {queen.id, bowie.id}
    assert (stats[queen.id].finished, stats[queen.id].skipped_early) == (1, 1)
    assert stats[queen.id].negative == pytest.approx(USER_STARTED_SKIP_FACTOR)
    assert (stats[bowie.id].finished, stats[bowie.id].total) == (1, 1)
    assert set(only_bowie) == {bowie.id}
    assert listen_stats_for_artists(db_session, user_id, []) == {}
