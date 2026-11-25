import random
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any, cast

from sqlalchemy import CursorResult, and_, delete, func, or_, select
from sqlalchemy.orm import Session

from app.access import accessible_root_ids, can_access_root
from app.artist_credits import artist_link_filter
from app.config import settings
from app.db import count_rows
from app.discovery import similar_tracks_for_seed
from app.discovery.eligibility import mix_eligible
from app.discovery.familiarity import draw_weights, user_taste
from app.discovery.sampling import (
    _balanced_track_sample,
    _picks,
    _picks_for_ids,
    recording_keys_of,
)
from app.folder_exclusions import not_in_ignored_folder
from app.library_query import (
    DEFAULT_TRACK_DIRECTION,
    PLAYED_TRACK_SORTS,
    TrackSort,
    filter_tracks_query,
    track_sort_terms,
)
from app.models import (
    Device,
    MediaItem,
    MediaKind,
    PlaybackQueueItem,
    PlaybackState,
    PlayHistory,
    User,
    utcnow,
)
from app.playback_protocol import (
    PlaybackDeviceSnapshot,
    PlaybackStateSnapshot,
    QueueOrigin,
)
from app.preferences import not_disliked

# The queue is never fully materialized as DB rows - only a window this deep
# is kept ahead of what's playing, topped up as playback advances (see
# _ensure_window). This keeps both DB writes and the WebSocket state
# broadcast O(a window) regardless of whether the played-from context is 12
# tracks or 100,000.
WINDOW_TARGET = 200
WINDOW_REFILL_THRESHOLD = 50
WINDOW_BATCH = 150
# How many already-played "generated" rows stick around for Previous/the
# queue panel's history view before getting trimmed.
HISTORY_KEEP = 50
# The deterministic per-seed shuffle order is a 31-bit integer hash computed
# in SQL - see _scramble_key. Not cryptographic, it just needs to look
# shuffled and be computable as a plain SQL expression (SQLite has no builtin
# hash function).
SCRAMBLE_MASK = 0x7FFFFFFF


def get_or_create_state(db: Session, user: User) -> PlaybackState:
    state = db.get(PlaybackState, user.id)
    if state is None:
        state = PlaybackState(user_id=user.id)
        db.add(state)
        db.commit()
        db.refresh(state)
    return state


def _queue(db: Session, user: User) -> list[PlaybackQueueItem]:
    return list(
        db.scalars(
            select(PlaybackQueueItem)
            .where(PlaybackQueueItem.user_id == user.id)
            .order_by(PlaybackQueueItem.position)
        )
    )


def _dedupe_device_name(
    db: Session, user: User, base_name: str, exclude_device_id: int | None = None
) -> str:
    """Two tabs of the same browser (or two identical phones) generate the
    same client-guessed name - indistinguishable in the device picker
    otherwise, since the client can't see what else is already connected.
    `exclude_device_id` is the device being renamed, so it never clashes
    with its own current name."""
    query = select(Device.name).where(Device.user_id == user.id)
    if exclude_device_id is not None:
        query = query.where(Device.id != exclude_device_id)
    existing_names = set(db.scalars(query))
    if base_name not in existing_names:
        return base_name
    n = 2
    while f"{base_name} #{n}" in existing_names:
        n += 1
    return f"{base_name} #{n}"


def register_device(
    db: Session,
    user: User,
    name: str,
    device_type: str,
    resume_playback: bool = False,
    client_token: str | None = None,
) -> Device:
    """Register a browser/device without confusing a dropped socket for a
    stopped media player.

    New clients never take another client's output merely because they set a
    reload-resume flag.  A persistent browser token instead reuses its own
    row, so an active phone reconnects as the same active device.
    """
    device = None
    if client_token:
        device = db.scalars(
            select(Device)
            .where(Device.user_id == user.id, Device.client_token == client_token)
            .limit(1)
        ).first()
    was_connected = device.is_connected if device is not None else False
    if device is None:
        name = _dedupe_device_name(db, user, name or "Unknown device")
        device = Device(
            user_id=user.id,
            name=name,
            device_type=device_type or "web",
            client_token=client_token,
            is_connected=True,
        )
        db.add(device)
        db.commit()
        db.refresh(device)
    else:
        # De-duplicated again on every reconnect: the bare client-guessed
        # name would make two identical phones indistinguishable as soon as
        # either reconnected.
        device.name = _dedupe_device_name(
            db, user, name or "Unknown device", exclude_device_id=device.id
        )
        device.device_type = device_type or "web"
        device.is_connected = True
        device.last_seen_at = utcnow()

    state = get_or_create_state(db, user)
    active_device = db.get(Device, state.active_device_id) if state.active_device_id else None
    if active_device is None:
        # If the last page was closed, its persisted server state can still
        # say "playing" even though no browser is producing audio anymore.
        # Only a reload-resume request is allowed to continue that state.
        if state.is_playing and not resume_playback:
            state.is_playing = False
        # First device to show up becomes the output by default - there's
        # nothing playing anywhere yet, so someone has to be able to.
        state.active_device_id = device.id
    elif active_device.client_token is None and client_token and resume_playback:
        # Upgrade path for the older, per-WebSocket rows.  They cannot be
        # reconnected safely because they have no persistent identity.
        state.active_device_id = device.id
    elif (
        active_device.id == device.id
        and state.is_playing
        and not was_connected
        and not resume_playback
    ):
        # Reopening the same browser after a genuine close must not start
        # stale server playback.  A temporary background disconnect carries
        # the session-scoped resume marker and keeps playing.
        state.is_playing = False
    db.commit()
    return device


# A disconnected device row unseen this long is pruned at startup: every
# fresh token (a private window, cleared storage) otherwise leaves a row that
# every snapshot lists forever.
STALE_DEVICE_DAYS = 30


def prune_stale_devices(db: Session, now: datetime | None = None) -> int:
    """Delete devices that are disconnected and unseen for STALE_DEVICE_DAYS,
    never a user's active device (it stays the reconnect target). Per-boot
    housekeeping; the caller commits. Returns how many were deleted."""
    cutoff = (now or utcnow()) - timedelta(days=STALE_DEVICE_DAYS)
    active_ids = select(PlaybackState.active_device_id).where(
        PlaybackState.active_device_id.is_not(None)
    )
    result = db.execute(
        delete(Device).where(
            Device.is_connected.is_(False),
            Device.last_seen_at < cutoff,
            Device.id.notin_(active_ids),
        ),
        execution_options={"synchronize_session": False},
    )
    return cast("CursorResult[Any]", result).rowcount


def touch_device(db: Session, device_id: int) -> None:
    device = db.get(Device, device_id)
    if device is not None:
        device.last_seen_at = utcnow()
        db.commit()


def disconnect_device(db: Session, user: User, device_id: int) -> None:
    """Mark a control connection offline without changing audio output.

    Mobile operating systems routinely pause a background PWA's WebSocket
    while allowing its media element to continue.  Moving the active device
    to a desktop at that point strands the phone at its next natural end.
    """
    disconnect_device_for_user_id(db, user.id, device_id)


def disconnect_device_for_user_id(db: Session, user_id: int, device_id: int) -> None:
    """`disconnect_device` for socket teardown, which must not touch a
    `User` row an admin may have deleted while the socket was open."""
    device = db.get(Device, device_id)
    if device is not None and device.user_id == user_id:
        device.is_connected = False
        device.last_seen_at = utcnow()
    db.commit()


def claim_output_for_requester(
    db: Session,
    user: User,
    state: PlaybackState,
    requester_device_id: int | None,
    active_is_live: Callable[[int], bool] | None = None,
) -> None:
    """An explicit "play this" (a new queue, or `play`) from a connected
    device makes it the active device when nothing is heard anywhere else:
    playback is stopped, or the active device is gone or offline.

    Sequence it handles: device A plays, A's browser closes without pausing
    (its row stays, offline; the state keeps active_device_id = A and
    is_playing), device C starts an album. C becomes the active device; as a
    remote of the vanished A nothing would play anywhere.

    An offline active device may still be audible (a locked phone keeps
    playing with its socket suspended), which is why a mere reconnect never
    moves output (see register_device); a command to play here does. If A
    is connected, the command stays a remote control for A. When A comes
    back, it finds C active and stops its own audio (client sync effect).

    `active_is_live` (the socket layer's view, given by the playback socket)
    narrows "connected": the device's row can say connected while its socket
    has gone quiet - a closed tab whose connection never delivered a close,
    a frozen tab whose browser still answers the transport's pings, a row
    left connected with no socket at all. A playing active device speaks
    every few seconds (position reports), so one that has not been heard
    from for longer is treated like an offline one. Without the callback
    (direct callers, tests) the row's flag alone decides."""
    if requester_device_id is None:
        return
    requester = db.get(Device, requester_device_id)
    if requester is None or requester.user_id != user.id or not requester.is_connected:
        return
    active = db.get(Device, state.active_device_id) if state.active_device_id else None
    active_online = (
        active is not None
        and active.user_id == user.id
        and active.is_connected
        and (active.id == requester_device_id or active_is_live is None or active_is_live(active.id))
    )
    if not state.is_playing or not active_online:
        state.active_device_id = requester_device_id


# --- Queue context / windowing ---------------------------------------------


def _get_context(state: PlaybackState) -> dict | None:
    # Read-only: a changed context is a new dict given to _set_context.
    return state.queue_context_json


def _set_context(state: PlaybackState, context: dict, count: int) -> None:
    state.queue_context_json = context
    state.queue_context_count = count


def _context_with_exclude(
    db: Session, user: User, context: dict, exclude_ids: list[int] | None
) -> dict:
    """Every time the tail cursor resets to 0 (shuffle toggling either way,
    a repeat-all wraparound, or starting a shuffled context) it's about to
    draw a fresh pass over the *whole* context - which would otherwise be
    able to re-queue something already playing or already actually heard
    this pass. `exclude_ids` is stored as part of the context (not just
    applied to one batch) so every batch drawn for the rest of this pass
    stays consistent - an OFFSET-based cursor would otherwise drift if the
    excluded set changed between calls within the same pass.

    Only ids the context actually holds are kept, each once: set_shuffle
    passes everything played so far, which includes manually added tracks
    and repeats."""
    new_context = {k: v for k, v in context.items() if k != "exclude_ids"}
    wanted = list(dict.fromkeys(i for i in exclude_ids or () if i is not None))
    if wanted and context["kind"] == "ids":
        in_context = set(context["ids"])
        wanted = [i for i in wanted if i in in_context]
    elif wanted:
        matching = set(
            db.scalars(
                filter_tracks_query(db, user, context["filters"])
                .with_only_columns(MediaItem.id)
                .where(MediaItem.id.in_(wanted))
            )
        )
        wanted = [i for i in wanted if i in matching]
    if wanted:
        new_context["exclude_ids"] = wanted
    return new_context


def _context_sort(context: dict) -> tuple[TrackSort, str, datetime | None] | None:
    """A "library" context's Songs-tab sort, or None for the default order
    (artist, album, track number, id). See normalize in set_queue_context."""
    sort = context.get("sort")
    if sort is None:
        return None
    played_before = context.get("played_before")
    return (
        TrackSort(sort),
        context.get("direction") or DEFAULT_TRACK_DIRECTION[TrackSort(sort)],
        datetime.fromisoformat(played_before) if played_before else None,
    )


def _sorted_ids_query(db: Session, user: User, context: dict) -> tuple[Any, list]:
    """The context's track ids (as a select) and its ORDER BY terms."""
    query = filter_tracks_query(db, user, context["filters"]).with_only_columns(MediaItem.id)
    sorting = _context_sort(context)
    if sorting is None:
        return query, [MediaItem.artist, MediaItem.album, MediaItem.track_number, MediaItem.id]
    sort, direction, played_before = sorting
    return track_sort_terms(query, sort, direction, user.id, played_before)


def _context_natural_batch(db: Session, user: User, context: dict, cursor: int, n: int) -> list[int]:
    exclude = set(context.get("exclude_ids") or ())
    if context["kind"] == "ids":
        ids = [i for i in context["ids"] if i not in exclude] if exclude else context["ids"]
        return ids[cursor : cursor + n]
    query, order = _sorted_ids_query(db, user, context)
    if exclude:
        query = query.where(MediaItem.id.notin_(exclude))
    return list(db.scalars(query.order_by(*order).offset(cursor).limit(n)))


def _scramble_key(seed: int) -> Any:
    """The SQL sort key of a shuffled pass: an integer hash of the track id
    and the seed, built from what SQLite has (multiply, shift, and, or).

    Each seed is an order of its own. Adding the seed to one fixed multiple
    of the id would shift every key by the same amount, which only rotates
    one fixed order: the same track would follow the same track on every
    shuffle. Multiplying by seed-derived constants alone is still a
    straight line through the ids, and a short run of neighbouring ids (an
    album) would come out in album order about once in seventy shuffles.
    The xor-shift between the multiplies is what breaks the line.

    Everything is masked to 31 bits, so each product stays below 2**62."""
    mask = SCRAMBLE_MASK

    def xor_shift(value: Any, bits: int) -> Any:
        shifted = value.op(">>")(bits)
        # SQLite has no xor operator: a ^ b == (a | b) - (a & b).
        return value.op("|")(shifted) - value.op("&")(shifted)

    start = (seed * 0x85EBCA6B + 0x165667B1) & mask
    mixed = ((MediaItem.id.op("&")(mask)) * 0x9E3779B1 + start).op("&")(mask)
    mixed = (xor_shift(mixed, 15) * 0x2C1B3C6D).op("&")(mask)
    mixed = (xor_shift(mixed, 12) * 0x297A2D39).op("&")(mask)
    return xor_shift(mixed, 15)


def _context_shuffled_batch(
    db: Session, user: User, context: dict, cursor: int, n: int, seed: int
) -> list[int]:
    exclude = set(context.get("exclude_ids") or ())
    scramble = _scramble_key(seed)
    if context["kind"] == "ids":
        ids = [i for i in context["ids"] if i not in exclude] if exclude else context["ids"]
        base = select(MediaItem.id).where(MediaItem.id.in_(ids))
    else:
        base = filter_tracks_query(db, user, context["filters"]).with_only_columns(MediaItem.id)
        if exclude:
            base = base.where(MediaItem.id.notin_(exclude))
    return list(db.scalars(base.order_by(scramble, MediaItem.id).offset(cursor).limit(n)))


def _context_batch(
    db: Session, user: User, context: dict, cursor: int, n: int, state: PlaybackState
) -> list[int]:
    if state.is_shuffled:
        return _context_shuffled_batch(db, user, context, cursor, n, state.shuffle_seed or 0)
    return _context_natural_batch(db, user, context, cursor, n)


def _playable_ids(db: Session, user: User, ids: list[int]) -> list[int]:
    """`ids` minus any that no longer exist or sit in a root the user cannot
    see, in one query, keeping order and repeats."""
    if not ids:
        return []
    found = set(
        db.scalars(
            select(MediaItem.id).where(
                MediaItem.id.in_(set(ids)),
                MediaItem.library_root_id.in_(accessible_root_ids(db, user)),
            )
        )
    )
    return [i for i in ids if i in found]


def _natural_index_of(
    db: Session,
    user: User,
    context: dict,
    media_item_id: int | None,
    explicit_index: int | None = None,
) -> int | None:
    """Where an item falls in the context's natural (unshuffled) order -
    used so turning shuffle off can resume right after whatever's currently
    playing instead of restarting the context from the top."""
    if media_item_id is None:
        return None
    if context["kind"] == "ids":
        ids = context["ids"]
        if (
            explicit_index is not None
            and 0 <= explicit_index < len(ids)
            and ids[explicit_index] == media_item_id
        ):
            return explicit_index
        return ids.index(media_item_id) if media_item_id in ids else None
    if context.get("sort") is not None:
        # Any other order: the item's row number in it. One pass over the
        # matching tracks, run once when the queue starts.
        query, order = _sorted_ids_query(db, user, context)
        ranked = query.add_columns(func.row_number().over(order_by=order).label("rank")).subquery()
        rank = db.scalar(select(ranked.c.rank).where(ranked.c.id == media_item_id))
        return None if rank is None else rank - 1
    item = db.get(MediaItem, media_item_id)
    if item is None:
        return None
    a, al, tn = item.artist or "", item.album or "", item.track_number or 0
    art = func.coalesce(MediaItem.artist, "")
    alb = func.coalesce(MediaItem.album, "")
    trk = func.coalesce(MediaItem.track_number, 0)
    query = filter_tracks_query(db, user, context["filters"])
    return count_rows(
        db,
        query.where(
            or_(
                art < a,
                and_(art == a, alb < al),
                and_(art == a, alb == al, trk < tn),
                and_(art == a, alb == al, trk == tn, MediaItem.id < item.id),
            )
        ),
    )


def normalize_queue(
    db: Session,
    user_id: int,
    remove_item_ids: set[int] | frozenset[int] = frozenset(),
    *,
    stop_if_current_removed: bool = False,
) -> None:
    """Delete the queue rows in `remove_item_ids` and keep the rest usable.

    `queue_index` is a list index into the position-ordered queue
    everywhere, so removing rows (deleted media, a root the user lost, the
    unplayed tail dropped by set_shuffle) must renumber positions densely and
    move the index with the item that was current. If that item itself goes,
    the next surviving one takes its place (else the last one; else the queue
    is empty and playback stops). Call it BEFORE the rows are gone: the
    current item is found from the queue as it was. One queue query per user.
    """
    rows = list(
        db.scalars(
            select(PlaybackQueueItem)
            .where(PlaybackQueueItem.user_id == user_id)
            .order_by(PlaybackQueueItem.position)
        )
    )
    state = db.get(PlaybackState, user_id)
    index = state.queue_index if state is not None else 0
    current = rows[index] if 0 <= index < len(rows) else None
    survivors = [q for q in rows if q.id not in remove_item_ids]
    if len(survivors) != len(rows):
        # "fetch" also drops the deleted rows from the session: SQLite reuses
        # their ids, and a stale object would collide with a new queue row.
        db.execute(
            delete(PlaybackQueueItem).where(
                PlaybackQueueItem.id.in_([q.id for q in rows if q.id in remove_item_ids])
            ),
            execution_options={"synchronize_session": "fetch"},
        )
    for position, q in enumerate(survivors):
        if q.position != position:
            q.position = position
    if state is not None:
        if current is not None and current.id not in remove_item_ids:
            state.queue_index = survivors.index(current)
        elif survivors:
            # The current item went: what followed it slides into its place.
            later = [q for q in rows[index + 1 :] if q.id not in remove_item_ids]
            replacement = later[0] if later else survivors[-1]
            state.queue_index = survivors.index(replacement)
            state.media_item_id = replacement.media_item_id
            state.position_seconds = 0.0
            if stop_if_current_removed:
                state.is_playing = False
        else:
            state.queue_index = 0
            state.media_item_id = None
            state.position_seconds = 0.0
            state.is_playing = False
    # Flushed now so bulk statements that follow (cleanup's own deletes and
    # its PlaybackState update) see the re-derived state, not the old one.
    db.flush()


def _drop_unplayed_generated(db: Session, user: User, state: PlaybackState) -> None:
    queue = _queue(db, user)
    stale_ids = {q.id for i, q in enumerate(queue) if i > state.queue_index and q.source == "generated"}
    if stale_ids:
        # Renumbered, not just deleted: a gap left here would make a later
        # "add to end" land before the rows drawn after it.
        normalize_queue(db, user.id, stale_ids)


def _trim_history(db: Session, user: User, state: PlaybackState) -> None:
    queue = _queue(db, user)
    played_generated = [i for i, q in enumerate(queue) if i < state.queue_index and q.source == "generated"]
    to_trim = played_generated[: max(0, len(played_generated) - HISTORY_KEEP)]
    if not to_trim:
        return
    to_trim_ids = {queue[i].id for i in to_trim}
    db.execute(
        delete(PlaybackQueueItem).where(PlaybackQueueItem.id.in_(to_trim_ids)),
        execution_options={"synchronize_session": "fetch"},
    )
    # Positions double as list-indices everywhere else in this module
    # (queue_index is compared against both) - renumber so trimming never
    # opens a gap between the two.
    remaining = [q for q in queue if q.id not in to_trim_ids]
    removed_before_current = sum(1 for i in to_trim if i < state.queue_index)
    for position, q in enumerate(remaining):
        q.position = position
    state.queue_index -= removed_before_current


def _ensure_window(db: Session, user: User, state: PlaybackState) -> None:
    """Tops up the materialized queue from `queue_context` with whatever's
    still available, up to WINDOW_TARGET ahead of what's playing - called
    after every action that can move `queue_index` forward. Deliberately
    does *not* decide what happens once the context runs dry (looping,
    autoplay-continuation) - see _handle_context_exhaustion for that. A
    context smaller than the window (an album, a playlist, most mixes -
    in practice nearly everything) runs dry on the very first call, and
    treating that as "exhausted, act on repeat_mode" here would mean every
    ordinary play action pays for a live ListenBrainz round
    trip just because the window couldn't be topped up to 200, not because
    the user has actually listened through anything."""
    context = _get_context(state)
    if context is None:
        return
    queue = _queue(db, user)
    upcoming_generated = sum(
        1 for i, q in enumerate(queue) if i > state.queue_index and q.source == "generated"
    )
    if upcoming_generated >= WINDOW_REFILL_THRESHOLD:
        return

    # How far the context has run is what the batch queries return, not
    # `queue_context_count`: that cached count overstates what is left when
    # the context holds a track twice, a track was deleted since, or a scan
    # shrank a library context.
    wanted = min(WINDOW_TARGET - upcoming_generated, WINDOW_BATCH)
    if wanted <= 0:
        return
    next_position = (queue[-1].position + 1) if queue else 0
    appended = 0
    while appended < wanted:
        take = wanted - appended
        batch = _context_batch(db, user, context, state.tail_cursor, take, state)
        state.tail_cursor += len(batch)
        # A deleted (or no longer accessible) id is skipped, never inserted:
        # with foreign keys on it would fail every advance from here on.
        for media_item_id in _playable_ids(db, user, batch):
            db.add(
                PlaybackQueueItem(
                    user_id=user.id,
                    position=next_position,
                    original_position=next_position,
                    media_item_id=media_item_id,
                    source="generated",
                )
            )
            next_position += 1
            appended += 1
        if len(batch) < take:
            break  # a short batch: the context is exhausted
    # The session is autoflush=False (see app.db) - without an explicit
    # flush here, a query run immediately after this call (_apply_index in
    # next_track, in particular) wouldn't see the rows just added above.
    db.flush()

    _trim_history(db, user, state)


# Fallback pool size for the end-of-queue autoplay continuation - generous
# enough that _ensure_window's next few top-ups have real material to draw
# from, without pulling in most of a large library at once.
CONTINUATION_FALLBACK_LIMIT = 50


# Autoplay is seeded from the track that just ended plus up to this many
# tracks played before it from the queue (see _recently_played_seeds).
AUTOPLAY_CONTEXT_TRACKS = 4


def _continuation_tracks(db: Session, user: User, last_id: int) -> list[int]:
    """What repeat_mode "off" plays next once the context runs out - see
    _continuation_for_seeds, seeded from the last few tracks played."""
    return _continuation_for_seeds(db, user, _recently_played_seeds(db, user, last_id))


def _recently_played_seeds(db: Session, user: User, last_id: int) -> list[int]:
    """`last_id` (the queue's last track, the one ending now) preceded by up
    to AUTOPLAY_CONTEXT_TRACKS earlier queue tracks the user actually played
    (a play in the last settings.autoplay_recent_hours, else in the last
    day), oldest first - tracks merely queued, or skipped before they
    counted as a play, do not steer. Just `[last_id]` when nothing else was
    played. Two statements."""
    hours = settings.autoplay_recent_hours or 24.0
    since = utcnow() - timedelta(hours=hours)
    played = set(
        db.scalars(
            select(PlayHistory.media_item_id)
            .where(PlayHistory.user_id == user.id, PlayHistory.played_at >= since)
            .distinct()
        )
    )
    queue = db.scalars(
        select(PlaybackQueueItem.media_item_id)
        .where(PlaybackQueueItem.user_id == user.id)
        .order_by(PlaybackQueueItem.position)
    ).all()
    earlier: list[int] = []
    for media_id in reversed(queue):
        if media_id != last_id and media_id in played and media_id not in earlier:
            earlier.append(media_id)
            if len(earlier) >= AUTOPLAY_CONTEXT_TRACKS:
                break
    return [*reversed(earlier), last_id]


def _autoplay_exclusions(db: Session, user: User) -> set[int]:
    """What autoplay must not bring back: every track in the queue (played
    or still to come) and every track the user played in the last
    settings.autoplay_recent_hours."""
    excluded = set(
        db.scalars(select(PlaybackQueueItem.media_item_id).where(PlaybackQueueItem.user_id == user.id))
    )
    if settings.autoplay_recent_hours > 0:
        since = utcnow() - timedelta(hours=settings.autoplay_recent_hours)
        excluded.update(
            db.scalars(
                select(PlayHistory.media_item_id)
                .where(PlayHistory.user_id == user.id, PlayHistory.played_at >= since)
                .distinct()
            )
        )
    return excluded


def _continuation_for_seeds(db: Session, user: User, seed_ids: list[int]) -> list[int]:
    """Autoplay's next batch, grown from `seed_ids`: the song that just ended
    last, anchoring the batch, and the songs played before it steering it
    (similar_tracks_for_seed's `context_ids`: an artist's score is half its
    likeness to the last song and half to the centroid of the earlier ones,
    so autoplay follows the listening without drifting off). Tiers, first
    that finds anything wins:
    similar tracks (similar_tracks_for_seed: alike artists and labels,
    ListenBrainz's cached similar tracks, artist-balanced, one version per song,
    drawn by the user's listen outcomes and fatigue),
    then other tracks by the seed's artist (covers an artist with no genre
    tag and no cached similar-artist data), then something at random from
    the whole library. Every tier leaves out disliked tracks, ignored
    folders and tracks outside the mix length/album-type rules
    (app.discovery.eligibility), the seeds and every other version of them,
    what is already in the queue and what was played in the last
    settings.autoplay_recent_hours. Only when that leaves nothing at all
    anywhere are the queue and recent plays allowed back, and after that the
    length/album-type rules dropped: playback should
    only actually stop because the library has nothing left to give, never
    just because the one context that was playing ran out."""
    seeds = set(seed_ids)
    strict = _autoplay_exclusions(db, user) | seeds
    found = _continuation_tiers(db, user, seed_ids, strict)
    if not found and strict != seeds:
        found = _continuation_tiers(db, user, seed_ids, seeds)
    if not found:
        # A library of nothing but short or spoken tracks: rather than stop,
        # play them (the fallback tiers, without the length/album rules).
        found = _continuation_tiers(db, user, seed_ids, seeds, mix_rules=False)
    return found


def _continuation_tiers(
    db: Session, user: User, seed_ids: list[int], exclude: set[int], mix_rules: bool = True
) -> list[int]:
    last_id = seed_ids[-1]
    if mix_rules:
        similar = similar_tracks_for_seed(
            db, user, last_id, allow_fetch=False, exclude_ids=exclude,
            context_ids=seed_ids[:-1], kind="plain",
        )
        new_ids = [i for i in (similar or []) if i not in exclude]
        if new_ids:
            return new_ids

    accessible = accessible_root_ids(db, user)
    eligible = (
        MediaItem.library_root_id.in_(accessible),
        MediaItem.kind == MediaKind.track,
        not_in_ignored_folder(),
        not_disliked(user.id),
        *((mix_eligible(),) if mix_rules else ()),
    )
    used = recording_keys_of(_picks_for_ids(db, exclude))
    seed = db.get(MediaItem, last_id)
    if seed is not None and seed.artist:
        same_artist = [
            pick
            for pick in _picks(
                db,
                select(MediaItem).where(
                    *eligible, or_(MediaItem.artist == seed.artist, artist_link_filter(seed.artist))
                ),
            )
            if pick.id not in exclude
        ]
        chosen = _balanced_track_sample(
            same_artist, CONTINUATION_FALLBACK_LIMIT,
            weights=draw_weights(user_taste(db, user), same_artist, "plain"), used=set(used),
        )
        if chosen:
            return [pick.id for pick in chosen]

    # A random handful, with room for the excluded ones it may draw.
    drawn = (
        select(MediaItem.id)
        .where(*eligible)
        .order_by(func.random())
        .limit(CONTINUATION_FALLBACK_LIMIT + len(exclude))
    )
    anything = [
        pick for pick in _picks(db, select(MediaItem).where(MediaItem.id.in_(drawn))) if pick.id not in exclude
    ]
    return [
        pick.id
        for pick in _balanced_track_sample(
            anything, CONTINUATION_FALLBACK_LIMIT,
            weights=draw_weights(user_taste(db, user), anything, "plain"), used=set(used),
        )
    ]


def _handle_context_exhaustion(db: Session, user: User, state: PlaybackState) -> None:
    """Called only when playback is genuinely about to advance past the
    last materialized item - i.e. once per track actually finished, not
    once per window top-up - so repeat_mode's real work (and "off"'s
    ListenBrainz round trip for the autoplay continuation)
    happens at the pace of actual listening, not on every WS message.
    "all" loops the context (reshuffling if shuffled); "off" auto-continues
    via _continuation_tracks (Spotify-style autoplay, with fallbacks so it
    never just stops); "one" never reaches here (see track_ended)."""
    context = _get_context(state)
    if context is None:
        return
    if _context_batch(db, user, context, state.tail_cursor, 1, state):
        return  # context isn't actually exhausted - _ensure_window just hasn't caught up yet
    queue = _queue(db, user)
    last_id = queue[-1].media_item_id if queue else state.media_item_id

    if state.repeat_mode == "all":
        state.tail_cursor = 0
        # A new pass holds every track again: what this pass left out (the
        # track a shuffle was started from, what had already played when
        # shuffle was switched) belongs to this pass only. Leaving the last
        # track out of the next pass would drop a track from every pass
        # after the first (A B C | A B | A C ...).
        context = _context_with_exclude(db, user, context, None)
        if state.is_shuffled:
            # A fresh order that does not open with the track just played
            # (a context of one track has no other choice).
            for _ in range(8):
                seed = random.getrandbits(31)
                opening = _context_shuffled_batch(db, user, context, 0, 1, seed)
                if not opening or opening[0] != last_id:
                    break
            state.shuffle_seed = seed
        _set_context(state, context, state.queue_context_count)
    elif state.repeat_mode == "off":
        new_ids = _continuation_tracks(db, user, last_id) if last_id else []
        if new_ids:
            _set_context(state, {"kind": "ids", "ids": new_ids}, len(new_ids))
            state.tail_cursor = 0
            # Autoplay: what follows was not started from anything.
            state.queue_origin_json = None
    # repeat_mode == "one" never reaches here - track_ended short-circuits
    # before calling next_track at all.


# The most ids one message may name: an explicit `set_queue_context` list or
# an `add_many_to_queue` batch.
MAX_EXPLICIT_IDS = 5000


def set_queue_context(
    db: Session,
    user: User,
    context: dict,
    start_media_item_id: int,
    start_index: int | None = None,
    requester_device_id: int | None = None,
    active_is_live: Callable[[int], bool] | None = None,
    origin: dict | None = None,
    start_position: float = 0.0,
    start_paused: bool = False,
) -> str | None:
    """Replace the queue with a new played-from context and start playing
    `start_media_item_id` - either a small explicit id list (an album,
    playlist, generated-from-song result) or a large filterable set (the
    Songs tab / whole library). Only the first window is materialized; the
    rest is pulled lazily by _ensure_window, so this is O(a window) in DB
    writes no matter how big the context is.

    `start_position` and `start_paused` are for a device handing over what it
    played while it could not reach the server (the Android app offline): the
    queue starts where that device is, and stays paused if it was."""
    kind = context.get("kind")
    if kind == "ids":
        ids = context.get("ids")
        if not isinstance(ids, list) or not ids or any(type(i) is not int for i in ids):
            return "Invalid queue"
        # Bounded by how big an explicit list can reasonably be (an album,
        # playlist, or generated mix) - the library-wide case uses "library"
        # instead, so this cap doesn't reintroduce an O(library) cost.
        if len(ids) > MAX_EXPLICIT_IDS:
            return "Invalid queue"
        # One query for the whole list, not several per id.
        playable = set(_playable_ids(db, user, ids))
        missing = next((i for i in ids if i not in playable), None)
        if missing is not None:
            return f"Media item {missing} not found"
        normalized = {"kind": "ids", "ids": ids}
        count = len(ids)
    elif kind == "library":
        filters = context.get("filters")
        if not isinstance(filters, dict):
            return "Invalid queue"
        if any(
            value is not None and not isinstance(value, str)
            for key in ("q", "genre", "tag", "artist", "album")
            if (value := filters.get(key)) is not None
        ):
            return "Invalid queue"
        if filters.get("artist_is_null") not in (None, False, True):
            return "Invalid queue"
        normalized = {
            "kind": "library",
            "filters": {
                **{k: filters.get(k) for k in ("q", "genre", "tag", "artist", "album")},
                "artist_is_null": filters.get("artist_is_null", False),
            },
        }
        # The Songs tab's sort, so the queue follows the order on screen.
        # The default order (artist A-Z) is stored as no sort at all.
        sort_name, direction = context.get("sort"), context.get("direction")
        if sort_name is not None or direction is not None:
            if sort_name not in {s.value for s in TrackSort} or direction not in (None, "asc", "desc"):
                return "Invalid queue"
            sort = TrackSort(sort_name)
            direction = direction or DEFAULT_TRACK_DIRECTION[sort]
            if (sort, direction) != (TrackSort.artist, "asc"):
                normalized["sort"] = sort.value
                normalized["direction"] = direction
                if sort in PLAYED_TRACK_SORTS:
                    # Frozen at the start: the plays this queue makes must
                    # not move tracks around under its cursor.
                    normalized["played_before"] = utcnow().isoformat()
        count = count_rows(db, filter_tracks_query(db, user, normalized["filters"]))
    else:
        return "Invalid queue"

    if count == 0:
        return "No matching tracks"

    start_item = db.get(MediaItem, start_media_item_id)
    if start_item is None or not can_access_root(db, user, start_item.library_root_id):
        return f"Media item {start_media_item_id} not found"
    natural_index = _natural_index_of(
        db, user, normalized, start_media_item_id, start_index
    )
    if natural_index is None:
        return f"Media item {start_media_item_id} not found"

    db.execute(delete(PlaybackQueueItem).where(PlaybackQueueItem.user_id == user.id))
    state = get_or_create_state(db, user)
    claim_output_for_requester(db, user, state, requester_device_id, active_is_live)
    if state.is_shuffled:
        state.shuffle_seed = random.getrandbits(31)
        state.tail_cursor = 0
        # The clicked track is about to be placed at position 0 below -
        # exclude it from its own shuffled tail so it can't be drawn again
        # immediately after itself.
        normalized = _context_with_exclude(db, user, normalized, [start_media_item_id])
    else:
        state.tail_cursor = natural_index + 1
    _set_context(state, normalized, count)
    # What the queue was started from (see PlaybackState.queue_origin_json).
    state.queue_origin_json = origin
    state.queue_index = 0
    state.media_item_id = start_media_item_id
    state.position_seconds = start_position
    state.is_playing = not start_paused
    state.updated_at = utcnow()
    db.add(
        PlaybackQueueItem(
            user_id=user.id,
            position=0,
            original_position=0,
            media_item_id=start_media_item_id,
            source="generated",
        )
    )
    db.commit()

    _ensure_window(db, user, state)
    db.commit()
    return None


def add_to_queue(db: Session, user: User, media_item_id: int, mode: str) -> str | None:
    """Insert one item without disturbing what's currently playing - unlike
    `set_queue_context`, which replaces everything and resets position.
    `mode` is "next" (right after the current item) or "end"."""
    return add_many_to_queue(db, user, [media_item_id], mode)


def add_many_to_queue(
    db: Session, user: User, media_item_ids: list[int], mode: str
) -> str | None:
    """Insert `media_item_ids`, in that order, as one change (e.g. "Add next
    in queue" on an album). Every
    item is checked first; one that is missing or out of reach refuses the
    whole batch and nothing changes. At most MAX_EXPLICIT_IDS at a time, the
    same bound as an explicit `set_queue_context` list."""
    if mode not in {"next", "end"}:
        return "Queue mode must be 'next' or 'end'"
    if not media_item_ids:
        return "Invalid add-to-queue command"
    if len(media_item_ids) > MAX_EXPLICIT_IDS:
        return "Too many items to queue at once"
    playable = set(_playable_ids(db, user, media_item_ids))
    missing = next((i for i in media_item_ids if i not in playable), None)
    if missing is not None:
        return f"Media item {missing} not found"

    queue = _queue(db, user)
    state = get_or_create_state(db, user)
    was_empty = not queue
    insert_at = (state.queue_index + 1) if mode == "next" else len(queue)
    count = len(media_item_ids)

    # Renumbered from list indices rather than shifted by position, so a
    # gap in the stored positions cannot put "end" before the tail.
    for i, q in enumerate(queue):
        q.position = i if i < insert_at else i + count
    next_original_position = max((q.original_position for q in queue), default=-1) + 1
    db.add_all(
        PlaybackQueueItem(
            user_id=user.id,
            position=insert_at + offset,
            original_position=next_original_position + offset,
            media_item_id=media_item_id,
            source="explicit",
        )
        for offset, media_item_id in enumerate(media_item_ids)
    )

    if was_empty:
        state.queue_index = 0
        state.media_item_id = media_item_ids[0]
        state.is_playing = True
        state.position_seconds = 0.0

    state.updated_at = utcnow()
    db.commit()
    return None


def _apply_index(db: Session, user: User, state: PlaybackState, index: int) -> None:
    queue = _queue(db, user)
    if not queue:
        state.media_item_id = None
        state.is_playing = False
        return
    index = max(0, min(index, len(queue) - 1))
    state.queue_index = index
    state.media_item_id = queue[index].media_item_id
    state.position_seconds = 0.0


def next_track(db: Session, user: User) -> None:
    state = get_or_create_state(db, user)
    _ensure_window(db, user, state)
    queue = _queue(db, user)
    if queue and state.queue_index + 1 >= len(queue):
        # About to fall off the end of what's materialized *and* nothing
        # more is available from the context right now - this is the one
        # moment repeat_mode's real work (including "off"'s network round
        # trip for the autoplay continuation) is actually warranted.
        _handle_context_exhaustion(db, user, state)
        _ensure_window(db, user, state)
        queue = _queue(db, user)
    if queue and state.queue_index + 1 >= len(queue) and state.repeat_mode == "off":
        # Nothing left anywhere (no context, or autoplay found nothing):
        # stop on the last track. Moving to it again would restart it
        # forever. Repeat-all keeps doing that - it loops a one-track pass.
        state.is_playing = False
        state.position_seconds = 0.0
    else:
        _apply_index(db, user, state, state.queue_index + 1)
    state.updated_at = utcnow()
    db.commit()


def prev_track(db: Session, user: User) -> None:
    state = get_or_create_state(db, user)
    _apply_index(db, user, state, state.queue_index - 1)
    state.updated_at = utcnow()
    db.commit()


STALE_QUEUE_ITEM = "stale_queue_item"


def current_queue_item_id(db: Session, user: User) -> int | None:
    """The queue row at `queue_index` - what a message naming a
    `queue_item_id` must match to still be about what is playing."""
    state = get_or_create_state(db, user)
    if state.queue_index < 0:
        return None
    return db.execute(
        select(PlaybackQueueItem.id)
        .where(PlaybackQueueItem.user_id == user.id)
        .order_by(PlaybackQueueItem.position)
        .offset(state.queue_index)
        .limit(1)
    ).scalar_one_or_none()


def bump_revision(db: Session, user: User) -> None:
    """Count one broadcast change. Done in SQL so two sockets of one user
    bumping at once cannot both write the same number."""
    state = get_or_create_state(db, user)
    state.revision = PlaybackState.revision + 1
    db.commit()


def track_ended(db: Session, user: User, queue_item_id: int) -> str | None:
    """The active device's media element reached its natural end. A manual
    skip (`next_track`, the skip-next button) always advances regardless of
    repeat mode - only a *natural* end loops the same track when
    repeat_mode is "one", matching Spotify/Apple Music's convention.

    `queue_item_id` names the queue row that ended; the end only counts while
    that row is still current: a replay after a reconnect, or an end that
    crossed a Next pressed on another device, would otherwise skip a track.
    Returns STALE_QUEUE_ITEM when it was ignored for that reason."""
    state = get_or_create_state(db, user)
    if queue_item_id != current_queue_item_id(db, user):
        return STALE_QUEUE_ITEM
    if state.repeat_mode == "one":
        state.position_seconds = 0.0
        state.is_playing = True
        state.updated_at = utcnow()
        db.commit()
        return None
    next_track(db, user)
    return None


def jump_to_index(db: Session, user: User, index: int) -> None:
    """Play a specific queue entry immediately - clicking a track in the
    queue panel, as opposed to stepping one at a time via next/prev."""
    state = get_or_create_state(db, user)
    _ensure_window(db, user, state)
    _apply_index(db, user, state, index)
    state.is_playing = True
    state.updated_at = utcnow()
    db.commit()


def set_shuffle(db: Session, user: User, enabled: bool) -> None:
    state = get_or_create_state(db, user)
    if enabled == state.is_shuffled:
        return
    state.is_shuffled = enabled
    context = _get_context(state)
    if context is not None:
        # Either direction draws a fresh pass over the whole context, so
        # exclude everything actually played-or-current so far this pass
        # (not just the current item) - turning shuffle off needs this too:
        # resuming from the *natural* position of whatever's current would
        # silently drop any track shuffle had placed later but hadn't
        # actually reached yet.
        queue = _queue(db, user)
        already_played = [q.media_item_id for i, q in enumerate(queue) if i <= state.queue_index]
        state.tail_cursor = 0
        if enabled:
            state.shuffle_seed = random.getrandbits(31)
        context = _context_with_exclude(db, user, context, already_played)
        _set_context(state, context, state.queue_context_count)
        _drop_unplayed_generated(db, user, state)
    state.updated_at = utcnow()
    db.commit()
    if context is not None:
        _ensure_window(db, user, state)
        db.commit()


def set_repeat(db: Session, user: User, mode: str) -> str | None:
    if mode not in {"off", "all", "one"}:
        return "Invalid repeat mode"
    state = get_or_create_state(db, user)
    state.repeat_mode = mode
    state.updated_at = utcnow()
    db.commit()
    # If playback was already stalled at the end of a context (repeat was
    # off, nothing left), turning repeat on should pick that back up
    # immediately rather than needing an extra "next" press first.
    queue = _queue(db, user)
    if queue and state.queue_index + 1 >= len(queue):
        _handle_context_exhaustion(db, user, state)
    _ensure_window(db, user, state)
    db.commit()
    return None


def remove_from_queue(db: Session, user: User, queue_item_id: int) -> str | None:
    queue = _queue(db, user)
    state = get_or_create_state(db, user)
    idx = next((i for i, q in enumerate(queue) if q.id == queue_item_id), None)
    if idx is None:
        return "Queue item not found"

    db.delete(queue[idx])
    remaining = queue[:idx] + queue[idx + 1 :]
    for position, q in enumerate(remaining):
        q.position = position

    if idx < state.queue_index:
        state.queue_index -= 1
    elif idx == state.queue_index:
        # The playing item itself was removed - whatever slid into its
        # slot (originally the next item) becomes current.
        if remaining:
            state.queue_index = min(state.queue_index, len(remaining) - 1)
            state.media_item_id = remaining[state.queue_index].media_item_id
            state.position_seconds = 0.0
        else:
            state.queue_index = 0
            state.media_item_id = None
            state.is_playing = False
            state.position_seconds = 0.0

    state.updated_at = utcnow()
    db.commit()
    _ensure_window(db, user, state)
    db.commit()
    return None


def move_queue_item(db: Session, user: User, queue_item_id: int, direction: int) -> str | None:
    """Swap a queue entry with its neighbor - up/down reordering, same
    interaction as the tab-order list in Settings, rather than free-form
    drag and drop."""
    if direction not in (-1, 1):
        return "Direction must be -1 or 1"
    queue = _queue(db, user)
    state = get_or_create_state(db, user)
    idx = next((i for i, q in enumerate(queue) if q.id == queue_item_id), None)
    if idx is None:
        return "Queue item not found"

    swap_idx = idx + direction
    if swap_idx < 0 or swap_idx >= len(queue):
        return None  # already at that edge - a harmless no-op, not an error

    queue[idx].position, queue[swap_idx].position = queue[swap_idx].position, queue[idx].position
    if idx == state.queue_index:
        state.queue_index = swap_idx
    elif swap_idx == state.queue_index:
        state.queue_index = idx

    state.updated_at = utcnow()
    db.commit()
    return None


def remove_device(db: Session, user: User, device_id: int) -> str | None:
    """Explicitly drop a device from the list - e.g. one that's still shown
    but the user has actually walked away from. Reconnecting (a page
    refresh, or the tab regaining focus) just re-registers it, same as any
    other fresh connection - there's nothing to remember was removed."""
    device = db.get(Device, device_id)
    if device is None or device.user_id != user.id:
        return "Device not found"
    state = get_or_create_state(db, user)
    db.delete(device)
    if state.active_device_id == device_id:
        remaining = db.scalars(
            select(Device)
            .where(
                Device.user_id == user.id,
                Device.id != device_id,
                Device.is_connected.is_(True),
            )
            .order_by(Device.last_seen_at.desc())
            .limit(1)
        ).first()
        state.active_device_id = remaining.id if remaining else None
        if remaining is None:
            state.is_playing = False
    state.updated_at = utcnow()
    db.commit()
    return None


def reorder_queue_item(db: Session, user: User, queue_item_id: int, target_index: int) -> str | None:
    """Move a queue entry to an arbitrary position - backs drag-and-drop
    reordering, unlike `move_queue_item`'s single-step swap."""
    queue = _queue(db, user)
    state = get_or_create_state(db, user)
    idx = next((i for i, q in enumerate(queue) if q.id == queue_item_id), None)
    if idx is None:
        return "Queue item not found"

    current_queue_item_id = (
        queue[state.queue_index].id if 0 <= state.queue_index < len(queue) else None
    )

    target_index = max(0, min(target_index, len(queue) - 1))
    item = queue.pop(idx)
    queue.insert(target_index, item)
    for position, q in enumerate(queue):
        q.position = position

    if current_queue_item_id is not None:
        new_index = next((i for i, q in enumerate(queue) if q.id == current_queue_item_id), None)
        if new_index is not None:
            state.queue_index = new_index

    state.updated_at = utcnow()
    db.commit()
    return None


def set_playing(
    db: Session, user: User, is_playing: bool, position_seconds: float | None = None
) -> None:
    """`position_seconds` comes with a pause from the device producing the
    audio: where the listener paused, so a later broadcast cannot carry an
    older save. A remote's pause has none and keeps the saved position."""
    state = get_or_create_state(db, user)
    state.is_playing = is_playing
    if position_seconds is not None:
        state.position_seconds = max(0.0, position_seconds)
    state.updated_at = utcnow()
    db.commit()


def seek(db: Session, user: User, position_seconds: float, queue_item_id: int) -> str | None:
    """A scrub of the queue row `queue_item_id`. It applies only while that
    row is current, so a seek for the previous track cannot land on the next
    one."""
    state = get_or_create_state(db, user)
    if queue_item_id != current_queue_item_id(db, user):
        return STALE_QUEUE_ITEM
    state.position_seconds = max(0.0, position_seconds)
    state.updated_at = utcnow()
    db.commit()
    return None


def report_position(
    db: Session, user: User, device_id: int, queue_item_id: int, position_seconds: float
) -> str | None:
    """The active device's periodic position save (`position_report`).
    Only the device producing the audio knows where it is, and only for the
    item it names; anything else is dropped. Returns why it was dropped. An applied report
    counts its own revision: broadcast it without bump_revision."""
    state = get_or_create_state(db, user)
    if state.active_device_id != device_id:
        return "inactive_device"
    if queue_item_id != current_queue_item_id(db, user):
        return STALE_QUEUE_ITEM
    state.position_seconds = position_seconds
    state.updated_at = utcnow()
    # An applied report is always broadcast, so its revision is counted here
    # (in SQL, as bump_revision does) rather than by a second commit: every
    # playing user sends one of these every five seconds.
    state.revision = PlaybackState.revision + 1
    db.commit()
    return None


def transfer_to(db: Session, user: User, device_id: int) -> str | None:
    device = db.get(Device, device_id)
    if device is None or device.user_id != user.id:
        return "Device not found"
    if not device.is_connected:
        return "Device is offline"
    state = get_or_create_state(db, user)
    state.active_device_id = device_id
    state.updated_at = utcnow()
    db.commit()
    return None


def build_state_snapshot(db: Session, user: User) -> dict:
    state = get_or_create_state(db, user)
    queue_items = _queue(db, user)
    roots = accessible_root_ids(db, user)
    # Only the root of each queued item: this runs for every broadcast, and
    # whole MediaItem rows (tags, paths, JSON columns) are not needed for
    # this comparison.
    queued_media_roots = {
        media_item_id: library_root_id
        for media_item_id, library_root_id in db.execute(
            select(MediaItem.id, MediaItem.library_root_id).where(
                MediaItem.id.in_([q.media_item_id for q in queue_items])
            )
        )
    }
    valid_queue_items = [
        q for q in queue_items if queued_media_roots.get(q.media_item_id) in roots
    ]
    if len(valid_queue_items) != len(queue_items):
        # Items in a root the user lost: the index moves with the current
        # item; losing the
        # current item itself moves on to the next and keeps playing.
        valid_ids = {q.id for q in valid_queue_items}
        normalize_queue(db, user.id, {q.id for q in queue_items if q.id not in valid_ids})
        db.commit()
        queue_items = valid_queue_items
    devices = list(db.scalars(select(Device).where(Device.user_id == user.id)))

    return PlaybackStateSnapshot(
        media_item_id=state.media_item_id,
        position_seconds=state.position_seconds,
        is_playing=state.is_playing,
        active_device_id=state.active_device_id,
        queue_index=state.queue_index,
        queue_media_item_ids=[q.media_item_id for q in queue_items],
        queue_item_ids=[q.id for q in queue_items],
        is_shuffled=state.is_shuffled,
        repeat_mode=cast(Any, state.repeat_mode),  # validated by the model
        queue_context_count=state.queue_context_count,
        queue_origin=(
            QueueOrigin.model_validate(state.queue_origin_json)
            if state.queue_origin_json
            else None
        ),
        devices=[
            PlaybackDeviceSnapshot(
                id=d.id,
                name=d.name,
                device_type=d.device_type,
                is_active=d.id == state.active_device_id,
                is_connected=d.is_connected,
                last_seen_at=d.last_seen_at.isoformat(),
            )
            for d in devices
        ],
        revision=state.revision,
        current_queue_item_id=(
            queue_items[state.queue_index].id
            if 0 <= state.queue_index < len(queue_items)
            else None
        ),
    ).model_dump()
