"""The admin's stored activity log: sign-ins, library tasks, edits, server
errors and the application log's own warnings and background-work lines
(app.log_mirror) as short lines in the activity_events table.

app.activity.log_action and the request middleware hand events to
``record_event``, which only puts them on an in-memory queue. One writer
thread turns the queue into rows, a batch per short transaction, so:

* recording never waits for the database, and a failed write never reaches
  the request (or the task) it describes - it is logged and dropped;
* the writer never shares a transaction with its caller, so an event logged
  while the caller still holds SQLite's write lock (inside an open write
  transaction) cannot wait on that same lock;
* the write lock is held only for one INSERT of the waiting batch; the
  names a summary mentions (library folder, song) are looked up first, in
  a read that ends before the write begins.

Summaries name what happened in words; they never carry a password, token,
API key, cookie or request body (``_SECRET_FIELD_WORDS`` fields are dropped
before a summary is built, whatever the caller passed)."""

import json
import logging
import queue
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import insert, select
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models import ActivityEvent, LibraryRoot, MediaItem, utcnow

logger = logging.getLogger(__name__)

LEVELS = ("info", "warning", "error")
# A level filter meaning "warning or error".
PROBLEMS = "problems"
SUMMARY_MAX_LENGTH = 300
# Events waiting for the writer; beyond this they are dropped (and counted
# in the log) rather than growing memory during a storm of errors.
QUEUE_MAX = 2000
WRITE_BATCH = 200
# Field names never put in a summary, whatever an event carries.
_SECRET_FIELD_WORDS = ("password", "token", "secret", "api_key", "apikey", "cookie", "credential")

TASK_LABELS = {
    "scan": "Scan",
    "full_setup": "Full setup scan",
    "full_setup_new": "Set up new entries",
    "mb_isrc": "MusicBrainz ISRC lookup",
    "mb_mbid": "MusicBrainz ID lookup",
    "enrich": "MusicBrainz details",
    "enrich_all": "MusicBrainz details (all)",
    # Last.fm tasks: not run any more, but named in stored events.
    "lf_metadata": "Last.fm metadata",
    "lf_metadata_all": "Last.fm metadata (all)",
    "lb_tags": "ListenBrainz tags",
    "lb_tags_all": "ListenBrainz tags (all)",
    "cover_art": "Cover art fetch",
    "artist_info": "Artist info fetch",
}
_SCAN_MODE_LABELS = {
    "metadata": "Rescan metadata",
    "full": "Full rescan",
    "confirm_removals": "Scan (confirmed removals)",
}


@dataclass
class _Pending:
    created_at: datetime
    kind: str
    level: str
    actor: str | None
    fields: dict[str, Any]
    request_id: str | None
    # The session factory current when the event was recorded, so an event
    # always lands in the database it describes (tests swap it per test).
    factory: Callable[[], Session]
    summary: str | None = None


_queue: queue.Queue[_Pending] = queue.Queue(maxsize=QUEUE_MAX)
_writer: threading.Thread | None = None
_writer_lock = threading.Lock()
_dropped = 0


def level_name(levelno: int) -> str:
    if levelno >= logging.ERROR:
        return "error"
    if levelno >= logging.WARNING:
        return "warning"
    return "info"


def actor_name(user: str | None) -> str | None:
    """"alice#1" (the request context's form) -> "alice"."""
    if not user:
        return None
    name, _sep, _id = str(user).rpartition("#")
    return name or str(user)


def _safe_fields(fields: dict[str, Any]) -> dict[str, Any]:
    """`fields` without secret-looking names, nor empty values."""
    return {
        key: value
        for key, value in fields.items()
        if value is not None and not any(word in key.lower() for word in _SECRET_FIELD_WORDS)
    }


def record_event(
    kind: str,
    level: str,
    *,
    actor: str | None = None,
    fields: dict[str, Any] | None = None,
    summary: str | None = None,
    request_id: str | None = None,
) -> None:
    """Queue one event for the activity log. Never raises and never waits
    for the database. `summary` overrides the one built from `fields`."""
    global _dropped
    try:
        pending = _Pending(
            created_at=utcnow(),
            kind=kind[:64],
            level=level if level in LEVELS else "info",
            actor=actor,
            fields=_safe_fields(fields or {}),
            request_id=request_id,
            factory=SessionLocal,
            summary=summary,
        )
        _ensure_writer()
        _queue.put_nowait(pending)
    except queue.Full:
        _dropped += 1
        if _dropped == 1 or _dropped % 100 == 0:
            logger.warning("Activity log queue is full; %d event(s) dropped so far", _dropped)
    except Exception:  # never let the log break what it describes
        logger.warning("Could not queue an activity event", exc_info=True)


def _ensure_writer() -> None:
    global _writer
    if _writer is not None and _writer.is_alive():
        return
    with _writer_lock:
        if _writer is None or not _writer.is_alive():
            # A plain daemon thread rather than app.background.spawn: it runs
            # for the life of the process, so shutdown's join_all would wait
            # its full timeout for it. Shutdown calls wait_for_writes instead.
            _writer = threading.Thread(target=_writer_loop, name="activity-log-writer", daemon=True)
            _writer.start()


def _writer_loop() -> None:
    while True:
        batch = [_queue.get()]
        while len(batch) < WRITE_BATCH:
            try:
                batch.append(_queue.get_nowait())
            except queue.Empty:
                break
        try:
            _write(batch)
        except Exception:  # pragma: no cover - _write catches per factory; this is a last resort
            logger.warning("Activity log write failed", exc_info=True)
        finally:
            for _ in batch:
                _queue.task_done()


def wait_for_writes(timeout: float = 5.0) -> bool:
    """Wait until every queued event has been written (or dropped). True
    when the queue drained within `timeout` seconds."""
    done = threading.Event()

    def join() -> None:
        _queue.join()
        done.set()

    threading.Thread(target=join, name="activity-log-join", daemon=True).start()
    return done.wait(timeout)


def _write(batch: list[_Pending]) -> None:
    by_factory: dict[int, list[_Pending]] = {}
    factories: dict[int, Callable[[], Session]] = {}
    for event in batch:
        by_factory.setdefault(id(event.factory), []).append(event)
        factories[id(event.factory)] = event.factory
    for key, events in by_factory.items():
        factory = factories[key]
        try:
            rows = _rows(factory, events)
            db = factory()
            try:
                db.execute(insert(ActivityEvent), rows)
                db.commit()
            finally:
                db.close()
        except Exception as exc:  # noqa: BLE001 - the log must never fail anything; say so and move on
            logger.warning("Could not store %d activity event(s): %s", len(events), exc)


def _rows(factory: Callable[[], Session], events: list[_Pending]) -> list[dict[str, Any]]:
    """Insert rows for `events`, with the root and song names their
    summaries mention looked up in one read session that is closed before
    the insert starts."""
    root_ids = {e.fields["root"] for e in events if isinstance(e.fields.get("root"), int)}
    media_ids = {e.fields["media"] for e in events if isinstance(e.fields.get("media"), int)}
    roots: dict[int, str] = {}
    media: dict[int, str] = {}
    if root_ids or media_ids:
        db = factory()
        try:
            if root_ids:
                roots = {
                    root_id: name
                    for root_id, name in db.execute(
                        select(LibraryRoot.id, LibraryRoot.display_name).where(LibraryRoot.id.in_(root_ids))
                    )
                }
            if media_ids:
                for item_id, title, artist in db.execute(
                    select(MediaItem.id, MediaItem.title, MediaItem.artist).where(MediaItem.id.in_(media_ids))
                ):
                    media[item_id] = f"{artist} - {title}" if artist else title
        except Exception as exc:  # noqa: BLE001 - names are a nicety; the event is still stored
            logger.warning("Activity log name lookup failed: %s", exc)
        finally:
            db.close()
    rows = []
    for event in events:
        root_id = event.fields.get("root") if isinstance(event.fields.get("root"), int) else None
        root_name = roots.get(root_id) if root_id is not None else None
        if root_name is None and root_id is not None and event.fields.get("name"):
            root_name = str(event.fields["name"])
        summary = event.summary or summarize(
            event.kind, event.fields, actor=event.actor, root_name=root_name,
            media_label=media.get(event.fields.get("media")),  # type: ignore[arg-type]
        )
        rows.append(
            {
                "created_at": event.created_at,
                "kind": event.kind,
                "level": event.level,
                "actor": event.actor,
                "summary": _one_line(summary),
                "root_id": root_id,
                "context": root_name,
                "request_id": event.request_id,
            }
        )
    return rows


def _one_line(text: str) -> str:
    flat = " ".join(str(text).split())
    if len(flat) > SUMMARY_MAX_LENGTH:
        flat = flat[: SUMMARY_MAX_LENGTH - 1] + "…"
    return flat


def _value(value: object) -> str:
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, default=str)
    return str(value)


def _plural(count: object, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def _task_label(fields: dict[str, Any]) -> str:
    kind = str(fields.get("kind") or "task")
    if kind == "scan" and fields.get("mode") in _SCAN_MODE_LABELS:
        return _SCAN_MODE_LABELS[str(fields["mode"])]
    return TASK_LABELS.get(kind, kind)


def _task_finish(who: str, fields: dict[str, Any], where: str) -> str:
    label = _task_label(fields)
    status = fields.get("status")
    if status == "failed":
        error = fields.get("error")
        return f"{label}{where} failed" + (f": {error}" if error else "")
    if status == "stopped":
        return f"{label}{where} stopped after {fields.get('processed', 0)} of {fields.get('total', 0)}"
    if fields.get("kind") in ("scan", "full_setup", "full_setup_new") and "added" in fields:
        return (
            f"{label}{where} finished: {fields.get('added', 0)} added, "
            f"{fields.get('updated', 0)} updated, {fields.get('removed', 0)} removed"
        )
    return f"{label}{where} finished ({fields.get('processed', 0)} of {fields.get('total', 0)})"


def summarize(
    kind: str,
    fields: dict[str, Any],
    *,
    actor: str | None = None,
    root_name: str | None = None,
    media_label: str | None = None,
) -> str:
    """A short line saying what `kind` with `fields` meant. Known events get
    a sentence; anything else reads "kind: key=value, ...". Secret-looking
    fields are left out either way."""
    fields = _safe_fields(fields)
    who = actor or "Someone"
    where = f" of {root_name}" if root_name else (f" of library {fields['root']}" if "root" in fields else "")
    song = media_label or (f"song #{fields['media']}" if "media" in fields else "a song")
    templates: dict[str, Callable[[], str]] = {
        "auth.login": lambda: f"{actor_name(str(fields.get('user'))) or who} signed in"
        + (f" from {fields['ip']}" if fields.get("ip") else ""),
        "auth.login_failed": lambda: f"Failed sign-in as {fields.get('username', '?')}"
        + (f" ({fields['reason']})" if fields.get("reason") else "")
        + (f" from {fields['ip']}" if fields.get("ip") else ""),
        "auth.logout": lambda: f"{who} signed out",
        "auth.logout_everywhere": lambda: f"{who} signed out on every device",
        "auth.password_change": lambda: f"{who} changed their password",
        "auth.username_change": lambda: f"{who} renamed the account {fields.get('old')} to {fields.get('new')}",
        "user.create": lambda: f"{who} created the user {actor_name(str(fields.get('target')))}"
        + (" (admin)" if fields.get("is_admin") else ""),
        "user.delete": lambda: f"{who} deleted the user {actor_name(str(fields.get('target')))}",
        "user.role": lambda: f"{who} made {actor_name(str(fields.get('target')))} "
        + ("an admin" if fields.get("is_admin") else "a regular user"),
        "user.password_reset": lambda: f"{who} reset the password of {actor_name(str(fields.get('target')))}",
        "root.create": lambda: f"{who} added the library folder {fields.get('name')} ({fields.get('path')})",
        "root.update": lambda: f"{who} changed the library folder {root_name or fields.get('root')}",
        "root.delete": lambda: f"{who} removed the library folder {fields.get('name')} ({_plural(fields.get('tracks', 0), 'track')})",
        "root.access_grant": lambda: f"{who} gave user #{fields.get('user')} access to {root_name or fields.get('root')}",
        "root.access_revoke": lambda: f"{who} took away user #{fields.get('user')}'s access to {root_name or fields.get('root')}",
        "task.start": lambda: f"{_task_label(fields)}{where} started" + (f" by {actor}" if actor else ""),
        "task.finish": lambda: _task_finish(who, fields, where),
        "task.stop_requested": lambda: f"{who} asked {_task_label(fields)}{where} to stop",
        "media.tags_edit": lambda: f"{who} edited the tags of {song}"
        + (f" ({', '.join(map(str, fields['fields']))})" if fields.get("fields") else ""),
        "media.bulk_tags": lambda: f"{who} edited tags in bulk: {fields.get('succeeded', 0)} saved, {fields.get('failed', 0)} failed",
        "media.bulk_remove": lambda: f"{who} removed {_plural(fields.get('removed', 0), 'file')}"
        + (f", {fields['failed']} failed" if fields.get("failed") else ""),
        "library.artist_split_rules": lambda: f"{who} changed the artist split rules",
        "art.remove_unrecorded": lambda: f"{who} removed {_plural(fields.get('removed', 0), 'artwork')} saved before sources were recorded",
        "artist.match": lambda: f"{who} matched artist #{fields.get('artist')} to MusicBrainz",
        "lastfm.server_key": lambda: f"{who} " + ("set" if fields.get("configured") else "removed") + " the server's Last.fm key",
        "privacy.lastfm_recommendations": lambda: f"{who} turned Last.fm recommendations "
        + ("on" if fields.get("enabled") else "off"),
        "privacy.online_recommendations": lambda: f"{who} turned ListenBrainz recommendations "
        + ("on" if fields.get("enabled") else "off"),
        "client.error": lambda: f"App error for {who}: {fields.get('message', '')}",
        "client.warning": lambda: f"App warning for {who}: {fields.get('message', '')}",
    }
    template = templates.get(kind)
    if template is not None:
        try:
            return template()
        except Exception:  # noqa: BLE001 - an odd field never loses the event; fall back below
            logger.debug("Activity summary for %s failed", kind, exc_info=True)
    parts = ", ".join(f"{key}={_value(value)}" for key, value in fields.items())
    return f"{kind}: {parts}" if parts else kind


# --- Reading -------------------------------------------------------------------


def _filters(
    kind: str | None, level: str | None, day: date | None, tz_offset_minutes: int
) -> list:
    filters = []
    if kind and kind.endswith("."):
        # A whole group: "log." is every "log.<area>" kind.
        filters.append(ActivityEvent.kind.startswith(kind, autoescape=True))
    elif kind:
        filters.append(ActivityEvent.kind == kind)
    if level == PROBLEMS:
        filters.append(ActivityEvent.level.in_(("warning", "error")))
    elif level:
        filters.append(ActivityEvent.level == level)
    if day is not None:
        # The day as the viewer's clock has it: getTimezoneOffset() minutes
        # are added to local time to get UTC.
        start = datetime(day.year, day.month, day.day) + timedelta(minutes=tz_offset_minutes)
        filters.append(ActivityEvent.created_at >= start)
        filters.append(ActivityEvent.created_at < start + timedelta(days=1))
    return filters


def list_events(
    db: Session,
    *,
    kind: str | None = None,
    level: str | None = None,
    day: date | None = None,
    tz_offset_minutes: int = 0,
    before_id: int | None = None,
    limit: int = 100,
) -> tuple[list[ActivityEvent], int | None]:
    """Newest first, and the id to pass as `before_id` for the next page
    (None when this is the last one)."""
    query = select(ActivityEvent).where(*_filters(kind, level, day, tz_offset_minutes))
    if before_id is not None:
        query = query.where(ActivityEvent.id < before_id)
    rows = list(db.scalars(query.order_by(ActivityEvent.id.desc()).limit(limit + 1)))
    next_before = rows[limit - 1].id if len(rows) > limit else None
    return rows[:limit], next_before


def iter_events(
    db: Session,
    *,
    kind: str | None = None,
    level: str | None = None,
    day: date | None = None,
    tz_offset_minutes: int = 0,
    batch: int = 1000,
) -> Iterable[ActivityEvent]:
    """Every matching event, newest first, read a page at a time."""
    before: int | None = None
    while True:
        rows, before = list_events(
            db, kind=kind, level=level, day=day, tz_offset_minutes=tz_offset_minutes,
            before_id=before, limit=batch,
        )
        yield from rows
        if before is None:
            return


def kinds_present(db: Session) -> list[str]:
    return sorted(db.scalars(select(ActivityEvent.kind).distinct()))


def download_line(event: ActivityEvent, tz_offset_minutes: int = 0) -> str:
    """"YYYY-MM-DD HH:MM:SS  kind  summary", in the viewer's local time."""
    local = event.created_at - timedelta(minutes=tz_offset_minutes)
    return f"{local:%Y-%m-%d %H:%M:%S}  {event.kind}  {event.summary}"

