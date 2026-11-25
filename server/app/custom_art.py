"""User-uploaded artwork for songs, albums, artists and playlists.

Shared library-wide and stored only in the app's data directory - never
written to audio files. Precedence for a track's art is its own custom art,
then its album's custom art, then the normal chain (see app.routers.stream).
"""

import base64
import calendar
import uuid
from collections.abc import Iterable
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.access import accessible_root_ids, can_access_root
from app.after_commit import run_after_commit
from app.art import ArtBytes, _resize_art
from app.artist_credits import ALL_ROLES, artist_link_filter
from app.config import settings
from app.image_sniff import IMAGE_TYPES as _IMAGE_TYPES
from app.image_sniff import MAX_IMAGE_DIMENSION, sniff_image, too_large
from app.library_cache import bump_library_version
from app.models import CustomArt, MediaItem, MediaKind, Playlist, User, utcnow

ENTITY_TYPES = ("media_item", "album", "artist", "playlist")
MAX_UPLOAD_BYTES = 30 * 1024 * 1024
_ALBUM_SEP = "\x1f"
_BATCH = 500


def encode_key(raw: str) -> str:
    """URL-safe, unpadded base64 of a name - the route key for albums/artists."""
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_key(key: str) -> str:
    try:
        return base64.urlsafe_b64decode(key + "=" * (-len(key) % 4)).decode()
    except ValueError:
        raise HTTPException(404, "Unknown art key") from None


def album_entity_key(artist: str | None, album: str) -> str:
    return f"{artist or ''}{_ALBUM_SEP}{album}"


def album_art_key(artist: str | None, album: str) -> str:
    return encode_key(album_entity_key(artist, album))


def artist_art_key(artist: str) -> str:
    return encode_key(artist)


def art_version(row: CustomArt) -> int:
    """Epoch milliseconds of the last change - append as ?v= to bust caches."""
    return calendar.timegm(row.updated_at.utctimetuple()) * 1000 + row.updated_at.microsecond // 1000


def _file(name: str) -> Path:
    return settings.custom_art_dir / name


def content_type(path: Path) -> str:
    return _IMAGE_TYPES.get(path.suffix.lower(), "application/octet-stream")


def entity_key_for_url(entity_type: str, key: str) -> str:
    """Translate a route key into the stored entity key (404 if malformed)."""
    if entity_type not in ENTITY_TYPES:
        raise HTTPException(404, "Unknown art type")
    if entity_type in ("media_item", "playlist"):
        if not key.isdecimal():
            raise HTTPException(404, "Unknown art key")
        return str(int(key))
    return decode_key(key)


def collection_filters(entity_type: str, entity_key: str) -> list:
    """Track filters selecting an artist's or album's tracks, matching either
    credit the way the Artists/Albums tabs group them."""
    if entity_type == "artist":
        # Any credit, so a featured-only artist's page can have art too.
        return [
            or_(
                MediaItem.artist == entity_key,
                MediaItem.album_artist == entity_key,
                artist_link_filter(entity_key, ALL_ROLES),
            )
        ]
    artist, _, album = entity_key.partition(_ALBUM_SEP)
    credit = artist or None
    return [
        MediaItem.album == album,
        or_(
            MediaItem.artist.is_(None) if credit is None else MediaItem.artist == credit,
            MediaItem.album_artist.is_(None)
            if credit is None
            else MediaItem.album_artist == credit,
            MediaItem.album_owner.is_(None) if credit is None else MediaItem.album_owner == credit,
        ),
    ]


def validate_entity(db: Session, user: User, entity_type: str, entity_key: str) -> None:
    """404 unless the entity exists and this user may see it (playlists:
    only their owner)."""
    if entity_type == "media_item":
        item = db.get(MediaItem, int(entity_key))
        if item is None or not can_access_root(db, user, item.library_root_id):
            raise HTTPException(404, "Media item not found")
        return
    if entity_type == "playlist":
        playlist = db.get(Playlist, int(entity_key))
        if playlist is None or playlist.user_id != user.id:
            raise HTTPException(404, "Playlist not found")
        return
    filters = [
        MediaItem.library_root_id.in_(accessible_root_ids(db, user)),
        MediaItem.kind == MediaKind.track,
        *collection_filters(entity_type, entity_key),
    ]
    if db.execute(select(MediaItem.id).where(*filters).limit(1)).first() is None:
        raise HTTPException(404, f"{entity_type.capitalize()} not found")


def get_custom_art(db: Session, entity_type: str, entity_key: str) -> CustomArt | None:
    return db.execute(
        select(CustomArt).where(
            CustomArt.entity_type == entity_type, CustomArt.entity_key == entity_key
        )
    ).scalar_one_or_none()


def _remove_files(row: CustomArt) -> None:
    for name in (row.path, row.thumb_path):
        _file(name).unlink(missing_ok=True)


def store_custom_art(
    db: Session,
    entity_type: str,
    entity_key: str,
    data: bytes,
    *,
    bump: bool = True,
    source: str | None = None,
    credit: dict | None = None,
) -> CustomArt:
    """Validate `data` as a real image, write the full copy and a <=500px
    thumbnail, then swap them in for any previous art. Raises ValueError for
    anything that isn't a decodable image."""
    ext = sniff_image(data)
    if ext is None:
        raise ValueError("Not a supported image")
    if too_large(data):
        raise ValueError(
            f"Image is too large (more than {MAX_IMAGE_DIMENSION}x{MAX_IMAGE_DIMENSION} pixels)"
        )
    directory = settings.custom_art_dir
    directory.mkdir(parents=True, exist_ok=True)
    stem = f"{entity_type}-{uuid.uuid4().hex}"
    thumb_name = f"{stem}.thumb.jpg"
    full_name = f"{stem}{ext}"
    # The thumbnail doubles as proof the bytes decode as an image at all.
    if not _resize_art(ArtBytes(data, _IMAGE_TYPES[ext]), _file(thumb_name)):
        raise ValueError("Not a decodable image")
    tmp = directory / f".{full_name}.tmp"
    try:
        tmp.write_bytes(data)
        tmp.replace(_file(full_name))
    finally:
        tmp.unlink(missing_ok=True)

    row = get_custom_art(db, entity_type, entity_key)
    old = None
    if row is None:
        row = CustomArt(entity_type=entity_type, entity_key=entity_key)
        db.add(row)
    else:
        old = CustomArt(path=row.path, thumb_path=row.thumb_path)
    row.path, row.thumb_path, row.updated_at = full_name, thumb_name, utcnow()
    row.source, row.credit_json = source, credit
    db.commit()
    db.refresh(row)
    if old is not None:
        _remove_files(old)
    if bump:
        bump_library_version()
    return row


def delete_custom_art(db: Session, entity_type: str, entity_key: str) -> bool:
    row = get_custom_art(db, entity_type, entity_key)
    if row is None:
        return False
    db.delete(row)
    db.commit()
    # After the commit, so a failed delete keeps the art it still points at.
    _remove_files(row)
    bump_library_version()
    return True


def purge_custom_art(db: Session, entity_type: str, keys: Iterable[object]) -> None:
    """Drop custom art rows for entities that were deleted, and their files
    once the caller's transaction commits (a rollback keeps both). Does not
    commit - callers do, as part of their own delete."""
    wanted = [str(k) for k in keys]
    for start in range(0, len(wanted), _BATCH):
        rows = db.execute(
            select(CustomArt).where(
                CustomArt.entity_type == entity_type,
                CustomArt.entity_key.in_(wanted[start : start + _BATCH]),
            )
        ).scalars()
        for row in list(rows):
            run_after_commit(db, "custom_art_files", _unlink_names, (row.path, row.thumb_path))
            db.delete(row)


def _unlink_names(names: set[str]) -> None:
    for name in names:
        _file(name).unlink(missing_ok=True)


def _rows(db: Session, entity_type: str, keys: list[str]) -> dict[str, CustomArt]:
    found: dict[str, CustomArt] = {}
    for start in range(0, len(keys), _BATCH):
        for row in db.execute(
            select(CustomArt).where(
                CustomArt.entity_type == entity_type,
                CustomArt.entity_key.in_(keys[start : start + _BATCH]),
            )
        ).scalars():
            found[row.entity_key] = row
    return found


def effective_item_art(
    db: Session, items: list[MediaItem]
) -> dict[int, tuple[CustomArt, bool]]:
    """For each item with custom art: (row, is_own). The song's own art wins;
    otherwise its album's art cascades down to it."""
    own = _rows(db, "media_item", [str(i.id) for i in items])
    has_albums = (
        db.execute(
            select(CustomArt.id).where(CustomArt.entity_type == "album").limit(1)
        ).first()
        is not None
    )
    result: dict[int, tuple[CustomArt, bool]] = {}
    keys: dict[int, list[str]] = {}
    for item in items:
        if str(item.id) in own:
            result[item.id] = (own[str(item.id)], True)
        elif has_albums and item.album:
            keys[item.id] = [album_entity_key(item.artist, item.album)]
            for owner in (item.album_artist, item.album_owner):
                if owner and owner != item.artist:
                    keys[item.id].append(album_entity_key(owner, item.album))
    if keys:
        albums = _rows(db, "album", sorted({k for ks in keys.values() for k in ks}))
        for item_id, ks in keys.items():
            for k in ks:
                if k in albums:
                    result[item_id] = (albums[k], False)
                    break
    return result


def find_item_art(db: Session, item: MediaItem) -> CustomArt | None:
    found = effective_item_art(db, [item]).get(item.id)
    return found[0] if found else None


def annotate_items(db: Session, outs: list) -> None:
    """Set custom_art / art_version on MediaItemOut-like objects (by .id)."""
    if not outs:
        return
    # Most libraries have little or no custom art: one cheap look at that
    # table avoids loading every listed item just to find nothing.
    if db.execute(
        select(CustomArt.id).where(CustomArt.entity_type.in_(("media_item", "album"))).limit(1)
    ).first() is None:
        for out in outs:
            out.custom_art, out.art_version = False, None
        return
    items: list[MediaItem] = []
    for start in range(0, len(outs), _BATCH):
        ids = [o.id for o in outs[start : start + _BATCH]]
        items.extend(db.execute(select(MediaItem).where(MediaItem.id.in_(ids))).scalars())
    found = effective_item_art(db, items)
    for out in outs:
        hit = found.get(out.id)
        out.custom_art = bool(hit and hit[1])
        out.art_version = art_version(hit[0]) if hit else None


def _any_art(db: Session, entity_type: str) -> bool:
    return db.execute(
        select(CustomArt.id).where(CustomArt.entity_type == entity_type).limit(1)
    ).first() is not None


def _list_rows(db: Session, entity_type: str, keys: list[str]) -> dict[str, CustomArt]:
    """The art for a listing's `keys`, in one or two statements whatever its
    length. A long list (the full album or artist list) reads every row of
    the type in one query: custom art is what users uploaded, a small table,
    where the list's keys would take an IN query per 500 - ~64 for a full
    album list on every request. A short list first checks
    there is any art of the type (most libraries have none), then asks for
    its own keys. Read per request, so new or removed art shows at once."""
    if not keys:
        return {}
    if len(keys) > _BATCH:
        return {
            row.entity_key: row
            for row in db.execute(
                select(CustomArt).where(CustomArt.entity_type == entity_type)
            ).scalars()
        }
    return _rows(db, entity_type, keys) if _any_art(db, entity_type) else {}


def annotate_albums(db: Session, outs: list) -> None:
    """Set art_key / custom_art / art_version on AlbumOut-like objects."""
    rows = _list_rows(db, "album", [album_entity_key(o.artist, o.album) for o in outs])
    for out in outs:
        out.art_key = album_art_key(out.artist, out.album)
        row = rows.get(album_entity_key(out.artist, out.album))
        out.custom_art = row is not None
        out.art_version = art_version(row) if row else None


def annotate_artists(db: Session, outs: list) -> None:
    rows = _list_rows(db, "artist", [o.artist for o in outs])
    for out in outs:
        out.art_key = artist_art_key(out.artist)
        row = rows.get(out.artist)
        out.custom_art = row is not None
        out.art_version = art_version(row) if row else None


def annotate_playlists(db: Session, outs: list, attr: str = "id") -> None:
    """Set custom_art / art_version on playlist-like objects keyed by `attr`."""
    rows = _rows(db, "playlist", [str(getattr(o, attr)) for o in outs])
    for out in outs:
        row = rows.get(str(getattr(out, attr)))
        out.custom_art = row is not None
        out.art_version = art_version(row) if row else None
