from __future__ import annotations

import json
from unittest.mock import patch

from app import scrobbling
from app.models import LibraryRoot, MediaItem, MediaKind, User, UserIntegration
from app.secrets import decrypt_credential


def _user(db_session) -> User:
    return db_session.query(User).one()


def _track(db_session, artist="Queen", title="Bohemian Rhapsody") -> MediaItem:
    root = LibraryRoot(path="/fake", display_name="Fake")
    db_session.add(root)
    db_session.commit()
    item = MediaItem(
        library_root_id=root.id, kind=MediaKind.track, relative_path="t.mp3", title=title, artist=artist
    )
    db_session.add(item)
    db_session.commit()
    db_session.refresh(item)
    return item


def _link(db_session, user: User, token: str = "TOKEN") -> None:
    with patch("app.scrobbling.listenbrainz.validate_token", return_value="me"):
        assert scrobbling.link_listenbrainz(db_session, user, token) is None


def test_list_integrations_shows_none_linked_initially(db_session):
    user = _user(db_session)
    result = scrobbling.list_integrations(db_session, user)
    assert {r["provider"] for r in result} == {"listenbrainz"}
    assert all(r["linked"] is False for r in result)


def test_relinking_updates_rather_than_duplicates(db_session):
    user = _user(db_session)
    _link(db_session, user, "T1")
    _link(db_session, user, "T2")
    rows = db_session.query(UserIntegration).all()
    assert len(rows) == 1
    assert json.loads(decrypt_credential(rows[0].credential)) == {"token": "T2"}


def test_unlink_removes_integration(db_session):
    user = _user(db_session)
    _link(db_session, user)
    scrobbling.unlink(db_session, user, "listenbrainz")
    assert db_session.query(UserIntegration).count() == 0


def test_link_stores_the_token_encrypted_with_the_listenbrainz_user_name(db_session):
    user = _user(db_session)
    with patch("app.scrobbling.listenbrainz.validate_token", return_value="lb-user") as check:
        assert scrobbling.link_listenbrainz(db_session, user, "  secret-token \n") is None
    check.assert_called_once_with("secret-token")
    row = db_session.query(UserIntegration).filter_by(provider="listenbrainz").one()
    assert row.external_username == "lb-user"
    assert row.credential.startswith("fernet:")
    assert "secret-token" not in row.credential
    assert json.loads(decrypt_credential(row.credential)) == {"token": "secret-token"}


def test_link_reports_a_rejected_token_and_an_outage(db_session):
    user = _user(db_session)
    with patch("app.scrobbling.listenbrainz.validate_token", return_value=False):
        error = scrobbling.link_listenbrainz(db_session, user, "wrong")
    assert error and "token" in error
    with patch("app.scrobbling.listenbrainz.validate_token", return_value=None):
        error = scrobbling.link_listenbrainz(db_session, user, "tok")
    assert error and "reached" in error
    assert db_session.query(UserIntegration).count() == 0


def test_scrobble_track_skips_untagged_artist_and_short_tracks(db_session):
    user = _user(db_session)
    item = _track(db_session, artist=None)
    _link(db_session, user)
    with patch("app.scrobbling.listenbrainz.submit_listen") as submit:
        scrobbling.scrobble_track(db_session, user, item)
        item.artist = "Queen"
        item.duration_seconds = 30
        db_session.commit()
        scrobbling.scrobble_track(db_session, user, item)
    submit.assert_not_called()


def test_scrobble_track_no_linked_providers_is_a_noop(db_session):
    user = _user(db_session)
    item = _track(db_session)
    with patch("app.scrobbling.listenbrainz.submit_listen") as submit:
        scrobbling.scrobble_track(db_session, user, item)
    submit.assert_not_called()


def test_scrobble_track_ignores_old_lastfm_links(db_session):
    user = _user(db_session)
    item = _track(db_session)
    db_session.add(UserIntegration(user_id=user.id, provider="lastfm", credential="legacy"))
    db_session.commit()
    with patch("app.scrobbling.listenbrainz.submit_listen") as submit:
        scrobbling.scrobble_track(db_session, user, item)  # must not raise
    submit.assert_not_called()


def test_scrobble_track_sends_to_listenbrainz(db_session):
    user = _user(db_session)
    item = _track(db_session)
    item.album = "A Night at the Opera"
    item.duration_seconds = 355.0
    item.musicbrainz_recording_mbid = "00000001-0000-4000-8000-000000000001"
    # An ID3v2.3 multi-artist tag: both IDs are sent, each well-formed.
    item.musicbrainz_artist_mbid = "00000004-0000-4000-8000-000000000004/00000005-0000-4000-8000-000000000005"
    db_session.commit()
    _link(db_session, user)
    # Without a start time (an old client's report) the timestamp is now.
    with patch("app.scrobbling.listenbrainz.submit_listen", return_value="accepted") as submit, patch(
        "app.scrobbling.time.time", return_value=1700000000.7
    ):
        scrobbling.scrobble_track(db_session, user, item)
    submit.assert_called_once_with(
        "TOKEN",
        listen_type="single",
        artist="Queen", title="Bohemian Rhapsody", album="A Night at the Opera",
        listened_at=1700000000, duration=355.0, recording_mbid="00000001-0000-4000-8000-000000000001", artist_mbids=["00000004-0000-4000-8000-000000000004", "00000005-0000-4000-8000-000000000005"],
    )


def test_failing_scrobble_never_raises(db_session):
    user = _user(db_session)
    item = _track(db_session)
    _link(db_session, user)
    with patch("app.scrobbling.listenbrainz.submit_listen", side_effect=RuntimeError("boom")):
        scrobbling.scrobble_track(db_session, user, item)  # must not raise
    # A corrupt stored credential is swallowed the same way.
    row = db_session.query(UserIntegration).one()
    row.credential = scrobbling.encrypt_credential("not json")
    db_session.commit()
    scrobbling.scrobble_track(db_session, user, item)
