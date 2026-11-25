"""Album identity and release details.

An album is (owner, title) - the owner being the album artist tag, else the
track artist, exactly as app.artist_credits.album_owner_expr shows it - with
both compared case/accent-insensitively. Its release details come from the
MusicBrainz recordings already cached on its tracks: each track votes for
the release (and so release group) whose title matches the album best, and
the most-voted release group wins. No extra MusicBrainz request is needed.
"""

from collections import Counter
from collections.abc import Iterable
from typing import Any, cast

from sqlalchemy import CursorResult, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.artist_credits import normalize_artist_name
from app.models import Album, Artist, MediaItem, MediaKind, TrackArtist

# Primary/secondary type names as MusicBrainz spells them.
SECTION_ORDER = ("Album", "EP", "Single", "Live", "Compilation", "Other")


def album_owner(item: MediaItem) -> str | None:
    """Python twin of app.artist_credits.album_owner_expr."""
    return (item.album_owner or "").strip() or (item.album_artist or "").strip() or item.artist


def _keys(owner: str | None, title: str) -> tuple[str, str]:
    return normalize_artist_name(owner or ""), normalize_artist_name(title)


def get_or_create_album(
    db: Session, owner: str | None, title: str, cache: dict | None = None
) -> Album:
    keys = _keys(owner, title)
    if cache is not None and keys in cache:
        return cache[keys]
    album = db.execute(
        select(Album).where(Album.owner_key == keys[0], Album.title_key == keys[1])
    ).scalar_one_or_none()
    if album is None:
        # A concurrent scan of another root may have just added it.
        try:
            with db.begin_nested():
                album = Album(owner=owner, title=title, owner_key=keys[0], title_key=keys[1])
                db.add(album)
        except IntegrityError:
            album = db.execute(
                select(Album).where(Album.owner_key == keys[0], Album.title_key == keys[1])
            ).scalar_one()
    if cache is not None:
        cache[keys] = album
    return album


def assign_album(db: Session, item: MediaItem, cache: dict | None = None) -> Album | None:
    """Point a track at its Album row (None for untitled albums and videos)."""
    if item.kind != MediaKind.track or not (item.album or "").strip():
        item.album_id = None
        return None
    assert item.album is not None  # a blank/None album returned above
    album = get_or_create_album(db, album_owner(item), item.album, cache)
    item.album_id = album.id
    return album


def _date(value: str | None) -> str | None:
    """MusicBrainz dates ("1999", "1999-05", "1999-05-20") padded the same
    way as MediaItem.release_date so they sort as strings."""
    if not value:
        return None
    parts = value.split("-")
    if not parts[0].isdigit() or len(parts[0]) != 4:
        return None
    month = parts[1] if len(parts) > 1 and parts[1].isdigit() else "01"
    day = parts[2] if len(parts) > 2 and parts[2].isdigit() else "01"
    return f"{parts[0]}-{month}-{day}"


def _recording(item: MediaItem) -> dict | None:
    return _parse(item.musicbrainz_metadata_json)


def _best_release(recording: dict, album_title: str) -> dict | None:
    """The recording's release whose title matches the album; official
    releases beat others, and an exact title beats a partial one."""
    title_key = normalize_artist_name(album_title)
    best, best_score = None, -1
    for release in recording.get("releases") or []:
        if not isinstance(release, dict) or not release.get("id"):
            continue
        release_key = normalize_artist_name(release.get("title") or "")
        score = 0
        if release_key == title_key:
            score += 4
        elif title_key and (title_key in release_key or release_key in title_key):
            score += 2
        if release.get("status") == "Official":
            score += 1
        if score > best_score:
            best, best_score = release, score
    return best if best_score >= 2 else None


def refresh_album_details(db: Session, album: Album, recordings: list[dict] | None = None) -> bool:
    """Re-derive one album's release details from its tracks' recordings
    (read from the database unless given). Returns whether anything was
    found. A release picked by hand is kept."""
    if recordings is None:
        # Just the stored recordings, not whole rows.
        recordings = [
            r
            for r in map(
                _parse,
                db.execute(
                    select(MediaItem.musicbrainz_metadata_json).where(
                        MediaItem.album_id == album.id, MediaItem.musicbrainz_metadata_json.is_not(None)
                    )
                ).scalars(),
            )
            if r
        ]
    votes: Counter[str] = Counter()
    releases: dict[str, dict] = {}
    groups: dict[str, dict] = {}
    release_votes: dict[str, Counter[str]] = {}
    for recording in recordings:
        release = _best_release(recording, album.title) if recording else None
        if release is None:
            continue
        group = release.get("release-group") or {}
        group_id = group.get("id")
        if not group_id:
            continue
        votes[group_id] += 1
        groups[group_id] = group
        releases[release["id"]] = release
        release_votes.setdefault(group_id, Counter())[release["id"]] += 1
    if not votes:
        return False
    group_id = min(votes, key=lambda g: (-votes[g], g))
    group = groups[group_id]
    album.release_group_mbid = group_id
    album.primary_type = group.get("primary-type")
    secondary = [str(t) for t in group.get("secondary-types") or []]
    album.secondary_types_json = secondary or None
    album.first_release_date = _date(group.get("first-release-date"))
    if not album.release_locked or album.release_mbid not in releases:
        counts = release_votes[group_id]
        album.release_mbid = min(counts, key=lambda r: (-counts[r], r))
    release = releases.get(album.release_mbid) or {}
    album.release_date = _date(release.get("date"))
    album.country = release.get("country")
    label_info = [row for row in release.get("label-info") or [] if isinstance(row, dict)]
    if label_info:
        album.label = (label_info[0].get("label") or {}).get("name") or album.label
        album.catalog_number = label_info[0].get("catalog-number") or album.catalog_number
    return True


def section_for(primary_type: str | None, secondary_types: Iterable[str]) -> str:
    """Which artist-page section an album belongs in: a secondary type
    (Live, Compilation) wins over the primary one; unknown is "Album"."""
    secondary = {t.lower() for t in secondary_types}
    if "live" in secondary:
        return "Live"
    if "compilation" in secondary:
        return "Compilation"
    if primary_type in ("Album", "EP", "Single"):
        return primary_type
    if primary_type:
        return "Other"
    return "Album"


def assign_artist_mbids(db: Session, item: MediaItem) -> None:
    """Fill linked artists' MBIDs from the track's cached MusicBrainz
    artist credits (each credit names its artist), and from the file's
    MusicBrainz artist id when the track has a single main artist. A match
    fixed by hand (mbid_locked) is never changed."""
    by_key: dict[str, str] = {}
    recording = _recording(item)
    for credit in (recording or {}).get("artist-credit") or []:
        if not isinstance(credit, dict):
            continue
        artist = credit.get("artist") or {}
        if not artist.get("id"):
            continue
        for name in (credit.get("name"), artist.get("name")):
            if name:
                by_key.setdefault(normalize_artist_name(name), artist["id"])
    links = db.execute(
        select(Artist, TrackArtist.role)
        .join(TrackArtist, TrackArtist.artist_id == Artist.id)
        .where(TrackArtist.media_item_id == item.id)
    ).all()
    mains = {artist.id: artist for artist, role in links if role == "main"}
    if item.musicbrainz_artist_mbid and len(mains) == 1:
        only = next(iter(mains.values()))
        by_key.setdefault(only.normalized_name, item.musicbrainz_artist_mbid)
    for artist, _role in links:
        mbid = by_key.get(artist.normalized_name)
        if mbid and not artist.mbid_locked and artist.mbid != mbid:
            artist.mbid = mbid


def _parse(value: object) -> dict | None:
    """A stored recording (MediaItem.musicbrainz_metadata_json, already
    decoded) if it is an object."""
    return value if isinstance(value, dict) else None


# Tracks per IN (...) list when reading recordings and artist links.
_RECORDING_BATCH = 500


def rebuild_albums(db: Session, *, only_missing: bool = False) -> int:
    """Assign every track its album, then derive each album's details and
    its artists' MBIDs from the cached recordings. Used once on upgrade and
    after bulk relinking; scans and enrichment keep it current per track.
    Bulk SQL, like app.artist_credits.rebuild_all_credits, so it takes
    seconds on a large library."""
    from sqlalchemy import insert, update

    # Step 1 needs only the small columns; the recordings are read album by
    # album below, so memory stays bounded however large the library (all of
    # them at once would be hundreds of megabytes).
    query = select(
        MediaItem.id,
        MediaItem.album,
        MediaItem.album_owner,
        MediaItem.album_artist,
        MediaItem.artist,
    ).where(MediaItem.kind == MediaKind.track)
    if only_missing:
        query = query.where(MediaItem.album_id.is_(None), MediaItem.album.is_not(None))
    rows = db.execute(query).all()

    # 1. Album rows and each track's album_id.
    existing = {(o, t): i for i, o, t in db.execute(select(Album.id, Album.owner_key, Album.title_key)).all()}
    wanted: dict[tuple[str, str], tuple[str | None, str]] = {}
    track_keys: dict[int, tuple[str, str] | None] = {}
    for media_id, album, owner_col, album_artist, artist in rows:
        if not album or not album.strip():
            track_keys[media_id] = None
            continue
        owner = (owner_col or "").strip() or (album_artist or "").strip() or artist
        keys = _keys(owner, album)
        track_keys[media_id] = keys
        if keys not in existing:
            wanted.setdefault(keys, (owner, album))
    new_rows = [
        {"owner": owner, "title": title, "owner_key": keys[0], "title_key": keys[1], "release_locked": False}
        for keys, (owner, title) in wanted.items()
    ]
    for start in range(0, len(new_rows), 2000):
        db.execute(insert(Album), new_rows[start : start + 2000])
    if new_rows:
        existing = {(o, t): i for i, o, t in db.execute(select(Album.id, Album.owner_key, Album.title_key)).all()}
    updates = [
        {"id": media_id, "album_id": existing[keys] if keys else None} for media_id, keys in track_keys.items()
    ]
    for start in range(0, len(updates), 2000):
        db.execute(update(MediaItem), updates[start : start + 2000])
    db.commit()

    # 2. Release details per album, voted from its tracks' recordings, read
    # for a batch of albums at a time and each parsed once. Album-less
    # tracks come last, for their artist MBIDs only.
    by_album: dict[int | None, list[int]] = {}
    for media_id, keys in track_keys.items():
        by_album.setdefault(existing[keys] if keys else None, []).append(media_id)
    album_ids: list[int | None] = [*sorted(a for a in by_album if a is not None)]
    if None in by_album:
        album_ids.append(None)
    mbid_by_track: dict[int, dict[str, str]] = {}
    batch: list[int] = []
    batch_albums: list[int | None] = []

    def flush() -> None:
        recordings_by_album: dict[int, list[dict]] = {}
        for start in range(0, len(batch), _RECORDING_BATCH):
            for media_id, album_id, meta, artist_mbid in db.execute(
                select(
                    MediaItem.id, MediaItem.album_id, MediaItem.musicbrainz_metadata_json, MediaItem.musicbrainz_artist_mbid
                )
                .where(
                    MediaItem.id.in_(batch[start : start + _RECORDING_BATCH]),
                    MediaItem.musicbrainz_metadata_json.is_not(None) | MediaItem.musicbrainz_artist_mbid.is_not(None),
                )
                .order_by(MediaItem.id)
            ):
                recording = _parse(meta)
                if recording and album_id is not None and track_keys.get(media_id):
                    recordings_by_album.setdefault(album_id, []).append(recording)
                by_key: dict[str, str] = {}
                for credit in (recording or {}).get("artist-credit") or []:
                    if not isinstance(credit, dict):
                        continue
                    artist = credit.get("artist") or {}
                    if artist.get("id"):
                        for name in (credit.get("name"), artist.get("name")):
                            if name:
                                by_key.setdefault(normalize_artist_name(name), artist["id"])
                if by_key or artist_mbid:
                    mbid_by_track[media_id] = by_key | ({"": artist_mbid} if artist_mbid else {})
        for album_id in batch_albums:
            recordings = recordings_by_album.get(album_id) if album_id is not None else None
            album = db.get(Album, album_id) if recordings else None
            if album is not None:
                refresh_album_details(db, album, recordings)
        db.commit()
        batch.clear()
        batch_albums.clear()

    for album_id in album_ids:
        batch.extend(by_album[album_id])
        batch_albums.append(album_id)
        if len(batch) >= _RECORDING_BATCH * 4:
            flush()
    flush()

    # 3. Artist MBIDs from the same recordings (a hand-fixed match stays).
    if mbid_by_track:
        tracks = sorted(mbid_by_track)
        links = []
        for start in range(0, len(tracks), _RECORDING_BATCH):
            links += db.execute(
                select(TrackArtist.media_item_id, TrackArtist.role, Artist.id, Artist.normalized_name, Artist.mbid, Artist.mbid_locked)
                .join(Artist, Artist.id == TrackArtist.artist_id)
                .where(TrackArtist.media_item_id.in_(tracks[start : start + _RECORDING_BATCH]))
                .order_by(TrackArtist.media_item_id, TrackArtist.artist_id, TrackArtist.role)
            ).all()
        mains: dict[int, set[int]] = {}
        for media_id, role, artist_id, *_ in links:
            if role == "main":
                mains.setdefault(media_id, set()).add(artist_id)
        changes: dict[int, str] = {}
        for media_id, _role, artist_id, key, current, locked in links:
            found = mbid_by_track.get(media_id)
            if not found or locked:
                continue
            mbid = found.get(key)
            if mbid is None and "" in found and mains.get(media_id) == {artist_id}:
                mbid = found[""]
            if mbid and current != mbid:
                changes.setdefault(artist_id, mbid)
        rows_out = [{"id": artist_id, "mbid": mbid} for artist_id, mbid in changes.items()]
        for start in range(0, len(rows_out), 2000):
            db.execute(update(Artist), rows_out[start : start + 2000])
        db.commit()
    prune_orphan_albums(db)
    return len(rows)


def prune_orphan_albums(db: Session) -> int:
    """Drop albums no track points at, unless they hold fetched info."""
    from sqlalchemy import delete

    used = select(MediaItem.album_id).where(MediaItem.album_id.is_not(None))
    result = db.execute(
        delete(Album).where(Album.id.not_in(used), Album.info_fetched_at.is_(None))
    )
    db.commit()
    # A DELETE returns a CursorResult at runtime; Session.execute is typed as Result.
    return cast("CursorResult[Any]", result).rowcount or 0
