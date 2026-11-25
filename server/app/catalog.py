"""Artist discographies from MusicBrainz, and what the library owns of them.

The catalog is MusicBrainz's release groups (complete and typed). Listing an
artist's release groups is one or two requests; a tracklist is one more per
album, fetched when that album is opened or wanted and cached server-wide.

Ownership is judged by recording, not release: a single whose song the
library has on the album counts as owned. Until a release group's
tracklist is known, a single is matched by its title against the artist's
owned songs and an album by its release group (or title) against owned
albums.
"""

import logging
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import provider_queue
from app.access import accessible_root_ids
from app.albums import _date, section_for
from app.artist_credits import normalize_artist_name
from app.db import SessionLocal
from app.external_cache import cached, peek, store
from app.metadata_info import mb_release, mb_release_group
from app.metadata_providers import musicbrainz
from app.models import Album, Artist, MediaItem, MediaKind, TrackArtist, User
from app.owned_match import match_owned, normalize_title

logger = logging.getLogger(__name__)

CATALOG_TTL = timedelta(days=30)
# Wanted artists are re-browsed this often so new releases show up.
WANTED_REFRESH = timedelta(days=7)


def release_groups(
    db: Session, artist_mbid: str, *, refresh: bool = False, allow_fetch: bool = True
) -> list[dict] | None:
    """The artist's release groups, or None when MusicBrainz failed and
    nothing is cached."""

    def fetch():
        result = musicbrainz.browse_release_groups(artist_mbid)
        if result.error:
            return None
        return [_group_out(row) for row in (result.value or {}).get("release-groups") or []]

    return cached(
        db,
        "musicbrainz",
        "artist-release-groups",
        artist_mbid,
        fetch,
        ttl=CATALOG_TTL,
        refresh=refresh,
        allow_fetch=allow_fetch,
    )


def _group_out(row: dict) -> dict:
    secondary = [str(t) for t in row.get("secondary-types") or []]
    return {
        "mbid": row.get("id"),
        "title": row.get("title") or "",
        "primary_type": row.get("primary-type"),
        "secondary_types": secondary,
        "section": section_for(row.get("primary-type"), secondary),
        "first_release_date": _date(row.get("first-release-date")),
        "disambiguation": row.get("disambiguation") or None,
    }


def catalog_fetched_at(db: Session, artist_mbid: str) -> datetime | None:
    found = peek(db, "musicbrainz", "artist-release-groups", artist_mbid)
    return found[1] if found else None


# --- tracklists -----------------------------------------------------------------


def _pick_release(group: dict, preferred: str | None) -> str | None:
    """The edition whose tracklist stands for the album: the one the library
    matched (or a user chose), else the earliest official release."""
    releases = [r for r in group.get("releases") or [] if isinstance(r, dict) and r.get("id")]
    if preferred and any(r["id"] == preferred for r in releases):
        return preferred
    official = [r for r in releases if r.get("status") == "Official"] or releases
    official.sort(key=lambda r: (r.get("date") or "9999", r.get("country") != "XW", r["id"]))
    return official[0]["id"] if official else None


def tracklist(
    db: Session, release_group_mbid: str, preferred_release: str | None = None, *, refresh: bool = False
) -> dict | None:
    """{"release_mbid", "title", "date", "tracks": [{"disc", "position",
    "title", "recording_mbid", "length_seconds", "artist"}]} - None when
    MusicBrainz couldn't be reached."""
    group = mb_release_group(db, release_group_mbid, refresh=refresh)
    if not group:
        return None
    release_mbid = _pick_release(group, preferred_release)
    if not release_mbid:
        return {"release_mbid": None, "title": group.get("title"), "date": None, "tracks": []}
    release = mb_release(db, release_mbid, refresh=refresh)
    if not release:
        return None
    tracks = []
    for disc_index, medium in enumerate(release.get("media") or [], start=1):
        if not isinstance(medium, dict):
            continue
        for track in medium.get("tracks") or []:
            if not isinstance(track, dict):
                continue
            recording = track.get("recording") or {}
            credit = (track.get("artist-credit") or recording.get("artist-credit") or [{}])[0]
            length = track.get("length") or recording.get("length")
            tracks.append(
                {
                    "disc": medium.get("position") or disc_index,
                    "position": track.get("position") or len(tracks) + 1,
                    "title": track.get("title") or recording.get("title") or "",
                    "recording_mbid": recording.get("id"),
                    "length_seconds": round(length / 1000) if isinstance(length, int) else None,
                    "artist": credit.get("name") or (credit.get("artist") or {}).get("name"),
                }
            )
    listing = {
        "release_mbid": release_mbid,
        "title": release.get("title") or group.get("title"),
        "date": _date(release.get("date")),
        "tracks": tracks,
    }
    if tracks:
        store(db, "catalog", "tracklist", _listing_key(release_group_mbid, preferred_release), listing)
    return listing


def _listing_key(release_group_mbid: str, preferred_release: str | None) -> str:
    return f"{release_group_mbid}:{preferred_release or ''}"


def cached_tracklist(db: Session, release_group_mbid: str, preferred_release: str | None = None) -> dict | None:
    """A tracklist only if it is already saved (no request). The tracklist
    itself is saved in compact form the first time it is built, so showing
    a discography doesn't decode two large MusicBrainz payloads per album."""
    derived = peek(db, "catalog", "tracklist", _listing_key(release_group_mbid, preferred_release))
    if derived and derived[0]:
        return derived[0]
    group = peek(db, "musicbrainz", "release-group", release_group_mbid)
    if not group or not group[0]:
        return None
    release_mbid = _pick_release(group[0], preferred_release)
    if not release_mbid or not peek(db, "musicbrainz", "release", release_mbid):
        return None
    return tracklist(db, release_group_mbid, preferred_release)


def owned_tracks(db: Session, user: User, artist_name: str, tracks: list[dict]) -> list[int | None]:
    """The owned library track (id) for each tracklist entry, if any."""
    accessible = accessible_root_ids(db, user)
    return match_owned(
        db,
        accessible,
        ({"artist": t.get("artist") or artist_name, "title": t["title"], "mbid": t.get("recording_mbid")} for t in tracks),
    )


# At most this many of an artist's release groups get their tracklists and
# covers prefetched (here and in app.want_metadata).
MAX_GROUP_COVERS = 150


def prefetch_tracklists_in_background(artist_mbid: str) -> None:
    """Fetch every release group's tracklist for an artist (wanted whole)
    so its albums' owned/missing songs are known. Runs on the background
    provider queue (one job at a time, behind page requests); an artist
    already queued is skipped."""
    def run():
        db = SessionLocal()
        try:
            groups = release_groups(db, artist_mbid) or []
            if len(groups) > MAX_GROUP_COVERS:
                logger.info(
                    "Tracklist prefetch for artist %s capped at %d of %d release groups",
                    artist_mbid, MAX_GROUP_COVERS, len(groups),
                )
            for group in groups[:MAX_GROUP_COVERS]:
                if group.get("mbid"):
                    tracklist(db, group["mbid"])
        except Exception:  # background warm-up only
            logger.exception("Tracklist prefetch failed for artist %s", artist_mbid)
        finally:
            db.close()

    provider_queue.submit(f"tracklists:{artist_mbid}", run)


def prefetch_progress(db: Session, artist_mbid: str) -> tuple[int, int]:
    """(release groups with a cached tracklist, release groups)."""
    groups = release_groups(db, artist_mbid, allow_fetch=False) or []
    done = sum(
        1 for group in groups if group.get("mbid") and cached_tracklist(db, group["mbid"]) is not None
    )
    return done, len(groups)


# --- ownership of a discography ---------------------------------------------------


def library_artist(db: Session, user: User, name: str | None, mbid: str | None) -> Artist | None:
    """The library's artist for a catalog artist: by MBID, else by name -
    only if the user can see any of its tracks."""
    query = select(Artist)
    if mbid:
        artist = db.execute(query.where(Artist.mbid == mbid)).scalars().first()
    else:
        artist = None
    if artist is None and name:
        artist = db.execute(
            query.where(Artist.normalized_name == normalize_artist_name(name))
        ).scalar_one_or_none()
    if artist is None:
        return None
    visible = db.execute(
        select(MediaItem.id)
        .join(TrackArtist, TrackArtist.media_item_id == MediaItem.id)
        .where(
            TrackArtist.artist_id == artist.id,
            MediaItem.library_root_id.in_(accessible_root_ids(db, user)),
            MediaItem.kind == MediaKind.track,
        )
        .limit(1)
    ).first()
    return artist if visible else None


def _owned_index(db: Session, user: User, artist: Artist | None) -> dict:
    """What the user owns by this artist: albums (by release group and by
    title) and song titles - the cheap signals used before a tracklist is
    known."""
    index = {"groups": {}, "album_titles": {}, "song_titles": set()}
    if artist is None:
        return index
    accessible = accessible_root_ids(db, user)
    rows = db.execute(
        select(MediaItem.title, MediaItem.album, Album.release_group_mbid, Album.owner, Album.title, TrackArtist.role)
        .join(TrackArtist, TrackArtist.media_item_id == MediaItem.id)
        .outerjoin(Album, Album.id == MediaItem.album_id)
        .where(
            TrackArtist.artist_id == artist.id,
            MediaItem.library_root_id.in_(accessible),
            MediaItem.kind == MediaKind.track,
        )
    ).all()
    for title, _album, group_mbid, owner, album_title, role in rows:
        index["song_titles"].add(normalize_title(title))
        if album_title and role == "album_artist":
            ref = {"artist": owner, "album": album_title}
            if group_mbid:
                index["groups"][group_mbid] = ref
            index["album_titles"][normalize_title(album_title)] = ref
    return index


def group_status(
    db: Session,
    user: User,
    artist_name: str,
    group: dict,
    index: dict,
    *,
    listing: dict | None = None,
    owned: list[int | None] | None = None,
) -> dict:
    """{"status": "owned"|"partial"|"missing", "owned_count", "total_count",
    "owned_album"} for one release group. `listing` and `owned` may be
    passed in when the caller already matched many groups in one batch."""
    owned_album = index["groups"].get(group["mbid"]) or index["album_titles"].get(normalize_title(group["title"]))
    if listing is None:
        listing = cached_tracklist(db, group["mbid"])
    if listing and listing["tracks"]:
        if owned is None:
            owned = owned_tracks(db, user, artist_name, listing["tracks"])
        count = sum(1 for media_id in owned if media_id is not None)
        total = len(listing["tracks"])
        status = "owned" if count == total else "partial" if count or owned_album else "missing"
        return {"status": status, "owned_count": count, "total_count": total, "owned_album": owned_album}
    if owned_album:
        return {"status": "owned", "owned_count": None, "total_count": None, "owned_album": owned_album}
    if group["primary_type"] == "Single" and normalize_title(group["title"]) in index["song_titles"]:
        return {"status": "owned", "owned_count": None, "total_count": None, "owned_album": None}
    return {"status": "missing", "owned_count": 0 if listing else None, "total_count": None, "owned_album": None}


def discography(
    db: Session, user: User, artist_mbid: str, name: str | None, *, refresh: bool = False
) -> dict | None:
    """The catalog page for an artist: every release group with its owned
    state. None when MusicBrainz couldn't be reached and nothing is cached."""
    groups = release_groups(db, artist_mbid, refresh=refresh)
    if groups is None:
        return None
    artist = library_artist(db, user, name, artist_mbid)
    display = artist.name if artist else name
    index = _owned_index(db, user, artist)
    valid = [g for g in groups if g.get("mbid")]
    listings = {g["mbid"]: cached_tracklist(db, g["mbid"]) for g in valid}
    # Match every album's songs against the library in one batch (three
    # queries in all) instead of per album.
    flat = [t for g in valid for t in ((listings[g["mbid"]] or {}).get("tracks") or [])]
    flat_owned = owned_tracks(db, user, display or "", flat) if flat else []
    out = []
    cursor = 0
    for group in valid:
        listing = listings[group["mbid"]]
        count = len((listing or {}).get("tracks") or [])
        owned = flat_owned[cursor : cursor + count]
        cursor += count
        out.append({**group, **group_status(db, user, display or "", group, index, listing=listing, owned=owned)})
    out.sort(key=lambda g: (g["first_release_date"] or "9999", g["title"].casefold()), reverse=True)
    done, total = prefetch_progress(db, artist_mbid)
    return {
        "artist": display,
        "artist_mbid": artist_mbid,
        "in_library": artist is not None,
        "release_groups": out,
        "tracklists_known": done,
        "tracklists_total": total,
    }
