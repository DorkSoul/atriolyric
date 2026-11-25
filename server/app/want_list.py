"""The want list: artists, albums and songs a user wants but doesn't own.

* An artist entry is live: every release group of theirs counts, including
  ones MusicBrainz adds later (app.catalog re-browses wanted artists
  weekly). Single albums/songs can be excluded from it.
* An album entry is a MusicBrainz release group; it is owned once every
  song of its edition is in the library (owned_count/total_count).
* A song entry is a recording. Radio bookmarks are song entries too.

Matching to the library never guesses loosely - a wrong "you own this" is
worse than a missed one. A song matches by recording MBID, or by an artist
the track credits plus the same title once version suffixes ("- 2011
Remaster", "(feat. X)") are ignored (app.owned_match.match_owned). Entries
stay listed once owned ("In library") until the user clears them.
"""

import csv
import io
import json
import logging
import threading
from collections.abc import Callable, Collection, Iterable
from datetime import timedelta

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.access import accessible_root_ids
from app.albums import section_for
from app.artist_credits import normalize_artist_name
from app.background import spawn
from app.catalog import (
    WANTED_REFRESH,
    cached_tracklist,
    catalog_fetched_at,
    discography,
    library_artist,
    owned_tracks,
    prefetch_tracklists_in_background,
    release_groups,
    tracklist,
)
from app.db import SessionLocal
from app.external_cache import cached, stale_ok
from app.library_cache import library_version
from app.metadata_info import (
    artist_candidates,
    find_artist,
    mb_release_group,
    resolve_artist_mbid,
)
from app.metadata_providers import musicbrainz
from app.models import (
    Album,
    Artist,
    MediaItem,
    MediaKind,
    TrackArtist,
    User,
    WantListItem,
    utcnow,
)
from app.owned_match import match_owned, normalize_title
from app.provider_queue import yield_to_user_jobs
from app.want_metadata import fetch_entry_in_background, revalidate_artist

logger = logging.getLogger(__name__)

KINDS = ("artist", "album", "track")
SEARCH_TTL = timedelta(days=1)


def _entry_key(entry: WantListItem) -> tuple:
    return (entry.kind, normalize_artist_name(entry.artist), normalize_title(entry.title))


# --- matching ----------------------------------------------------------------


def _match_album_without_tracklist(
    db: Session, entry: WantListItem, accessible: Collection[int]
) -> bool:
    """Refresh the owned/total counts of an album entry whose tracklist is
    not known: the best signal is an owned album with the same release group
    (or owner and title). Returns whether they changed."""
    before = (entry.owned_count, entry.total_count)
    album = None
    if entry.release_group_mbid:
        album = db.execute(
            select(Album).where(Album.release_group_mbid == entry.release_group_mbid)
        ).scalars().first()
    if album is None:
        album = db.execute(
            select(Album).where(
                Album.owner_key == normalize_artist_name(entry.artist),
                Album.title_key == normalize_artist_name(entry.title),
            )
        ).scalar_one_or_none()
    visible = album is not None and db.execute(
        select(MediaItem.id).where(
            MediaItem.album_id == album.id, MediaItem.library_root_id.in_(accessible)
        ).limit(1)
    ).first() is not None
    entry.owned_count = 1 if visible else 0
    entry.total_count = 1 if visible else None
    return (entry.owned_count, entry.total_count) != before


# What each user's entries were last matched against: (library version,
# accessible roots). Every change that could alter a match - a scan, tag
# edit, enrichment run or media delete - bumps the library version, and an
# access change alters the roots; with neither, matching again finds what it
# found last time. So reads re-match only when one of them moved.
_matched_against: dict[int, tuple[int, frozenset[int]]] = {}


def _tracklist_arrived(db: Session, entries: list[WantListItem]) -> bool:
    """Whether an album entry still counted without its tracklist has one
    now (fetched in the background since the last match)."""
    return any(
        entry.kind == "album"
        and entry.total_count is None
        and entry.release_group_mbid
        and cached_tracklist(db, entry.release_group_mbid) is not None
        for entry in entries
    )


def _match_user(db: Session, entries: list[WantListItem], roots: set[int]) -> int:
    """Match one user's entries. Every unmatched song and every album's
    tracklist go to match_owned in one call - a call per entry would scan
    the library twice each. Returns how many entries changed."""
    changed = 0
    linked = [e for e in entries if e.kind == "track" and e.media_item_id is not None]
    root_of = dict(
        db.execute(
            select(MediaItem.id, MediaItem.library_root_id).where(
                MediaItem.id.in_({e.media_item_id for e in linked})
            )
        ).all()
    ) if linked else {}
    for entry in linked:
        if root_of.get(entry.media_item_id or 0) not in roots:
            entry.media_item_id = None
            changed += 1
    songs = [e for e in entries if e.kind == "track" and e.media_item_id is None] if roots else []
    queries = [{"artist": e.artist, "title": e.title, "mbid": e.recording_mbid} for e in songs]
    albums: list[tuple[WantListItem, int, int]] = []  # entry, first query, query count
    for entry in entries:
        if entry.kind != "album":
            continue
        listing = cached_tracklist(db, entry.release_group_mbid) if entry.release_group_mbid else None
        if listing and listing["tracks"]:
            albums.append((entry, len(queries), len(listing["tracks"])))
            queries.extend(
                {"artist": t.get("artist") or entry.artist, "title": t["title"], "mbid": t.get("recording_mbid")}
                for t in listing["tracks"]
            )
        else:
            changed += _match_album_without_tracklist(db, entry, roots)
    found = match_owned(db, roots, queries)
    for entry, match in zip(songs, found, strict=False):
        if match is not None:
            entry.media_item_id = match
            changed += 1
    for entry, start, count in albums:
        before = (entry.owned_count, entry.total_count)
        entry.owned_count = sum(1 for media_id in found[start : start + count] if media_id is not None)
        entry.total_count = count
        changed += (entry.owned_count, entry.total_count) != before
    return changed


def _match_is_stale(
    db: Session, user_id: int, token: tuple[int, frozenset[int]], entries: list[WantListItem]
) -> bool:
    return _matched_against.get(user_id) != token or _tracklist_arrived(db, entries)


def match_want_list(
    db: Session, user_ids: Iterable[int] | None = None, *, force: bool = False
) -> int:
    """Link song entries to library tracks (and unlink ones the owner can no
    longer reach), and refresh album entries' owned counts. Limited to
    `user_ids` when given. A user whose library and access are unchanged
    since their last match is skipped unless `force` (a new entry). Returns
    how many entries changed."""
    query = select(WantListItem, User).join(User, User.id == WantListItem.user_id)
    if user_ids is not None:
        query = query.where(WantListItem.user_id.in_(list(user_ids)))
    by_user: dict[int, tuple[User, list[WantListItem]]] = {}
    for entry, user in db.execute(query.order_by(WantListItem.id)).all():
        by_user.setdefault(user.id, (user, []))[1].append(entry)
    # Read before matching: a change committed meanwhile leaves the stored
    # version behind, so the next read matches again.
    version = library_version()
    matched: dict[int, tuple[int, frozenset[int]]] = {}
    changed = 0
    for user_id, (user, entries) in by_user.items():
        roots = accessible_root_ids(db, user)
        token = (version, frozenset(roots))
        if not force and not _match_is_stale(db, user_id, token, entries):
            continue
        changed += _match_user(db, entries, roots)
        matched[user_id] = token
    if changed:
        db.commit()
    _matched_against.update(matched)
    return changed


# A read never matches: after a library or access change it queues one
# background re-match per user and answers with the entries as last matched.
# The next read shows the new state.
_rematch_lock = threading.Lock()
_rematch_threads: dict[int, threading.Thread] = {}


def _rematch(user_id: int, session_factory: Callable[[], Session]) -> None:
    db = session_factory()
    try:
        match_want_list(db, [user_id])
    except Exception:  # a failed match leaves the stored state; the next read retries
        logger.exception("Want-list re-match for user %s failed", user_id)
    finally:
        db.close()
        with _rematch_lock:
            _rematch_threads.pop(user_id, None)


def rematch_in_background(db: Session, user: User, entries: list[WantListItem]) -> bool:
    """Queue a re-match of `user`'s `entries` (all of them) when the library
    version, their accessible roots or a fetched tracklist has moved since
    they were last matched. True when a re-match was queued or is running."""
    if not entries:
        return False
    token = (library_version(), frozenset(accessible_root_ids(db, user)))
    if not _match_is_stale(db, user.id, token, entries):
        return False
    with _rematch_lock:
        if user.id not in _rematch_threads:
            _rematch_threads[user.id] = spawn(
                "want-list-match", _rematch, user.id, SessionLocal
            )
    return True


def rematch_visible_in_background(db: Session, user: User, *, all_users: bool = False) -> bool:
    """`rematch_in_background` for every user whose entries a want-list page
    shows (all of them in an admin's all-users view). True when any
    re-match was queued or is running."""
    by_user: dict[int, list[WantListItem]] = {}
    for entry in _entries(db, user, all_users):
        by_user.setdefault(entry.user_id, []).append(entry)
    users = {u.id: u for u in db.scalars(select(User).where(User.id.in_(list(by_user))))} if by_user else {}
    queued = [rematch_in_background(db, users[user_id], entries) for user_id, entries in by_user.items() if user_id in users]
    return any(queued)


def rematch_running(db: Session, user: User, *, all_users: bool = False) -> bool:
    """Whether a background re-match of what the user's want-list pages show
    is queued or running (for /api/want-list/match-status)."""
    with _rematch_lock:
        running = set(_rematch_threads)
    if not running:
        return False
    if all_users and user.is_admin:
        return True
    return user.id in running


def wait_for_rematches(timeout: float = 5.0) -> None:
    """Wait for the queued background re-matches to end (for tests; app
    shutdown waits for them through app.background.join_all)."""
    with _rematch_lock:
        threads = list(_rematch_threads.values())
    for thread in threads:
        thread.join(timeout)


# --- adding and removing -------------------------------------------------------


def add_entry(db: Session, user: User, values: dict) -> WantListItem:
    """Add (or return the existing) entry. Wanting something that was
    excluded from a wanted artist un-excludes it; `excluded=True` adds the
    exclusion instead."""
    kind = values["kind"]
    artist = values["artist"].strip()
    if kind == "artist" and not values.get("artist_mbid"):
        # Wanted by name (a similar artist, say): find their MusicBrainz
        # artist so the live discography works.
        values = {**values, "artist_mbid": _artist_mbid_for_name(db, artist)}
    title = (values.get("title") or artist).strip()
    probe = WantListItem(kind=kind, artist=artist, title=title)
    existing = None
    for entry in db.scalars(
        select(WantListItem).where(WantListItem.user_id == user.id, WantListItem.kind == kind)
    ):
        same_mbid = (
            (kind == "artist" and values.get("artist_mbid") and entry.artist_mbid == values.get("artist_mbid"))
            or (kind == "album" and values.get("release_group_mbid") and entry.release_group_mbid == values.get("release_group_mbid"))
            or (kind == "track" and values.get("recording_mbid") and entry.recording_mbid == values.get("recording_mbid"))
        )
        if same_mbid or _entry_key(entry) == _entry_key(probe):
            existing = entry
            break
    entry = existing or WantListItem(user_id=user.id, kind=kind, artist=artist, title=title)
    entry.excluded = bool(values.get("excluded", False))
    for field in ("album", "station_name", "artist_mbid", "release_group_mbid", "recording_mbid", "release_type"):
        if values.get(field):
            setattr(entry, field, values[field])
    if existing is None:
        db.add(entry)
    db.commit()
    match_want_list(db, [user.id], force=True)
    db.refresh(entry)
    if not entry.excluded:
        # Art and every info lookup for it, so its pages open fully populated.
        fetch_entry_in_background(entry.id, user_id=user.id)
    if kind == "artist" and entry.artist_mbid and not entry.excluded:
        # Wanting a whole artist: learn every album's songs in the background.
        prefetch_tracklists_in_background(entry.artist_mbid)
    if kind == "album" and entry.release_group_mbid and entry.total_count is None:
        if tracklist(db, entry.release_group_mbid) is not None:
            match_want_list(db, [user.id], force=True)
            db.refresh(entry)
    return entry


def _artist_mbid_for_name(db: Session, name: str) -> str | None:
    artist = find_artist(db, name)
    if artist is not None:
        return resolve_artist_mbid(db, artist)
    exact = [
        c for c in artist_candidates(db, name) if normalize_artist_name(c["name"] or "") == normalize_artist_name(name)
    ]
    # Only a clear match: several same-named artists stay unresolved.
    if len(exact) == 1 or (len(exact) > 1 and (exact[0].get("score") or 0) - (exact[1].get("score") or 0) >= 5):
        return exact[0]["mbid"]
    return None


def clear_owned(db: Session, user: User) -> int:
    """Remove the user's songs and albums that are now fully in the library."""
    match_want_list(db, [user.id])
    removed = 0
    for entry in db.scalars(
        select(WantListItem).where(WantListItem.user_id == user.id, WantListItem.excluded.is_(False))
    ):
        if entry_owned(entry):
            db.delete(entry)
            removed += 1
    db.commit()
    return removed


def entry_owned(entry: WantListItem) -> bool:
    if entry.kind == "track":
        return entry.media_item_id is not None
    if entry.kind == "album":
        return bool(entry.total_count) and entry.owned_count == entry.total_count
    return False


# --- views ---------------------------------------------------------------------


def entry_out(entry: WantListItem, user_names: dict[int, str] | None = None) -> dict:
    out = {
        "id": entry.id,
        "kind": entry.kind,
        "artist": entry.artist,
        "title": entry.title,
        "album": entry.album,
        "station_name": entry.station_name,
        "artist_mbid": entry.artist_mbid,
        "release_group_mbid": entry.release_group_mbid,
        "recording_mbid": entry.recording_mbid,
        "release_type": entry.release_type,
        "excluded": entry.excluded,
        "media_item_id": entry.media_item_id,
        "owned_count": entry.owned_count,
        "total_count": entry.total_count,
        "owned": entry_owned(entry),
        "created_at": entry.created_at.isoformat() if entry.created_at else None,
    }
    if user_names is not None:
        out["user"] = user_names.get(entry.user_id)
    return out


def _entries(db: Session, user: User, all_users: bool) -> list[WantListItem]:
    query = select(WantListItem)
    if not (all_users and user.is_admin):
        query = query.where(WantListItem.user_id == user.id)
    return list(db.scalars(query.order_by(WantListItem.created_at.desc())))


def _user_names(db: Session) -> dict[int, str]:
    return {u.id: u.username for u in db.scalars(select(User))}


@stale_ok
def artist_summaries(db: Session, user: User, *, all_users: bool = False) -> list[dict]:
    """One row per artist the user wants anything by, for the Want list tab
    (excluded-only artists aren't listed). The entries as last matched: the
    caller queues a background re-match (rematch_visible_in_background)."""
    groups: dict[str, dict] = {}
    names = _user_names(db) if all_users and user.is_admin else None
    for entry in _entries(db, user, all_users):
        if entry.excluded:
            continue
        key = entry.artist_mbid or normalize_artist_name(entry.artist)
        row = groups.setdefault(
            key,
            {
                "artist": entry.artist,
                "artist_mbid": entry.artist_mbid,
                "wants_artist": False,
                "album_count": 0,
                "track_count": 0,
                "owned_count": 0,
                "users": set(),
                "latest": entry.created_at,
            },
        )
        row["artist_mbid"] = row["artist_mbid"] or entry.artist_mbid
        if entry.kind == "artist":
            row["wants_artist"] = True
            row["artist"] = entry.artist
        elif entry.kind == "album":
            row["album_count"] += 1
        else:
            row["track_count"] += 1
        if entry_owned(entry):
            row["owned_count"] += 1
        if names is not None:
            row["users"].add(names.get(entry.user_id))
        if entry.created_at and (row["latest"] is None or entry.created_at > row["latest"]):
            row["latest"] = entry.created_at
    found = _library_artists(db, user, [(row["artist"], row["artist_mbid"]) for row in groups.values()])
    out = []
    for row in groups.values():
        artist, representative = found.get((row["artist"], row["artist_mbid"]), (None, None))
        out.append(
            {
                **{k: v for k, v in row.items() if k not in ("users", "latest")},
                "users": sorted(u for u in row["users"] if u) if names is not None else None,
                "in_library": artist is not None,
                "library_name": artist.name if artist else None,
                "representative_media_item_id": representative,
                "updated_at": row["latest"].isoformat() if row["latest"] else None,
            }
        )
    out.sort(key=lambda r: r["artist"].casefold())
    return out


def _library_artists(
    db: Session, user: User, wanted: list[tuple[str, str | None]]
) -> dict[tuple[str, str | None], tuple[Artist | None, int | None]]:
    """app.catalog.library_artist for many (name, MBID) pairs in three
    queries, with each found artist's representative track (its lowest
    accessible media id): {(name, mbid): (artist or None, media id or None)}."""
    if not wanted:
        return {}
    mbids = {mbid for _name, mbid in wanted if mbid}
    keys = {normalize_artist_name(name) for name, _mbid in wanted if name}
    by_mbid: dict[str, Artist] = {}
    if mbids:
        # The lowest id per MBID, as library_artist's first() picks.
        for artist in db.scalars(select(Artist).where(Artist.mbid.in_(mbids)).order_by(Artist.id.desc())):
            if artist.mbid is not None:
                by_mbid[artist.mbid] = artist
    by_key = {
        artist.normalized_name: artist
        for artist in db.scalars(select(Artist).where(Artist.normalized_name.in_(keys)))
    } if keys else {}
    chosen: dict[tuple[str, str | None], Artist | None] = {}
    for name, mbid in wanted:
        artist = by_mbid.get(mbid) if mbid else None
        if artist is None and name:
            artist = by_key.get(normalize_artist_name(name))
        chosen[(name, mbid)] = artist
    ids = {artist.id for artist in chosen.values() if artist is not None}
    seen: dict[int, tuple[bool, int]] = {}
    if ids:
        for artist_id, has_track, first_id in db.execute(
            select(
                TrackArtist.artist_id,
                func.max(case((MediaItem.kind == MediaKind.track, 1), else_=0)),
                func.min(MediaItem.id),
            )
            .join(MediaItem, MediaItem.id == TrackArtist.media_item_id)
            .where(
                TrackArtist.artist_id.in_(ids),
                MediaItem.library_root_id.in_(accessible_root_ids(db, user)),
            )
            .group_by(TrackArtist.artist_id)
        ):
            seen[artist_id] = (bool(has_track), first_id)
    out: dict[tuple[str, str | None], tuple[Artist | None, int | None]] = {}
    for pair, artist in chosen.items():
        visible = artist is not None and seen.get(artist.id, (False, None))[0]
        out[pair] = (artist, seen[artist.id][1]) if visible and artist is not None else (None, None)
    return out


@stale_ok
def artist_detail(
    db: Session, user: User, name: str, mbid: str | None, *, all_users: bool = False
) -> dict:
    """Everything wanted by one artist: the artist entry (with how much of
    their catalog is still missing, when it's known), albums and songs.
    The entries as last matched: the caller queues a background re-match
    (rematch_visible_in_background)."""
    key = normalize_artist_name(name)
    names = _user_names(db) if all_users and user.is_admin else None
    entries = [
        e
        for e in _entries(db, user, all_users)
        if (mbid and e.artist_mbid == mbid) or normalize_artist_name(e.artist) == key
    ]
    artist_entry = next((e for e in entries if e.kind == "artist"), None)
    mbid = mbid or next((e.artist_mbid for e in entries if e.artist_mbid), None)
    missing = None
    refreshing = False
    if artist_entry is not None and mbid:
        catalog = discography(db, user, mbid, name)
        if catalog is not None:
            refreshing = revalidate_artist(db, mbid, name, user_id=user.id)
            excluded = {e.release_group_mbid for e in entries if e.kind == "album" and e.excluded}
            missing = [
                {k: g[k] for k in ("mbid", "title", "primary_type", "section", "first_release_date", "status", "owned_count", "total_count")}
                for g in catalog["release_groups"]
                if g["status"] != "owned" and g["mbid"] not in excluded
            ]
    return {
        "artist": artist_entry.artist if artist_entry else name,
        "artist_mbid": mbid,
        "artist_entry": entry_out(artist_entry, names) if artist_entry else None,
        "missing_release_groups": missing,
        "refreshing": refreshing,
        "albums": [entry_out(e, names) for e in entries if e.kind == "album"],
        "tracks": [entry_out(e, names) for e in entries if e.kind == "track"],
        "in_library": library_artist(db, user, name, mbid) is not None,
    }


def wanted_index(db: Session, user: User) -> dict:
    """What the user wants, for marking catalog/search rows: sets of artist
    MBIDs, release group MBIDs and recording MBIDs, plus name keys, and the
    exclusions."""
    index = {"artists": set(), "groups": set(), "recordings": set(), "names": set(), "excluded_groups": set(), "excluded_recordings": set(), "entry_ids": {}}
    for entry in db.scalars(select(WantListItem).where(WantListItem.user_id == user.id)):
        if entry.excluded:
            if entry.release_group_mbid:
                index["excluded_groups"].add(entry.release_group_mbid)
            if entry.recording_mbid:
                index["excluded_recordings"].add(entry.recording_mbid)
            continue
        if entry.kind == "artist":
            if entry.artist_mbid:
                index["artists"].add(entry.artist_mbid)
            index["names"].add(("artist", normalize_artist_name(entry.artist), ""))
        elif entry.kind == "album":
            if entry.release_group_mbid:
                index["groups"].add(entry.release_group_mbid)
                index["entry_ids"][("album", entry.release_group_mbid)] = entry.id
        else:
            if entry.recording_mbid:
                index["recordings"].add(entry.recording_mbid)
                index["entry_ids"][("track", entry.recording_mbid)] = entry.id
        index["names"].add(_entry_key(entry))
    return index


# --- the catalog with want state, for catalog pages ---------------------------


@stale_ok
def catalog_artist(db: Session, user: User, mbid: str, name: str | None, *, refresh: bool = False) -> dict | None:
    # What is saved is shown at once; "Refresh" re-checks in the background
    # (a first-ever look has nothing saved, so it does have to fetch).
    force = refresh and catalog_fetched_at(db, mbid) is not None
    catalog = discography(db, user, mbid, name, refresh=refresh and not force)
    if catalog is None:
        return None
    catalog["refreshing"] = revalidate_artist(db, mbid, name, force=force, user_id=user.id)
    index = wanted_index(db, user)
    artist_wanted = mbid in index["artists"]
    for group in catalog["release_groups"]:
        group["wanted"] = group["mbid"] in index["groups"] or (
            artist_wanted and group["mbid"] not in index["excluded_groups"] and group["status"] != "owned"
        )
        group["excluded"] = group["mbid"] in index["excluded_groups"]
        group["want_entry_id"] = index["entry_ids"].get(("album", group["mbid"]))
    catalog["artist_wanted"] = artist_wanted
    catalog["artist_name"] = catalog["artist"] or name
    return catalog


@stale_ok
def catalog_release_group(db: Session, user: User, mbid: str, artist_name: str | None, *, refresh: bool = False) -> dict | None:
    group = mb_release_group(db, mbid, refresh=refresh)
    if not group:
        return None
    credit = (group.get("artist-credit") or [{}])[0]
    artist = artist_name or credit.get("name") or (credit.get("artist") or {}).get("name") or ""
    artist_mbid = (credit.get("artist") or {}).get("id")
    album = db.execute(select(Album).where(Album.release_group_mbid == mbid)).scalars().first()
    listing = tracklist(db, mbid, album.release_mbid if album else None, refresh=refresh) or {"tracks": [], "release_mbid": None}
    owned = owned_tracks(db, user, artist, listing["tracks"])
    index = wanted_index(db, user)
    group_wanted = mbid in index["groups"] or (artist_mbid in index["artists"] and mbid not in index["excluded_groups"])
    tracks = []
    for track, media_id in zip(listing["tracks"], owned, strict=False):
        recording = track.get("recording_mbid")
        tracks.append(
            {
                **track,
                "media_item_id": media_id,
                "wanted": bool(recording and recording in index["recordings"]) or (group_wanted and media_id is None),
                "want_entry_id": index["entry_ids"].get(("track", recording)) if recording else None,
            }
        )
    secondary = [str(t) for t in group.get("secondary-types") or []]
    return {
        "mbid": mbid,
        "title": group.get("title"),
        "artist": artist,
        "artist_mbid": artist_mbid,
        "primary_type": group.get("primary-type"),
        "secondary_types": secondary,
        "section": section_for(group.get("primary-type"), secondary),
        "first_release_date": group.get("first-release-date"),
        "release_mbid": listing.get("release_mbid"),
        "tracks": tracks,
        "owned_count": sum(1 for t in tracks if t["media_item_id"]),
        "wanted": mbid in index["groups"],
        "want_entry_id": index["entry_ids"].get(("album", mbid)),
        "artist_wanted": artist_mbid in index["artists"],
        "excluded": mbid in index["excluded_groups"],
        "owned_album": {"artist": album.owner, "album": album.title} if album else None,
    }


def catalog_artist_placeholder(db: Session, user: User, mbid: str, name: str | None) -> dict:
    """A catalog page with nothing saved yet, while its first look-up runs
    in the background: no release groups, `refreshing` (and `pending`)."""
    artist = library_artist(db, user, name, mbid)
    return {
        "artist": artist.name if artist else name,
        "artist_mbid": mbid,
        "in_library": artist is not None,
        "release_groups": [],
        "tracklists_known": 0,
        "tracklists_total": 0,
        "refreshing": True,
        "artist_wanted": mbid in wanted_index(db, user)["artists"],
        "artist_name": artist.name if artist else name,
    }


def catalog_release_group_placeholder(db: Session, user: User, mbid: str, artist_name: str | None) -> dict:
    """A catalog album with nothing saved yet, while its first look-up runs
    in the background: no title or tracks, the want state as far as known."""
    album = db.execute(select(Album).where(Album.release_group_mbid == mbid)).scalars().first()
    index = wanted_index(db, user)
    return {
        "mbid": mbid,
        "title": album.title if album else None,
        "artist": artist_name or (album.owner if album else None) or "",
        "artist_mbid": None,
        "primary_type": None,
        "secondary_types": [],
        "section": section_for(None, []),
        "first_release_date": None,
        "release_mbid": None,
        "tracks": [],
        "owned_count": 0,
        "wanted": mbid in index["groups"],
        "want_entry_id": index["entry_ids"].get(("album", mbid)),
        "artist_wanted": False,
        "excluded": mbid in index["excluded_groups"],
        "owned_album": {"artist": album.owner, "album": album.title} if album else None,
    }


# --- search ----------------------------------------------------------------------


def _year(date: str | None) -> str | None:
    return date[:4] if date and date[:4].isdigit() else None


def search(db: Session, user: User, q: str) -> dict:
    """Artists, albums and songs from MusicBrainz for `q` - things the user
    may not own - each marked in-library/wanted. Three requests (about three
    seconds, MusicBrainz's rate limit), cached for a day."""
    q = q.strip()
    if not q:
        return {"artists": [], "albums": [], "tracks": []}
    key = q.casefold()

    def run(fetch, field):
        def go():
            result = fetch(q, 15)
            if result.error:
                return None
            return (result.value or {}).get(field) or []

        return cached(db, "musicbrainz", f"search-{field}", key, go, ttl=SEARCH_TTL) or []

    artist_rows = run(musicbrainz.search_artists, "artists")
    group_rows = run(musicbrainz.search_release_groups, "release-groups")
    recording_rows = run(musicbrainz.search_recordings, "recordings")
    index = wanted_index(db, user)
    accessible = accessible_root_ids(db, user)

    artists = []
    for row in artist_rows:
        if not row.get("id"):
            continue
        in_library = library_artist(db, user, row.get("name"), row["id"])
        artists.append(
            {
                "name": row.get("name"),
                "mbid": row["id"],
                "disambiguation": row.get("disambiguation") or None,
                "type": row.get("type"),
                "country": row.get("country"),
                "score": row.get("score"),
                "in_library": in_library is not None,
                "library_name": in_library.name if in_library else None,
                "wanted": row["id"] in index["artists"],
            }
        )

    albums = []
    for row in group_rows:
        if not row.get("id"):
            continue
        credit = (row.get("artist-credit") or [{}])[0]
        artist_name = credit.get("name") or (credit.get("artist") or {}).get("name") or ""
        secondary = [str(t) for t in row.get("secondary-types") or []]
        owned = db.execute(select(Album.owner, Album.title).where(Album.release_group_mbid == row["id"])).first()
        albums.append(
            {
                "title": row.get("title"),
                "artist": artist_name,
                "artist_mbid": (credit.get("artist") or {}).get("id"),
                "release_group_mbid": row["id"],
                "primary_type": row.get("primary-type"),
                "section": section_for(row.get("primary-type"), secondary),
                "year": _year(row.get("first-release-date")),
                "in_library": owned is not None,
                "owned_album": {"artist": owned[0], "album": owned[1]} if owned else None,
                "wanted": row["id"] in index["groups"],
            }
        )

    track_queries = []
    tracks = []
    for row in recording_rows:
        if not row.get("id"):
            continue
        credit = (row.get("artist-credit") or [{}])[0]
        artist_name = credit.get("name") or (credit.get("artist") or {}).get("name") or ""
        release = next((r for r in row.get("releases") or [] if isinstance(r, dict)), {})
        group = release.get("release-group") or {}
        length = row.get("length")
        tracks.append(
            {
                "title": row.get("title"),
                "artist": artist_name,
                "artist_mbid": (credit.get("artist") or {}).get("id"),
                "recording_mbid": row["id"],
                "album": release.get("title"),
                "release_group_mbid": group.get("id"),
                "year": _year(row.get("first-release-date") or release.get("date")),
                "length_seconds": round(length / 1000) if isinstance(length, int) else None,
                "wanted": row["id"] in index["recordings"],
            }
        )
        track_queries.append({"artist": artist_name, "title": row.get("title"), "mbid": row["id"]})
    for track, media_id in zip(tracks, match_owned(db, accessible, track_queries), strict=False):
        track["media_item_id"] = media_id
        track["in_library"] = media_id is not None
    # MusicBrainz lists every recording (live takes, remasters) separately;
    # one row per artist + title is plenty for picking what to want.
    seen, unique_tracks = set(), []
    for track in tracks:
        name_key = (normalize_artist_name(track["artist"]), normalize_title(track["title"]))
        if name_key in seen:
            continue
        seen.add(name_key)
        unique_tracks.append(track)
    return {"artists": artists, "albums": albums, "tracks": unique_tracks}


# --- export and refresh -----------------------------------------------------------

EXPORT_FIELDS = ("kind", "artist", "title", "album", "release_type", "artist_mbid", "release_group_mbid", "recording_mbid", "owned", "owned_count", "total_count", "excluded", "station_name", "created_at")


_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _csv_safe(value: object) -> object:
    """Spreadsheets run a cell that starts with one of these as a formula, and
    titles and artist names come from tags, MusicBrainz and other users: a
    leading single quote makes the cell plain text."""
    if isinstance(value, str) and value.startswith(_FORMULA_PREFIXES):
        return "'" + value
    return value


def export(db: Session, user: User, fmt: str, *, all_users: bool = False) -> tuple[str, str]:
    """(body, media type) of the user's want list as CSV or JSON."""
    match_want_list(db, None if all_users and user.is_admin else [user.id])
    names = _user_names(db) if all_users and user.is_admin else None
    rows = [entry_out(e, names) for e in _entries(db, user, all_users)]
    fields = (*EXPORT_FIELDS, "user") if names is not None else EXPORT_FIELDS
    if fmt == "json":
        return json.dumps([{k: r.get(k) for k in fields} for r in rows], indent=2, ensure_ascii=False), "application/json"
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(fields), extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({key: _csv_safe(value) for key, value in row.items()})
    return buffer.getvalue(), "text/csv"


def refresh_wanted_catalogs(db: Session, limit: int = 20) -> int:
    """Re-browse wanted artists whose catalog is older than WANTED_REFRESH,
    so their new releases appear. Bounded per run (each artist is one or
    two MusicBrainz requests)."""
    mbids = {
        mbid
        for mbid in db.scalars(
            select(WantListItem.artist_mbid).where(
                WantListItem.kind == "artist",
                WantListItem.excluded.is_(False),
                WantListItem.artist_mbid.is_not(None),
            )
        )
        if mbid is not None  # excluded in SQL; tells the type checker
    }
    refreshed = 0
    now = utcnow().replace(tzinfo=None)
    for mbid in sorted(mbids):
        fetched = catalog_fetched_at(db, mbid)
        if fetched is not None and now - fetched.replace(tzinfo=None) < WANTED_REFRESH:
            continue
        if release_groups(db, mbid, refresh=True) is not None:
            refreshed += 1
        if refreshed >= limit:
            break
        # A scheduled job on the provider queue: user-triggered jobs that
        # are waiting run before the next artist.
        yield_to_user_jobs()
    return refreshed
