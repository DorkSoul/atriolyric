from __future__ import annotations

import pytest

from app import playback
from app.models import LibraryRoot, MediaItem, MediaKind, User
from app.security import hash_password


@pytest.fixture()
def user(db_session) -> User:
    return db_session.query(User).one()


@pytest.fixture()
def tracks(db_session) -> list[MediaItem]:
    root = LibraryRoot(path="/fake", display_name="Fake")
    db_session.add(root)
    db_session.commit()
    items = []
    for i in range(3):
        item = MediaItem(
            library_root_id=root.id,
            kind=MediaKind.track,
            relative_path=f"t{i}.mp3",
            title=f"Track {i}",
        )
        db_session.add(item)
        items.append(item)
    db_session.commit()
    for item in items:
        db_session.refresh(item)
    return items


def _current_row(db_session, user) -> int:
    current = playback.current_queue_item_id(db_session, user)
    assert current is not None
    return current


def _end_current(db_session, user) -> str | None:
    """The current queue row's natural end, as the active device reports it."""
    return playback.track_ended(db_session, user, _current_row(db_session, user))


def _seek_current(db_session, user, seconds: float) -> str | None:
    return playback.seek(db_session, user, seconds, _current_row(db_session, user))


def _set_queue(db_session, user, items: list[MediaItem], start_index: int = 0) -> str | None:
    """Test-only shim offering a set_queue(ids, start_index) shape over the
    real API (set_queue_context), which takes a context dict and starts at a
    specific media item rather than a raw list index."""
    return playback.set_queue_context(
        db_session, user, {"kind": "ids", "ids": [t.id for t in items]}, items[start_index].id
    )


def test_register_device_becomes_active_when_none_active(db_session, user):
    device = playback.register_device(db_session, user, "Laptop", "web")
    state = playback.get_or_create_state(db_session, user)
    assert state.active_device_id == device.id


def test_duplicate_device_names_get_disambiguated(db_session, user):
    d1 = playback.register_device(db_session, user, "Chrome (Desktop)", "web")
    d2 = playback.register_device(db_session, user, "Chrome (Desktop)", "web")
    d3 = playback.register_device(db_session, user, "Chrome (Desktop)", "web")
    assert d1.name == "Chrome (Desktop)"
    assert d2.name == "Chrome (Desktop) #2"
    assert d3.name == "Chrome (Desktop) #3"


def test_second_device_does_not_steal_active(db_session, user):
    first = playback.register_device(db_session, user, "Laptop", "web")
    playback.register_device(db_session, user, "Phone", "mobile")
    state = playback.get_or_create_state(db_session, user)
    assert state.active_device_id == first.id


def test_paused_new_queue_context_claims_requesting_device(db_session, user, tracks):
    playback.register_device(db_session, user, "Laptop", "web")
    second = playback.register_device(db_session, user, "Phone", "mobile")
    state = playback.get_or_create_state(db_session, user)
    state.is_playing = False
    db_session.commit()

    assert playback.set_queue_context(
        db_session, user, {"kind": "ids", "ids": [t.id for t in tracks]}, tracks[0].id,
        requester_device_id=second.id,
    ) is None
    db_session.refresh(state)
    assert state.active_device_id == second.id
    assert state.is_playing is True


def test_paused_queue_context_does_not_claim_disconnected_requester(db_session, user, tracks):
    first = playback.register_device(db_session, user, "Laptop", "web")
    second = playback.register_device(db_session, user, "Phone", "mobile")
    state = playback.get_or_create_state(db_session, user)
    state.is_playing = False
    playback.disconnect_device(db_session, user, second.id)

    assert playback.set_queue_context(
        db_session, user, {"kind": "ids", "ids": [t.id for t in tracks]}, tracks[0].id,
        requester_device_id=second.id,
    ) is None
    db_session.refresh(state)
    assert state.active_device_id == first.id


def test_playing_queue_context_preserves_active_device(db_session, user, tracks):
    first = playback.register_device(db_session, user, "Laptop", "web")
    second = playback.register_device(db_session, user, "Phone", "mobile")
    state = playback.get_or_create_state(db_session, user)
    state.is_playing = True
    db_session.commit()

    assert playback.set_queue_context(
        db_session, user, {"kind": "ids", "ids": [t.id for t in tracks]}, tracks[1].id,
        requester_device_id=second.id,
    ) is None
    db_session.refresh(state)
    assert state.active_device_id == first.id


def test_new_queue_claims_output_when_the_playing_active_device_is_offline(db_session, user, tracks):
    # A plays; A's browser closes without pausing (offline, state still
    # "playing" on A); C starts an album: C must play it, not be a remote.
    a = playback.register_device(db_session, user, "Laptop", "web", client_token="a")
    c = playback.register_device(db_session, user, "Phone", "mobile", client_token="c")
    state = playback.get_or_create_state(db_session, user)
    state.is_playing = True
    db_session.commit()
    playback.disconnect_device(db_session, user, a.id)

    assert playback.set_queue_context(
        db_session, user, {"kind": "ids", "ids": [t.id for t in tracks]}, tracks[0].id,
        requester_device_id=c.id,
    ) is None
    db_session.refresh(state)
    assert state.active_device_id == c.id
    assert state.is_playing is True
    snapshot = playback.build_state_snapshot(db_session, user)
    assert snapshot["active_device_id"] == c.id

    # A comes back (a reconnect, even asking to resume): C stays active.
    playback.register_device(db_session, user, "Laptop", "web", resume_playback=True, client_token="a")
    db_session.refresh(state)
    assert state.active_device_id == c.id
    assert playback.build_state_snapshot(db_session, user)["active_device_id"] == c.id


def test_new_queue_claims_output_when_there_is_no_active_device(db_session, user, tracks):
    playback.register_device(db_session, user, "Laptop", "web")
    c = playback.register_device(db_session, user, "Phone", "mobile")
    state = playback.get_or_create_state(db_session, user)
    state.is_playing = True
    state.active_device_id = None
    db_session.commit()

    playback.set_queue_context(
        db_session, user, {"kind": "ids", "ids": [t.id for t in tracks]}, tracks[0].id,
        requester_device_id=c.id,
    )
    db_session.refresh(state)
    assert state.active_device_id == c.id


def test_remote_commands_do_not_claim_output_from_an_offline_active_device(db_session, user, tracks):
    # next/seek/queue edits change server state only; they never move output.
    a = playback.register_device(db_session, user, "Laptop", "web")
    c = playback.register_device(db_session, user, "Phone", "mobile")
    playback.set_queue_context(
        db_session, user, {"kind": "ids", "ids": [t.id for t in tracks]}, tracks[0].id,
        requester_device_id=a.id,
    )
    playback.disconnect_device(db_session, user, a.id)
    playback.next_track(db_session, user)
    _seek_current(db_session, user, 3.0)
    state = playback.get_or_create_state(db_session, user)
    assert state.active_device_id == a.id
    assert c.id != a.id


def test_reconnect_reuses_active_device_without_handing_off(db_session, user):
    first = playback.register_device(db_session, user, "Phone", "mobile", client_token="phone")
    desktop = playback.register_device(db_session, user, "Laptop", "web", client_token="desktop")
    state = playback.get_or_create_state(db_session, user)
    state.is_playing = True
    db_session.commit()

    # A phone backgrounding can drop its control socket while its media keeps
    # playing.  It must stay the output instead of silently moving to laptop.
    playback.disconnect_device(db_session, user, first.id)
    state = playback.get_or_create_state(db_session, user)
    assert state.active_device_id == first.id
    assert state.is_playing is True
    assert db_session.get(type(first), first.id).is_connected is False

    resumed = playback.register_device(
        db_session, user, "Phone", "mobile", True, client_token="phone"
    )
    assert resumed.id == first.id
    assert resumed.is_connected is True
    assert playback.get_or_create_state(db_session, user).active_device_id == first.id
    assert desktop.id != first.id


def test_stale_resume_flag_cannot_steal_another_devices_output(db_session, user):
    phone = playback.register_device(db_session, user, "Phone", "mobile", client_token="phone")
    desktop = playback.register_device(db_session, user, "Laptop", "web", client_token="desktop")
    state = playback.get_or_create_state(db_session, user)
    state.is_playing = True
    state.active_device_id = phone.id
    db_session.commit()

    playback.disconnect_device(db_session, user, desktop.id)
    resumed_desktop = playback.register_device(
        db_session, user, "Laptop", "web", True, client_token="desktop"
    )
    assert resumed_desktop.id == desktop.id
    assert playback.get_or_create_state(db_session, user).active_device_id == phone.id


def test_legacy_active_device_can_be_claimed_only_by_a_resuming_client(db_session, user):
    legacy = playback.register_device(db_session, user, "Old Phone", "mobile")
    state = playback.get_or_create_state(db_session, user)
    state.is_playing = True
    db_session.commit()

    fresh = playback.register_device(
        db_session, user, "Phone", "mobile", True, client_token="phone"
    )
    assert fresh.id != legacy.id
    assert playback.get_or_create_state(db_session, user).active_device_id == fresh.id


def test_same_device_reopen_without_resume_stops_stale_playback(db_session, user):
    phone = playback.register_device(db_session, user, "Phone", "mobile", client_token="phone")
    state = playback.get_or_create_state(db_session, user)
    state.is_playing = True
    db_session.commit()

    playback.disconnect_device(db_session, user, phone.id)
    playback.register_device(db_session, user, "Phone", "mobile", client_token="phone")
    assert playback.get_or_create_state(db_session, user).is_playing is False


def test_first_device_without_resume_stops_orphaned_playback(db_session, user):
    state = playback.get_or_create_state(db_session, user)
    state.is_playing = True
    db_session.commit()

    device = playback.register_device(db_session, user, "Laptop", "web")
    state = playback.get_or_create_state(db_session, user)
    assert state.active_device_id == device.id
    assert state.is_playing is False


def test_set_queue_context_starts_playback_at_clicked_item(db_session, user, tracks):
    error = _set_queue(db_session, user, tracks, start_index=1)
    assert error is None
    state = playback.get_or_create_state(db_session, user)
    assert state.media_item_id == tracks[1].id
    # Always 0 for a freshly-started context - the clicked item is placed
    # first in the new materialized window, not at its natural-order index.
    assert state.queue_index == 0
    assert state.is_playing is True
    assert state.position_seconds == 0.0
    # Windowed forward from the click - nothing before it is materialized
    # (the accepted "Previous can't walk further back than this" tradeoff).
    snapshot = playback.build_state_snapshot(db_session, user)
    assert snapshot["queue_media_item_ids"] == [tracks[1].id, tracks[2].id]
    assert snapshot["queue_context_count"] == 3


def test_set_queue_context_rejects_inaccessible_item(db_session, user, tracks):
    error = playback.set_queue_context(db_session, user, {"kind": "ids", "ids": [999999]}, 999999)
    assert error is not None
    state = playback.get_or_create_state(db_session, user)
    assert state.media_item_id is None  # untouched


def test_next_and_prev_track_move_through_queue(db_session, user, tracks):
    _set_queue(db_session, user, tracks, start_index=0)
    playback.next_track(db_session, user)
    state = playback.get_or_create_state(db_session, user)
    assert state.media_item_id == tracks[1].id

    playback.next_track(db_session, user)
    state = playback.get_or_create_state(db_session, user)
    assert state.media_item_id == tracks[2].id

    playback.prev_track(db_session, user)
    playback.prev_track(db_session, user)
    playback.prev_track(db_session, user)  # past the start
    state = playback.get_or_create_state(db_session, user)
    assert state.media_item_id == tracks[0].id


def test_next_track_past_the_end_autoplays_with_repeat_off(db_session, user, tracks):
    # repeat is "off" by default - going past the last track shouldn't
    # just stop. No artist/genre data to match on here, but the library
    # still has two other tracks, so the random-library fallback tier of
    # the autoplay continuation picks one of them instead of clamping.
    _set_queue(db_session, user, tracks, start_index=0)
    playback.next_track(db_session, user)
    playback.next_track(db_session, user)
    playback.next_track(db_session, user)  # past the end
    state = playback.get_or_create_state(db_session, user)
    assert state.media_item_id in {tracks[0].id, tracks[1].id}


def test_autoplay_past_the_end_drops_the_queue_origin(db_session, user, tracks):
    # Now Playing's "Playing from" link: once autoplay moves on past what
    # was started, the tracks no longer come from it.
    origin = {"kind": "album", "label": "A", "payload": {"album": "A", "artist": None}}
    playback.set_queue_context(
        db_session, user, {"kind": "ids", "ids": [tracks[0].id]}, tracks[0].id, origin=origin
    )
    assert playback.build_state_snapshot(db_session, user)["queue_origin"] == origin
    playback.next_track(db_session, user)
    state = playback.get_or_create_state(db_session, user)
    assert state.media_item_id != tracks[0].id
    assert playback.build_state_snapshot(db_session, user)["queue_origin"] is None


def test_add_to_queue_next_does_not_disturb_current_playback(db_session, user, tracks):
    _set_queue(db_session, user, tracks[:2], start_index=0)
    playback.set_playing(db_session, user, True)
    _seek_current(db_session, user, 42.0)

    playback.add_to_queue(db_session, user, tracks[2].id, mode="next")

    state = playback.get_or_create_state(db_session, user)
    assert state.media_item_id == tracks[0].id  # unchanged
    assert state.position_seconds == 42.0  # unchanged
    snapshot = playback.build_state_snapshot(db_session, user)
    assert snapshot["queue_media_item_ids"] == [tracks[0].id, tracks[2].id, tracks[1].id]


def test_add_to_queue_end_appends(db_session, user, tracks):
    _set_queue(db_session, user, tracks[:1], start_index=0)
    playback.add_to_queue(db_session, user, tracks[1].id, mode="end")
    snapshot = playback.build_state_snapshot(db_session, user)
    assert snapshot["queue_media_item_ids"] == [tracks[0].id, tracks[1].id]


def test_add_to_queue_on_empty_queue_starts_playback(db_session, user, tracks):
    error = playback.add_to_queue(db_session, user, tracks[0].id, mode="end")
    assert error is None
    state = playback.get_or_create_state(db_session, user)
    assert state.media_item_id == tracks[0].id
    assert state.is_playing is True


def test_add_many_to_queue_next_inserts_the_items_in_order_after_the_current_one(
    db_session, user, tracks
):
    _set_queue(db_session, user, tracks[:1], start_index=0)
    _seek_current(db_session, user, 42.0)

    error = playback.add_many_to_queue(
        db_session, user, [tracks[1].id, tracks[2].id, tracks[1].id], mode="next"
    )

    assert error is None
    state = playback.get_or_create_state(db_session, user)
    assert state.media_item_id == tracks[0].id
    assert state.position_seconds == 42.0
    snapshot = playback.build_state_snapshot(db_session, user)
    assert snapshot["queue_media_item_ids"] == [
        tracks[0].id, tracks[1].id, tracks[2].id, tracks[1].id
    ]
    # Each queued entry is its own row, even a repeated song.
    assert len(set(snapshot["queue_item_ids"])) == 4


def test_add_many_to_queue_end_appends_in_order(db_session, user, tracks):
    _set_queue(db_session, user, tracks[:1], start_index=0)
    playback.add_to_queue(db_session, user, tracks[0].id, mode="end")
    playback.add_many_to_queue(db_session, user, [tracks[2].id, tracks[1].id], mode="end")
    snapshot = playback.build_state_snapshot(db_session, user)
    assert snapshot["queue_media_item_ids"] == [
        tracks[0].id, tracks[0].id, tracks[2].id, tracks[1].id
    ]


def test_add_many_to_queue_on_an_empty_queue_starts_the_first_item(db_session, user, tracks):
    error = playback.add_many_to_queue(db_session, user, [tracks[1].id, tracks[0].id], mode="next")
    assert error is None
    state = playback.get_or_create_state(db_session, user)
    assert state.media_item_id == tracks[1].id
    assert state.queue_index == 0
    assert state.is_playing is True
    snapshot = playback.build_state_snapshot(db_session, user)
    assert snapshot["queue_media_item_ids"] == [tracks[1].id, tracks[0].id]


def test_add_many_to_queue_refuses_the_whole_batch_for_one_unknown_item(db_session, user, tracks):
    _set_queue(db_session, user, tracks[:1], start_index=0)
    before = playback.build_state_snapshot(db_session, user)
    error = playback.add_many_to_queue(db_session, user, [tracks[1].id, 99999], mode="end")
    assert error == "Media item 99999 not found"
    assert playback.build_state_snapshot(db_session, user) == before


def test_add_many_to_queue_refuses_an_empty_or_oversized_batch(db_session, user, tracks):
    _set_queue(db_session, user, tracks[:1], start_index=0)
    before = playback.build_state_snapshot(db_session, user)
    assert playback.add_many_to_queue(db_session, user, [], mode="end") == "Invalid add-to-queue command"
    too_many = [tracks[1].id] * (playback.MAX_EXPLICIT_IDS + 1)
    assert playback.add_many_to_queue(db_session, user, too_many, mode="end") == "Too many items to queue at once"
    assert playback.add_many_to_queue(db_session, user, [tracks[1].id], mode="sideways") == (
        "Queue mode must be 'next' or 'end'"
    )
    assert playback.build_state_snapshot(db_session, user) == before


def test_add_many_to_queue_accepts_the_largest_batch(db_session, user, tracks):
    _set_queue(db_session, user, tracks[:1], start_index=0)
    batch = [tracks[1].id] * playback.MAX_EXPLICIT_IDS
    assert playback.add_many_to_queue(db_session, user, batch, mode="end") is None
    snapshot = playback.build_state_snapshot(db_session, user)
    assert len(snapshot["queue_media_item_ids"]) == playback.MAX_EXPLICIT_IDS + 1


def test_seek_and_track_end_for_a_row_that_is_not_current_change_nothing(
    db_session, user, tracks
):
    _set_queue(db_session, user, tracks[:2], start_index=0)
    _seek_current(db_session, user, 12.0)
    stale = _current_row(db_session, user) + 1000

    assert playback.seek(db_session, user, 30.0, stale) == playback.STALE_QUEUE_ITEM
    assert playback.track_ended(db_session, user, stale) == playback.STALE_QUEUE_ITEM

    state = playback.get_or_create_state(db_session, user)
    assert state.media_item_id == tracks[0].id
    assert state.position_seconds == 12.0


def test_seek_clamps_negative_to_zero(db_session, user, tracks):
    _set_queue(db_session, user, tracks[:1], start_index=0)
    _seek_current(db_session, user, -5.0)
    state = playback.get_or_create_state(db_session, user)
    assert state.position_seconds == 0.0


def test_transfer_to_changes_active_device(db_session, user):
    playback.register_device(db_session, user, "Laptop", "web")
    d2 = playback.register_device(db_session, user, "Phone", "mobile")
    error = playback.transfer_to(db_session, user, d2.id)
    assert error is None
    state = playback.get_or_create_state(db_session, user)
    assert state.active_device_id == d2.id


def test_transfer_to_rejects_other_users_device(db_session, user):
    other = User(username="other", password_hash=hash_password("x"))
    db_session.add(other)
    db_session.commit()
    other_device = playback.register_device(db_session, other, "Their laptop", "web")

    error = playback.transfer_to(db_session, user, other_device.id)
    assert error is not None


def test_transfer_to_rejects_offline_device(db_session, user):
    device = playback.register_device(db_session, user, "Phone", "mobile")
    playback.disconnect_device(db_session, user, device.id)

    assert playback.transfer_to(db_session, user, device.id) == "Device is offline"


def test_disconnect_active_device_keeps_it_selected_for_reconnect(db_session, user):
    d1 = playback.register_device(db_session, user, "Laptop", "web")
    d2 = playback.register_device(db_session, user, "Phone", "mobile")
    playback.transfer_to(db_session, user, d1.id)

    playback.disconnect_device(db_session, user, d1.id)
    state = playback.get_or_create_state(db_session, user)
    assert state.active_device_id == d1.id
    assert db_session.get(type(d1), d1.id).is_connected is False
    assert db_session.get(type(d2), d2.id).is_connected is True


def test_disconnect_last_device_keeps_active_identity_for_reconnect(db_session, user):
    d1 = playback.register_device(db_session, user, "Laptop", "web")
    playback.disconnect_device(db_session, user, d1.id)
    state = playback.get_or_create_state(db_session, user)
    assert state.active_device_id == d1.id


def test_removing_last_active_device_stops_playback(db_session, user):
    device = playback.register_device(db_session, user, "Phone", "mobile")
    playback.set_playing(db_session, user, True)

    assert playback.remove_device(db_session, user, device.id) is None
    state = playback.get_or_create_state(db_session, user)
    assert state.active_device_id is None
    assert state.is_playing is False


def test_build_state_snapshot_shape(db_session, user, tracks):
    device = playback.register_device(db_session, user, "Laptop", "web")
    _set_queue(db_session, user, tracks, start_index=0)

    snapshot = playback.build_state_snapshot(db_session, user)
    assert snapshot["media_item_id"] == tracks[0].id
    assert snapshot["queue_media_item_ids"] == [t.id for t in tracks]
    assert snapshot["active_device_id"] == device.id
    assert snapshot["devices"][0]["is_active"] is True
    assert snapshot["repeat_mode"] == "off"
    assert snapshot["queue_context_count"] == 3


def test_jump_to_index_plays_selected_track(db_session, user, tracks):
    _set_queue(db_session, user, tracks, start_index=0)
    playback.set_playing(db_session, user, False)

    playback.jump_to_index(db_session, user, 2)

    state = playback.get_or_create_state(db_session, user)
    assert state.media_item_id == tracks[2].id
    assert state.queue_index == 2
    assert state.is_playing is True


def _make_many(db_session, tracks, extra_count=10) -> list[MediaItem]:
    many = tracks + [
        MediaItem(
            library_root_id=tracks[0].library_root_id,
            kind=MediaKind.track,
            relative_path=f"extra{i}.mp3",
            title=f"Extra {i}",
        )
        for i in range(extra_count)
    ]
    db_session.add_all(many[len(tracks) :])
    db_session.commit()
    for item in many:
        db_session.refresh(item)
    return many


def test_shuffle_on_does_not_move_or_duplicate_the_currently_playing_item(db_session, user, tracks):
    many = _make_many(db_session, tracks)
    _set_queue(db_session, user, many, start_index=1)
    playback.set_shuffle(db_session, user, True)

    snapshot = playback.build_state_snapshot(db_session, user)
    assert snapshot["is_shuffled"] is True
    assert snapshot["media_item_id"] == many[1].id
    assert snapshot["queue_index"] == 0
    assert snapshot["queue_media_item_ids"][0] == many[1].id
    # The currently-playing item must not also appear again in its own
    # freshly-shuffled tail (see _context_with_exclude); everything else in
    # the whole context - shuffling draws from all of it, not just what was
    # windowed forward from the click - shows up somewhere in it once.
    rest = snapshot["queue_media_item_ids"][1:]
    assert len(rest) == len(set(rest))
    assert set(rest) == {t.id for t in many if t.id != many[1].id}


def test_shuffle_off_restores_natural_order_when_nothing_shuffled_ahead_yet(db_session, user, tracks):
    _set_queue(db_session, user, tracks, start_index=0)
    playback.set_shuffle(db_session, user, True)
    playback.set_shuffle(db_session, user, False)

    snapshot = playback.build_state_snapshot(db_session, user)
    assert snapshot["is_shuffled"] is False
    assert snapshot["queue_media_item_ids"] == [t.id for t in tracks]
    assert snapshot["media_item_id"] == tracks[0].id
    assert snapshot["queue_index"] == 0


def test_shuffle_off_keeps_playing_item_current_and_loses_nothing(db_session, user, tracks):
    _set_queue(db_session, user, tracks, start_index=0)
    playback.set_shuffle(db_session, user, True)
    playback.next_track(db_session, user)  # move into the shuffled tail
    playing_id = playback.get_or_create_state(db_session, user).media_item_id

    playback.set_shuffle(db_session, user, False)

    state = playback.get_or_create_state(db_session, user)
    assert state.media_item_id == playing_id  # unchanged, just re-indexed
    snapshot = playback.build_state_snapshot(db_session, user)
    # Nothing from the context is lost, regardless of which order shuffle
    # happened to play tracks[1]/tracks[2] in before this.
    assert set(snapshot["queue_media_item_ids"]) == {t.id for t in tracks}
    assert snapshot["queue_media_item_ids"][snapshot["queue_index"]] == playing_id


def test_new_queue_is_shuffled_immediately_when_shuffle_is_already_on(db_session, user, tracks):
    _set_queue(db_session, user, tracks[:1], start_index=0)
    playback.set_shuffle(db_session, user, True)

    many = _make_many(db_session, tracks)
    _set_queue(db_session, user, many, start_index=0)

    snapshot = playback.build_state_snapshot(db_session, user)
    assert snapshot["is_shuffled"] is True
    assert snapshot["media_item_id"] == many[0].id
    # Not simply left in the input order end-to-end.
    assert snapshot["queue_media_item_ids"] != [t.id for t in many]
    assert set(snapshot["queue_media_item_ids"]) == {t.id for t in many}


def test_remove_from_queue_before_current_shifts_index_back(db_session, user, tracks):
    _set_queue(db_session, user, tracks, start_index=2)
    snapshot = playback.build_state_snapshot(db_session, user)
    # Nothing precedes tracks[2] in the materialized window (it was the
    # click target) - insert something ahead of it explicitly instead.
    playback.add_to_queue(db_session, user, tracks[0].id, mode="next")
    playback.next_track(db_session, user)  # now on tracks[0], queue_index 1
    snapshot = playback.build_state_snapshot(db_session, user)
    queue_item_id = snapshot["queue_item_ids"][0]

    error = playback.remove_from_queue(db_session, user, queue_item_id)
    assert error is None

    state = playback.get_or_create_state(db_session, user)
    assert state.media_item_id == tracks[0].id  # still the same track playing
    assert state.queue_index == 0  # shifted down since something before it left


def test_remove_current_track_advances_to_next(db_session, user, tracks):
    _set_queue(db_session, user, tracks, start_index=0)
    snapshot = playback.build_state_snapshot(db_session, user)
    queue_item_id = snapshot["queue_item_ids"][0]

    playback.remove_from_queue(db_session, user, queue_item_id)

    state = playback.get_or_create_state(db_session, user)
    assert state.media_item_id == tracks[1].id
    assert state.queue_index == 0


def test_remove_last_remaining_track_clears_playback(db_session, user, tracks):
    _set_queue(db_session, user, tracks[:1], start_index=0)
    snapshot = playback.build_state_snapshot(db_session, user)
    queue_item_id = snapshot["queue_item_ids"][0]

    playback.remove_from_queue(db_session, user, queue_item_id)

    state = playback.get_or_create_state(db_session, user)
    assert state.media_item_id is None
    assert state.is_playing is False


def test_remove_from_queue_missing_item_errors(db_session, user, tracks):
    _set_queue(db_session, user, tracks, start_index=0)
    error = playback.remove_from_queue(db_session, user, 999999)
    assert error is not None


def test_move_queue_item_swaps_and_tracks_current_index(db_session, user, tracks):
    _set_queue(db_session, user, tracks, start_index=0)
    snapshot = playback.build_state_snapshot(db_session, user)
    current_item_id, next_item_id = snapshot["queue_item_ids"][0], snapshot["queue_item_ids"][1]

    error = playback.move_queue_item(db_session, user, next_item_id, direction=-1)
    assert error is None

    state = playback.get_or_create_state(db_session, user)
    snapshot = playback.build_state_snapshot(db_session, user)
    # The moved item is now first, the still-current track shifted to index 1.
    assert snapshot["queue_media_item_ids"][0] == tracks[1].id
    assert snapshot["queue_media_item_ids"][1] == tracks[0].id
    assert state.media_item_id == tracks[0].id
    assert state.queue_index == 1
    assert snapshot["queue_item_ids"][1] == current_item_id


def test_move_queue_item_at_edge_is_a_no_op(db_session, user, tracks):
    _set_queue(db_session, user, tracks, start_index=0)
    snapshot = playback.build_state_snapshot(db_session, user)
    first_item_id = snapshot["queue_item_ids"][0]

    error = playback.move_queue_item(db_session, user, first_item_id, direction=-1)
    assert error is None

    snapshot = playback.build_state_snapshot(db_session, user)
    assert snapshot["queue_media_item_ids"] == [t.id for t in tracks]


def test_reorder_queue_item_moves_to_arbitrary_position(db_session, user, tracks):
    _set_queue(db_session, user, tracks, start_index=0)
    snapshot = playback.build_state_snapshot(db_session, user)
    last_item_id = snapshot["queue_item_ids"][2]

    error = playback.reorder_queue_item(db_session, user, last_item_id, target_index=0)
    assert error is None

    snapshot = playback.build_state_snapshot(db_session, user)
    assert snapshot["queue_media_item_ids"] == [tracks[2].id, tracks[0].id, tracks[1].id]
    # The still-playing track (originally at index 0) moved along with the reorder.
    state = playback.get_or_create_state(db_session, user)
    assert state.media_item_id == tracks[0].id
    assert state.queue_index == 1


def test_reorder_queue_item_clamps_out_of_range_target(db_session, user, tracks):
    _set_queue(db_session, user, tracks, start_index=0)
    snapshot = playback.build_state_snapshot(db_session, user)
    first_item_id = snapshot["queue_item_ids"][0]

    error = playback.reorder_queue_item(db_session, user, first_item_id, target_index=999)
    assert error is None

    snapshot = playback.build_state_snapshot(db_session, user)
    assert snapshot["queue_media_item_ids"] == [tracks[1].id, tracks[2].id, tracks[0].id]


def test_reorder_queue_item_missing_item_errors(db_session, user, tracks):
    _set_queue(db_session, user, tracks, start_index=0)
    error = playback.reorder_queue_item(db_session, user, 999999, target_index=0)
    assert error is not None


def test_remove_device_drops_it_and_hands_off_active(db_session, user):
    first = playback.register_device(db_session, user, "Laptop", "web")
    second = playback.register_device(db_session, user, "Phone", "mobile")
    state = playback.get_or_create_state(db_session, user)
    assert state.active_device_id == first.id

    error = playback.remove_device(db_session, user, first.id)
    assert error is None

    from app.models import Device

    assert db_session.get(Device, first.id) is None
    db_session.refresh(state)
    assert state.active_device_id == second.id


def test_remove_device_rejects_another_users_device(db_session, user):
    mine = playback.register_device(db_session, user, "Laptop", "web")
    other = User(username="plain", password_hash="x")
    db_session.add(other)
    db_session.commit()

    error = playback.remove_device(db_session, other, mine.id)
    assert error is not None

    from app.models import Device

    assert db_session.get(Device, mine.id) is not None


def _autoplay_library(db_session, user, *, seed_artist, seed_genre=None):
    """A seed plus, for each fallback tier, one ordinary track, one disliked
    track and one inside an ignored folder."""
    from app.models import IgnoredLibraryFolder, TrackDislike

    root = LibraryRoot(path="/autoplay", display_name="Autoplay")
    db_session.add(root)
    db_session.commit()

    def track(title, path, artist, genre=None):
        item = MediaItem(
            library_root_id=root.id,
            kind=MediaKind.track,
            relative_path=path,
            title=title,
            artist=artist,
            genre=genre,
        )
        db_session.add(item)
        return item

    seed = track("Seed", "seed.mp3", seed_artist, seed_genre)
    ok = track("Ok", "ok.mp3", seed_artist or "Someone", seed_genre)
    disliked = track("Disliked", "disliked.mp3", seed_artist or "Someone", seed_genre)
    track("Ignored", "Hidden/ignored.mp3", seed_artist or "Someone", seed_genre)
    db_session.add(IgnoredLibraryFolder(library_root_id=root.id, relative_path="Hidden"))
    db_session.commit()
    db_session.add(TrackDislike(user_id=user.id, media_item_id=disliked.id))
    db_session.commit()
    return seed, ok


def test_autoplay_similar_tier_leaves_out_dislikes(db_session, user):
    from app.models import CollectionPreference

    seed, ok = _autoplay_library(db_session, user, seed_artist="Seeder", seed_genre="Rock")
    # "Similar" means another artist sharing the genre.
    ok.artist = "Similar"
    other = MediaItem(
        library_root_id=ok.library_root_id, kind=MediaKind.track,
        relative_path="shunned.mp3", title="Shunned", artist="Shunned", genre="Rock",
    )
    db_session.add_all([
        other,
        CollectionPreference(user_id=user.id, kind="artist", artist="Shunned", state="disliked"),
    ])
    db_session.commit()
    for item in db_session.query(MediaItem).filter(MediaItem.title.in_(["Disliked", "Ignored"])):
        item.artist = "Similar"
    db_session.commit()

    assert playback._continuation_tracks(db_session, user, seed.id) == [ok.id]


def test_autoplay_same_artist_tier_leaves_out_dislikes_and_ignored_folders(db_session, user):
    # No genre at all, so the similar tier comes back empty.
    seed, ok = _autoplay_library(db_session, user, seed_artist="Lonely")
    assert playback._continuation_tracks(db_session, user, seed.id) == [ok.id]


def test_autoplay_random_tier_leaves_out_dislikes_and_ignored_folders(db_session, user):
    # No artist, so both earlier tiers come back empty.
    seed, ok = _autoplay_library(db_session, user, seed_artist=None)
    assert playback._continuation_tracks(db_session, user, seed.id) == [ok.id]


def test_disliking_the_current_song_keeps_the_queue_and_index(db_session, user, tracks):
    from app.preferences import set_track_state

    _set_queue(db_session, user, tracks, start_index=1)
    before = playback.build_state_snapshot(db_session, user)

    set_track_state(db_session, user, tracks[1].id, "disliked")

    after = playback.build_state_snapshot(db_session, user)
    assert after["queue_media_item_ids"] == before["queue_media_item_ids"]
    assert after["queue_index"] == before["queue_index"]
    assert playback.get_or_create_state(db_session, user).media_item_id == tracks[1].id


# --- End of queue follows what the queries return ---------------------------


def _tiny_window(monkeypatch) -> None:
    """One generated row ahead of the current one, so a context item can be
    deleted before the window has materialised it."""
    monkeypatch.setattr(playback, "WINDOW_TARGET", 1)
    monkeypatch.setattr(playback, "WINDOW_BATCH", 1)


def _delete_media(db_session, *items: MediaItem) -> None:
    from app.cleanup import delete_media_records

    delete_media_records(db_session, [item.id for item in items])
    db_session.commit()


def test_shuffled_context_holding_a_track_twice_wraps_on_repeat_all(db_session, user, tracks):
    # The IN query of a shuffled pass returns the duplicated track once while
    # the cached count counts it twice: the cursor never reaches the count,
    # and repeat-all must still wrap instead of restarting the last track.
    playback.set_shuffle(db_session, user, True)
    assert playback.set_queue_context(
        db_session, user, {"kind": "ids", "ids": [tracks[0].id, tracks[1].id, tracks[0].id]},
        tracks[0].id,
    ) is None
    playback.set_repeat(db_session, user, "all")

    _end_current(db_session, user)
    assert playback.get_or_create_state(db_session, user).media_item_id == tracks[1].id
    _end_current(db_session, user)
    state = playback.get_or_create_state(db_session, user)
    assert state.media_item_id == tracks[0].id
    assert state.is_playing is True


def test_unshuffled_context_with_a_deleted_track_advances_without_an_error(
    db_session, user, tracks, monkeypatch
):
    # The dead id is not inserted as a queue row; with foreign keys on
    # that would raise IntegrityError out of track_ended on every retry.
    _tiny_window(monkeypatch)
    _set_queue(db_session, user, tracks, start_index=0)
    _delete_media(db_session, tracks[2])

    _end_current(db_session, user)
    assert playback.get_or_create_state(db_session, user).media_item_id == tracks[1].id
    _end_current(db_session, user)  # must not raise

    snapshot = playback.build_state_snapshot(db_session, user)
    assert tracks[2].id not in snapshot["queue_media_item_ids"]
    # Repeat is off: autoplay continues with what the library still has.
    assert snapshot["media_item_id"] == tracks[0].id
    assert snapshot["is_playing"] is True


def test_shuffled_context_with_a_deleted_track_still_wraps_on_repeat_all(
    db_session, user, tracks, monkeypatch
):
    _tiny_window(monkeypatch)
    playback.set_shuffle(db_session, user, True)
    _set_queue(db_session, user, tracks, start_index=0)
    playback.set_repeat(db_session, user, "all")
    queued = playback.build_state_snapshot(db_session, user)["queue_media_item_ids"]
    assert len(queued) == 2  # the start track plus one drawn from the pass
    never_drawn = next(t for t in tracks if t.id not in queued)
    _delete_media(db_session, never_drawn)

    _end_current(db_session, user)
    assert playback.get_or_create_state(db_session, user).media_item_id == queued[1]
    _end_current(db_session, user)
    state = playback.get_or_create_state(db_session, user)
    # Wrapped into a new pass instead of restarting the last track forever.
    assert state.media_item_id == tracks[0].id
    assert state.is_playing is True


def test_set_shuffle_after_manual_additions_still_queues_every_context_track(
    db_session, user, tracks
):
    # set_shuffle must not exclude the manually added item and count it
    # against the context, or the last context track is never queued.
    many = _make_many(db_session, tracks, extra_count=3)
    context_items, added = many[:5], many[5]
    _set_queue(db_session, user, context_items, start_index=0)
    playback.add_to_queue(db_session, user, added.id, mode="next")
    playback.next_track(db_session, user)  # now playing the manual addition
    assert playback.get_or_create_state(db_session, user).media_item_id == added.id

    playback.set_shuffle(db_session, user, True)

    snapshot = playback.build_state_snapshot(db_session, user)
    assert set(snapshot["queue_media_item_ids"]) == {t.id for t in many[:6]}
    assert len(snapshot["queue_media_item_ids"]) == 6


def test_set_shuffle_stores_each_exclusion_once_and_only_from_the_context(
    db_session, user, tracks
):
    many = _make_many(db_session, tracks, extra_count=1)
    _set_queue(db_session, user, many[:3], start_index=0)
    playback.add_to_queue(db_session, user, many[3].id, mode="next")
    playback.add_to_queue(db_session, user, many[0].id, mode="next")
    playback.next_track(db_session, user)
    playback.next_track(db_session, user)

    playback.set_shuffle(db_session, user, True)

    state = playback.get_or_create_state(db_session, user)
    context = playback._get_context(state)
    assert context is not None
    assert context["exclude_ids"] == [many[0].id]


def test_library_context_shrunk_by_a_scan_still_wraps_on_repeat_all(
    db_session, user, tracks, monkeypatch
):
    # The cached count of a library context goes stale when a scan removes
    # tracks; exhaustion is what the batch query returns.
    _tiny_window(monkeypatch)
    assert playback.set_queue_context(
        db_session, user, {"kind": "library", "filters": {}}, tracks[0].id
    ) is None
    playback.set_repeat(db_session, user, "all")
    _delete_media(db_session, tracks[2])

    _end_current(db_session, user)
    assert playback.get_or_create_state(db_session, user).media_item_id == tracks[1].id
    _end_current(db_session, user)
    state = playback.get_or_create_state(db_session, user)
    assert state.media_item_id == tracks[0].id
    assert state.is_playing is True


def test_two_shuffles_of_one_context_are_different_orders_not_one_order_rotated(
    db_session, user, tracks
):
    # Adding the seed to every key would only move the starting point:
    # after track X the same track Y would follow on every shuffle.
    many = _make_many(db_session, tracks, extra_count=60)
    context = {"kind": "ids", "ids": [t.id for t in many]}

    def successors(seed: int) -> set[tuple[int, int]]:
        order = playback._context_shuffled_batch(db_session, user, context, 0, len(many), seed)
        assert sorted(order) == sorted(t.id for t in many)
        return set(zip(order, order[1:] + order[:1], strict=True))

    shared = [len(successors(seed) & successors(seed + 1)) for seed in (0, 7, 123456, 2**31 - 2)]
    # A rotation shares all (or all but one) of its neighbouring pairs.
    assert max(shared) < len(many) // 3


def test_a_short_run_of_neighbouring_tracks_is_not_left_in_album_order(db_session, user, tracks):
    # Seed-derived multipliers alone keep an album in its own order about once
    # in seventy shuffles.
    many = _make_many(db_session, tracks, extra_count=9)
    context = {"kind": "ids", "ids": [t.id for t in many]}
    natural = sorted(t.id for t in many)
    orders = {
        tuple(playback._context_shuffled_batch(db_session, user, context, 0, len(many), seed))
        for seed in range(0, 2**31, 2**31 // 400)
    }
    assert tuple(natural) not in orders
    assert tuple(reversed(natural)) not in orders
    assert len(orders) > 390  # practically every seed is its own order


def test_shuffled_batches_page_through_one_order(db_session, user, tracks):
    many = _make_many(db_session, tracks, extra_count=20)
    context = {"kind": "ids", "ids": [t.id for t in many]}
    whole = playback._context_shuffled_batch(db_session, user, context, 0, len(many), 99)
    paged = [
        i
        for cursor in range(0, len(many), 5)
        for i in playback._context_shuffled_batch(db_session, user, context, cursor, 5, 99)
    ]
    assert paged == whole


def test_repeat_all_without_shuffle_plays_every_track_on_every_pass(db_session, user, tracks):
    # The wrap must not leave the last track out of the whole next pass:
    # A B C | A B | A C | ...
    _set_queue(db_session, user, tracks, start_index=0)
    playback.set_repeat(db_session, user, "all")
    played = [playback.get_or_create_state(db_session, user).media_item_id]
    for _ in range(len(tracks) * 3 - 1):
        _end_current(db_session, user)
        played.append(playback.get_or_create_state(db_session, user).media_item_id)
    assert played == [t.id for t in tracks] * 3


def test_repeat_all_with_shuffle_plays_every_track_on_every_pass(db_session, user, tracks):
    many = _make_many(db_session, tracks, extra_count=3)
    playback.set_shuffle(db_session, user, True)
    _set_queue(db_session, user, many, start_index=0)
    playback.set_repeat(db_session, user, "all")
    played = [playback.get_or_create_state(db_session, user).media_item_id]
    for _ in range(len(many) * 4 - 1):
        _end_current(db_session, user)
        played.append(playback.get_or_create_state(db_session, user).media_item_id)
    passes = [played[i : i + len(many)] for i in range(0, len(played), len(many))]
    assert all(sorted(p) == sorted(t.id for t in many) for p in passes)
    # A new pass never opens with the track that closed the last one.
    assert all(a[-1] != b[0] for a, b in zip(passes, passes[1:], strict=False))


def test_last_track_of_a_queue_with_no_context_stops_playback(db_session, user, tracks):
    playback.add_to_queue(db_session, user, tracks[0].id, mode="end")
    playback.add_to_queue(db_session, user, tracks[1].id, mode="end")
    playback.next_track(db_session, user)

    _end_current(db_session, user)

    state = playback.get_or_create_state(db_session, user)
    # Stays on the last track, stopped at its start - not restarted forever.
    assert state.media_item_id == tracks[1].id
    assert state.queue_index == 1
    assert state.is_playing is False
    assert state.position_seconds == 0.0


def test_repeat_all_keeps_looping_a_one_track_context(db_session, user, tracks):
    _set_queue(db_session, user, tracks[:1], start_index=0)
    playback.set_repeat(db_session, user, "all")

    _end_current(db_session, user)
    _end_current(db_session, user)

    state = playback.get_or_create_state(db_session, user)
    assert state.media_item_id == tracks[0].id
    assert state.is_playing is True


# --- The queue stays consistent when media goes away ------------------------


def _positions(db_session, user) -> list[int]:
    return [q.position for q in playback._queue(db_session, user)]


def test_deleting_an_earlier_queue_item_keeps_the_same_track_current(db_session, user, tracks):
    many = _make_many(db_session, tracks, extra_count=2)
    _set_queue(db_session, user, many, start_index=0)
    for _ in range(3):
        playback.next_track(db_session, user)
    before = playback.build_state_snapshot(db_session, user)
    current_item = before["queue_item_ids"][before["queue_index"]]
    assert before["queue_media_item_ids"][before["queue_index"]] == many[3].id

    _delete_media(db_session, many[1])

    after = playback.build_state_snapshot(db_session, user)
    assert after["queue_index"] == 2
    assert after["queue_item_ids"][after["queue_index"]] == current_item
    assert after["media_item_id"] == many[3].id
    assert _positions(db_session, user) == list(range(len(after["queue_item_ids"])))
    playback.next_track(db_session, user)
    assert playback.get_or_create_state(db_session, user).media_item_id == many[4].id


def test_deleting_the_current_item_moves_to_the_next_and_stops(db_session, user, tracks):
    _set_queue(db_session, user, tracks, start_index=0)
    playback.next_track(db_session, user)
    state = playback.get_or_create_state(db_session, user)
    state.position_seconds = 42.0
    db_session.commit()

    _delete_media(db_session, tracks[1])

    state = playback.get_or_create_state(db_session, user)
    assert state.media_item_id == tracks[2].id
    assert state.queue_index == 1
    assert state.is_playing is False
    assert state.position_seconds == 0.0
    assert _positions(db_session, user) == [0, 1]


def test_deleting_the_last_item_while_it_plays_falls_back_to_the_one_before(
    db_session, user, tracks
):
    _set_queue(db_session, user, tracks, start_index=0)
    playback.next_track(db_session, user)
    playback.next_track(db_session, user)

    _delete_media(db_session, tracks[2])

    state = playback.get_or_create_state(db_session, user)
    assert state.media_item_id == tracks[1].id
    assert state.queue_index == 1
    assert state.is_playing is False


def test_deleting_every_queued_track_empties_the_queue(db_session, user, tracks):
    _set_queue(db_session, user, tracks, start_index=1)

    _delete_media(db_session, *tracks)

    state = playback.get_or_create_state(db_session, user)
    assert playback._queue(db_session, user) == []
    assert state.media_item_id is None
    assert state.queue_index == 0
    assert state.is_playing is False


def test_add_to_end_after_a_shuffle_toggle_appends_at_the_tail(db_session, user, tracks):
    # Turning shuffle on drops the unplayed generated rows; that must not
    # leave a gap in the positions, or "add to end" lands before the
    # newly drawn shuffled rows instead of after them.
    many = _make_many(db_session, tracks, extra_count=2)
    _set_queue(db_session, user, many[:3], start_index=0)
    playback.add_to_queue(db_session, user, many[3].id, mode="end")
    playback.set_shuffle(db_session, user, True)
    assert _positions(db_session, user) == list(range(4))

    playback.add_to_queue(db_session, user, many[4].id, mode="end")

    snapshot = playback.build_state_snapshot(db_session, user)
    assert snapshot["queue_media_item_ids"][-1] == many[4].id
    assert _positions(db_session, user) == list(range(5))


def test_add_to_end_closes_a_gap_left_by_an_older_release(db_session, user, tracks):
    _set_queue(db_session, user, tracks[:2], start_index=0)
    last = playback._queue(db_session, user)[-1]
    last.position = 7  # a gap, as older databases can hold
    db_session.commit()

    playback.add_to_queue(db_session, user, tracks[2].id, mode="end")

    snapshot = playback.build_state_snapshot(db_session, user)
    assert snapshot["queue_media_item_ids"] == [t.id for t in tracks]
    assert _positions(db_session, user) == [0, 1, 2]


def _private_track(db_session, user, *, granted: bool = True) -> MediaItem:
    from app.models import UserLibraryAccess

    root = LibraryRoot(path="/private", display_name="Private", is_shared=False)
    db_session.add(root)
    db_session.commit()
    item = MediaItem(
        library_root_id=root.id, kind=MediaKind.track, relative_path="p.mp3", title="Private"
    )
    db_session.add(item)
    if granted:
        db_session.add(UserLibraryAccess(user_id=user.id, library_root_id=root.id))
    db_session.commit()
    return item


def _revoke_all_grants(db_session, user) -> None:
    from app.models import UserLibraryAccess

    db_session.query(UserLibraryAccess).filter(UserLibraryAccess.user_id == user.id).delete()
    db_session.commit()


def test_losing_access_to_an_earlier_item_keeps_the_same_track_current(db_session, user, tracks):
    private = _private_track(db_session, user)
    order = [tracks[0], private, tracks[1], tracks[2]]
    _set_queue(db_session, user, order, start_index=0)
    playback.next_track(db_session, user)
    playback.next_track(db_session, user)
    assert playback.get_or_create_state(db_session, user).media_item_id == tracks[1].id

    _revoke_all_grants(db_session, user)

    snapshot = playback.build_state_snapshot(db_session, user)
    assert snapshot["queue_media_item_ids"] == [tracks[0].id, tracks[1].id, tracks[2].id]
    assert snapshot["queue_index"] == 1
    assert snapshot["media_item_id"] == tracks[1].id
    assert snapshot["is_playing"] is True
    playback.next_track(db_session, user)
    assert playback.get_or_create_state(db_session, user).media_item_id == tracks[2].id


def test_losing_access_to_the_current_item_moves_on_to_the_next(db_session, user, tracks):
    private = _private_track(db_session, user)
    _set_queue(db_session, user, [tracks[0], private, tracks[1]], start_index=0)
    playback.next_track(db_session, user)

    _revoke_all_grants(db_session, user)

    snapshot = playback.build_state_snapshot(db_session, user)
    assert snapshot["queue_media_item_ids"] == [tracks[0].id, tracks[1].id]
    assert snapshot["queue_index"] == 1
    assert snapshot["media_item_id"] == tracks[1].id
    assert snapshot["position_seconds"] == 0.0


def test_snapshot_names_the_current_queue_item_and_revision(db_session, user, tracks):
    empty = playback.build_state_snapshot(db_session, user)
    assert empty["current_queue_item_id"] is None
    assert empty["revision"] == 0

    _set_queue(db_session, user, tracks, start_index=0)
    playback.next_track(db_session, user)
    snapshot = playback.build_state_snapshot(db_session, user)
    assert snapshot["current_queue_item_id"] == snapshot["queue_item_ids"][1]
    # Reading a snapshot is not a change.
    assert playback.build_state_snapshot(db_session, user)["revision"] == snapshot["revision"]


def test_snapshot_keeps_every_existing_key(db_session, user, tracks):
    playback.register_device(db_session, user, "Laptop", "web")
    _set_queue(db_session, user, tracks, start_index=0)
    snapshot = playback.build_state_snapshot(db_session, user)
    assert set(snapshot) == {
        "media_item_id", "position_seconds", "is_playing", "active_device_id",
        "queue_index", "queue_media_item_ids", "queue_item_ids", "is_shuffled",
        "repeat_mode", "queue_context_count", "devices", "revision",
        "current_queue_item_id", "queue_origin",
    }
    assert set(snapshot["devices"][0]) == {
        "id", "name", "device_type", "is_active", "is_connected", "last_seen_at",
    }
    device = snapshot["devices"][0]
    assert isinstance(device["last_seen_at"], str) and "T" in device["last_seen_at"]


def test_set_queue_context_checks_a_long_id_list_in_one_query(db_session, user, tracks):
    from sqlalchemy import event

    many = _make_many(db_session, tracks, extra_count=797)
    statements: list[str] = []
    engine = db_session.get_bind()
    listener = lambda *args: statements.append(args[2])  # noqa: E731
    event.listen(engine, "before_cursor_execute", listener)
    try:
        assert playback.set_queue_context(
            db_session, user, {"kind": "ids", "ids": [t.id for t in many]}, many[0].id
        ) is None
    finally:
        event.remove(engine, "before_cursor_execute", listener)
    # Looking the ids up takes a fixed handful of queries, not a few per id.
    # The window's own row inserts are not counted.
    lookups = [s for s in statements if not s.lstrip().upper().startswith(("INSERT", "DELETE", "UPDATE"))]
    assert len(lookups) < 40, len(lookups)


def test_set_queue_context_names_the_first_missing_id(db_session, user, tracks):
    private = _private_track(db_session, user, granted=False)
    ids = [tracks[0].id, private.id, 999_999]
    assert playback.set_queue_context(
        db_session, user, {"kind": "ids", "ids": ids}, tracks[0].id
    ) == f"Media item {private.id} not found"


# --- Device rows -------------------------------------------------------------


def test_a_reconnecting_device_keeps_its_deduplicated_name(db_session, user):
    first = playback.register_device(db_session, user, "Phone", "mobile", client_token="a")
    second = playback.register_device(db_session, user, "Phone", "mobile", client_token="b")
    assert second.name == "Phone #2"
    playback.disconnect_device(db_session, user, second.id)

    again = playback.register_device(db_session, user, "Phone", "mobile", client_token="b")
    assert again.id == second.id
    assert again.name == "Phone #2"  # not a second plain "Phone"
    # The original keeps its own name rather than being renamed past itself.
    assert playback.register_device(
        db_session, user, "Phone", "mobile", client_token="a"
    ).name == first.name == "Phone"


def test_a_reconnect_under_another_name_still_avoids_a_clash(db_session, user):
    playback.register_device(db_session, user, "Laptop", "web", client_token="a")
    phone = playback.register_device(db_session, user, "Phone", "mobile", client_token="b")
    renamed = playback.register_device(db_session, user, "Laptop", "web", client_token="b")
    assert renamed.id == phone.id
    assert renamed.name == "Laptop #2"


def test_old_disconnected_devices_are_pruned_but_never_the_active_one(db_session, user):
    from datetime import timedelta

    from app.models import Device, utcnow

    long_ago = utcnow() - timedelta(days=31)
    active = playback.register_device(db_session, user, "Active", "web", client_token="1")
    stale = playback.register_device(db_session, user, "Stale", "web", client_token="2")
    recent = playback.register_device(db_session, user, "Recent", "web", client_token="3")
    connected = playback.register_device(db_session, user, "Connected", "web", client_token="4")
    for device in (active, stale, recent):
        playback.disconnect_device(db_session, user, device.id)
    for device in (active, stale, connected):
        device.last_seen_at = long_ago
    db_session.commit()
    assert playback.get_or_create_state(db_session, user).active_device_id == active.id

    assert playback.prune_stale_devices(db_session) == 1
    db_session.commit()

    remaining = {d.name for d in db_session.query(Device)}
    assert remaining == {"Active", "Recent", "Connected"}
