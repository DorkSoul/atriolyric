"""A play is reported once it has been listened to: the
`/played` body, its de-duplication and bounds, and the scrobble queue."""

from __future__ import annotations

import json
from datetime import UTC, timedelta
from unittest.mock import patch

import pytest
from sqlalchemy.exc import IntegrityError

from app import scrobbling
from app.models import (
    LibraryRoot,
    MediaItem,
    MediaKind,
    PlayHistory,
    ScrobbleQueueItem,
    User,
    UserIntegration,
    utcnow,
)
from app.secrets import encrypt_credential


def _item(db_session, duration: float | None = 200.0, title: str = "Song") -> MediaItem:
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
        album="Opera",
        duration_seconds=duration,
    )
    db_session.add(item)
    db_session.commit()
    db_session.refresh(item)
    return item


def _iso(moment) -> str:
    return moment.isoformat() + "Z"


def _link_listenbrainz(db_session, user: User | None = None) -> User:
    user = user or db_session.query(User).first()
    assert user is not None
    db_session.add(
        UserIntegration(
            user_id=user.id,
            provider="listenbrainz",
            credential=encrypt_credential(json.dumps({"token": "TOKEN"})),
            external_username="me",
        )
    )
    db_session.commit()
    return user


# --- POST /api/stream/{id}/played ------------------------------------------


def test_a_report_with_a_body_is_recorded_with_its_start(client, db_session):
    item = _item(db_session)
    started = (utcnow() - timedelta(minutes=3)).replace(microsecond=0)
    with patch("app.routers.stream.scrobble_track") as scrobble:
        resp = client.post(
            f"/api/stream/{item.id}/played",
            json={"started_at": _iso(started), "queue_item_id": 7, "listened_seconds": 100.5},
        )
    assert resp.status_code == 200
    assert resp.json()["recorded"] is True
    row = db_session.query(PlayHistory).one()
    assert row.started_at == started
    # The scrobble is sent with when the play began.
    assert scrobble.call_args.args[3] == started


def test_the_same_play_reported_twice_is_recorded_once(client, db_session):
    item = _item(db_session)
    started = _iso(utcnow() - timedelta(minutes=3))
    with patch("app.routers.stream.scrobble_track") as scrobble:
        first = client.post(f"/api/stream/{item.id}/played", json={"started_at": started})
        again = client.post(f"/api/stream/{item.id}/played", json={"started_at": started})
    assert first.json()["recorded"] is True
    assert again.status_code == 200
    assert again.json()["recorded"] is False
    assert db_session.query(PlayHistory).count() == 1
    assert scrobble.call_count == 1


def test_a_second_play_of_the_same_track_is_its_own_row(client, db_session):
    item = _item(db_session)
    with patch("app.routers.stream.scrobble_track"):
        for minutes in (10, 5):
            started = _iso(utcnow() - timedelta(minutes=minutes))
            client.post(f"/api/stream/{item.id}/played", json={"started_at": started})
    assert db_session.query(PlayHistory).count() == 2


def test_the_unique_index_refuses_a_second_row_for_the_same_play(db_session):
    # Two retries racing past the endpoint's check: the index stops the second.
    item = _item(db_session)
    user_id = db_session.query(User).first().id
    started = utcnow().replace(microsecond=0)
    db_session.add(PlayHistory(user_id=user_id, media_item_id=item.id, started_at=started))
    db_session.commit()
    db_session.add(PlayHistory(user_id=user_id, media_item_id=item.id, started_at=started))
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()
    # NULL starts (old clients, old rows) never collide.
    db_session.add_all([PlayHistory(user_id=user_id, media_item_id=item.id) for _ in range(2)])
    db_session.commit()


def test_a_report_without_a_body_or_a_start_is_refused(client, db_session):
    # A body-less report is refused: the body and its started_at are
    # required.
    item = _item(db_session)
    with patch("app.routers.stream.scrobble_track") as scrobble:
        assert client.post(f"/api/stream/{item.id}/played").status_code == 422
        assert client.post(f"/api/stream/{item.id}/played", json={}).status_code == 422
        resp = client.post(
            f"/api/stream/{item.id}/played", json={"started_at": None, "listened_seconds": 5}
        )
        assert resp.status_code == 422
    assert db_session.query(PlayHistory).count() == 0
    scrobble.assert_not_called()


def test_absurd_start_times_are_rejected(client, db_session):
    item = _item(db_session)
    for started in (utcnow() - timedelta(days=15), utcnow() + timedelta(minutes=10)):
        resp = client.post(f"/api/stream/{item.id}/played", json={"started_at": _iso(started)})
        assert resp.status_code == 422
    # Within bounds: a little in the future (a fast client clock) is fine.
    resp = client.post(
        f"/api/stream/{item.id}/played", json={"started_at": _iso(utcnow() + timedelta(minutes=2))}
    )
    assert resp.status_code == 200
    assert db_session.query(PlayHistory).count() == 1


def test_a_report_for_an_unknown_item_is_404(client):
    resp = client.post("/api/stream/999999/played", json={"started_at": _iso(utcnow())})
    assert resp.status_code == 404


# --- scrobbling ---------------------------------------------------------------


def test_the_scrobble_carries_the_start_time(db_session):
    user = _link_listenbrainz(db_session)
    item = _item(db_session)
    started = utcnow().replace(microsecond=0) - timedelta(minutes=4)
    with patch("app.scrobbling.listenbrainz.submit_listen", return_value="accepted") as submit:
        scrobbling.scrobble_track(db_session, user, item, started)
    assert submit.call_args.kwargs["listened_at"] == int(started.replace(tzinfo=UTC).timestamp())
    assert submit.call_args.kwargs["duration"] == 200.0


def test_a_track_of_thirty_seconds_or_less_is_recorded_but_never_scrobbled(client, db_session):
    _link_listenbrainz(db_session)
    item = _item(db_session, duration=30.0)
    with patch("app.scrobbling.listenbrainz.submit_listen", return_value="accepted") as submit:
        resp = client.post(
            f"/api/stream/{item.id}/played", json={"started_at": _iso(utcnow() - timedelta(seconds=20))}
        )
    assert resp.status_code == 200
    assert db_session.query(PlayHistory).count() == 1
    submit.assert_not_called()


def test_a_transient_failure_is_queued_and_retried_later_in_order(db_session):
    user = _link_listenbrainz(db_session)
    first = _item(db_session, title="First")
    second = _item(db_session, title="Second")
    now = utcnow()
    with patch("app.scrobbling.listenbrainz.submit_listen", return_value="transient"):
        scrobbling.scrobble_track(db_session, user, second, now - timedelta(minutes=5))
        scrobbling.scrobble_track(db_session, user, first, now - timedelta(minutes=10))
    queued = db_session.query(ScrobbleQueueItem).all()
    assert sorted(q.title for q in queued) == ["First", "Second"]
    assert all(q.attempts == 1 and q.next_attempt_at > now for q in queued)

    # Not due yet: nothing is sent.
    with patch("app.scrobbling.listenbrainz.submit_listen", return_value="accepted") as submit:
        assert scrobbling.retry_queued_scrobbles(db_session, now=now) == 0
    submit.assert_not_called()

    # Due: sent oldest play first, and the queue empties.
    later = now + timedelta(minutes=3)
    with patch("app.scrobbling.listenbrainz.submit_listen", return_value="accepted") as submit:
        assert scrobbling.retry_queued_scrobbles(db_session, now=later) == 2
    assert [c.kwargs["title"] for c in submit.call_args_list] == ["First", "Second"]
    assert db_session.query(ScrobbleQueueItem).count() == 0


def test_a_retry_pass_stops_at_the_first_transient_failure_and_backs_off(db_session):
    user = _link_listenbrainz(db_session)
    now = utcnow()
    for minutes in (30, 20, 10):
        db_session.add(
            ScrobbleQueueItem(
                user_id=user.id, artist="Queen", title=f"T{minutes}", started_at=now - timedelta(minutes=minutes),
                attempts=1, next_attempt_at=now,
            )
        )
    db_session.commit()
    with patch("app.scrobbling.listenbrainz.submit_listen", return_value="transient") as submit:
        assert scrobbling.retry_queued_scrobbles(db_session, now=now) == 0
    assert submit.call_count == 1
    oldest = db_session.query(ScrobbleQueueItem).filter_by(title="T30").one()
    assert oldest.attempts == 2
    assert oldest.next_attempt_at == now + timedelta(minutes=4)
    assert db_session.query(ScrobbleQueueItem).count() == 3


def test_a_permanent_rejection_is_not_queued(db_session):
    user = _link_listenbrainz(db_session)
    item = _item(db_session)
    for outcome in ("rejected",):
        with patch("app.scrobbling.listenbrainz.submit_listen", return_value=outcome):
            scrobbling.scrobble_track(db_session, user, item, utcnow())
    assert db_session.query(ScrobbleQueueItem).count() == 0


def test_a_queued_entry_rejected_on_retry_is_dropped(db_session):
    user = _link_listenbrainz(db_session)
    now = utcnow()
    db_session.add(ScrobbleQueueItem(user_id=user.id, artist="A", title="T", started_at=now, next_attempt_at=now))
    db_session.commit()
    with patch("app.scrobbling.listenbrainz.submit_listen", return_value="rejected"):
        assert scrobbling.retry_queued_scrobbles(db_session, now=now) == 0
    assert db_session.query(ScrobbleQueueItem).count() == 0


def test_entries_older_than_fourteen_days_expire(db_session):
    user = _link_listenbrainz(db_session)
    now = utcnow()
    db_session.add(
        ScrobbleQueueItem(
            user_id=user.id, artist="A", title="Old", started_at=now - timedelta(days=15), next_attempt_at=now
        )
    )
    db_session.commit()
    with patch("app.scrobbling.listenbrainz.submit_listen", return_value="accepted") as submit:
        scrobbling.retry_queued_scrobbles(db_session, now=now)
    submit.assert_not_called()
    assert db_session.query(ScrobbleQueueItem).count() == 0


def test_a_successful_scrobble_also_sends_a_few_queued_ones(db_session):
    user = _link_listenbrainz(db_session)
    item = _item(db_session)
    now = utcnow()
    for i in range(scrobbling.OPPORTUNISTIC_RETRIES + 2):
        db_session.add(
            ScrobbleQueueItem(
                user_id=user.id, artist="A", title=f"Q{i}", started_at=now - timedelta(hours=1, minutes=-i),
                next_attempt_at=now - timedelta(minutes=1),
            )
        )
    db_session.commit()
    with patch("app.scrobbling.listenbrainz.submit_listen", return_value="accepted") as submit:
        scrobbling.scrobble_track(db_session, user, item, now)
    assert submit.call_count == 1 + scrobbling.OPPORTUNISTIC_RETRIES
    assert db_session.query(ScrobbleQueueItem).count() == 2


def test_entries_of_a_user_who_unlinked_listenbrainz_are_dropped(db_session):
    user = db_session.query(User).first()
    now = utcnow()
    db_session.add(ScrobbleQueueItem(user_id=user.id, artist="A", title="T", started_at=now, next_attempt_at=now))
    db_session.commit()
    with patch("app.scrobbling.listenbrainz.submit_listen") as submit:
        scrobbling.retry_queued_scrobbles(db_session, now=now)
    submit.assert_not_called()
    assert db_session.query(ScrobbleQueueItem).count() == 0


def test_deleting_a_user_deletes_their_queued_scrobbles(db_session):
    from app.cleanup import delete_user_records

    user = User(username="gone", password_hash="x")
    db_session.add(user)
    db_session.commit()
    now = utcnow()
    db_session.add(ScrobbleQueueItem(user_id=user.id, artist="A", title="T", started_at=now, next_attempt_at=now))
    db_session.commit()
    delete_user_records(db_session, user.id)
    db_session.commit()
    assert db_session.query(ScrobbleQueueItem).count() == 0


def test_an_unreadable_credential_keeps_queued_scrobbles(db_session):
    user = _link_listenbrainz(db_session)
    row = db_session.query(UserIntegration).one()
    row.credential = encrypt_credential("not json")
    now = utcnow()
    db_session.add(ScrobbleQueueItem(user_id=user.id, artist="A", title="T", started_at=now, next_attempt_at=now))
    db_session.commit()
    with patch("app.scrobbling.listenbrainz.submit_listen") as submit:
        assert scrobbling.retry_queued_scrobbles(db_session, now=now) == 0
        scrobbling.scrobble_track(db_session, user, _item(db_session), now)
    submit.assert_not_called()
    assert db_session.query(ScrobbleQueueItem).count() == 1


def test_a_failing_opportunistic_retry_never_raises(db_session):
    user = _link_listenbrainz(db_session)
    with patch("app.scrobbling.listenbrainz.submit_listen", return_value="accepted"), patch(
        "app.scrobbling.retry_queued_scrobbles", side_effect=RuntimeError("boom")
    ):
        scrobbling.scrobble_track(db_session, user, _item(db_session), utcnow())


def test_a_start_without_a_timezone_is_read_as_utc(client, db_session):
    item = _item(db_session)
    started = (utcnow() - timedelta(minutes=2)).replace(microsecond=0)
    with patch("app.routers.stream.scrobble_track"):
        resp = client.post(f"/api/stream/{item.id}/played", json={"started_at": started.isoformat()})
    assert resp.status_code == 200
    assert db_session.query(PlayHistory).one().started_at == started
