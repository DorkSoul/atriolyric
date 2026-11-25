"""Artwork to choose from for a song, album or artist ("Choose artwork…").

Candidates come from the Cover Art Archive (every image of the album's
release group's editions), the files themselves (embedded pictures and
cover/folder images beside them) and, for artists, Wikimedia Commons (the
photo the artist's Wikidata entry names, credited in its label). Picking one downloads
it through the same guarded fetcher as cover art (HTTPS, no private
addresses, size cap) and stores it as custom art, so its thumbnail is made
immediately and "Reset art" still restores the default.

A candidate id is self-describing: "url:<https url>" for a remote image,
"embedded:<media id>" or "folder:<media id>:<file name>" for local ones.
"""

import hashlib
from datetime import timedelta
from pathlib import Path
from urllib.parse import quote, urlparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.access import accessible_root_ids, can_access_root, media_file_path
from app.art import ART_HOSTS, _download_remote_art, extract_embedded_art
from app.artist_credits import artist_link_filter, normalize_artist_name
from app.custom_art import (
    _ALBUM_SEP,
    collection_filters,
    get_custom_art,
    store_custom_art,
)
from app.external_cache import cached, peek, store
from app.image_sniff import sniff_mime
from app.listenbrainz_data import artist_mbid
from app.metadata_info import artist_image, find_album, mb_release_group
from app.metadata_providers import coverart
from app.models import MediaItem, MediaKind, User

ART_TTL = timedelta(days=30)
MAX_RELEASES = 8
MAX_LOCAL_TRACKS = 40
FOLDER_IMAGE_NAMES = ("cover", "folder", "front", "album", "artist", "albumart", "albumartsmall")
IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp", ".gif")
MAX_FOLDER_IMAGE_BYTES = 30 * 1024 * 1024
# Remote images may only come from where candidates come from.
ALLOWED_HOSTS = ART_HOSTS


def _remote(url: str, thumb: str | None, source: str, label: str) -> dict:
    return {"id": f"url:{url}", "source": source, "label": label, "url": url, "thumb_url": thumb or url}


def _local(token: str, source: str, label: str) -> dict:
    path = f"/api/art/local-candidate?token={quote(token, safe='')}"
    return {"id": token, "source": source, "label": label, "url": path, "thumb_url": path}


def _caa_candidates(db: Session, release_group_mbid: str) -> list[dict]:
    group = mb_release_group(db, release_group_mbid)
    releases = [r for r in group.get("releases") or [] if isinstance(r, dict) and r.get("id")]
    releases.sort(key=lambda r: (r.get("status") != "Official", r.get("date") or "9999"))
    out, seen = [], set()
    for release in releases[:MAX_RELEASES]:
        images = cached(
            db,
            "coverart",
            "release-images",
            release["id"],
            lambda release_id=release["id"]: coverart.list_release_images(release_id),
            ttl=ART_TTL,
        ) or []
        where = " · ".join(v for v in (release.get("date"), release.get("country")) if v)
        for image in images:
            if image["url"] in seen:
                continue
            seen.add(image["url"])
            kind = ", ".join(image["types"]) or ("Front" if image["front"] else "Image")
            out.append(_remote(image["url"], image["thumb"], "Cover Art Archive", f"{kind}{f' · {where}' if where else ''}"))
    # Front covers first, then the rest in edition order.
    out.sort(key=lambda c: not c["label"].startswith("Front"))
    return out


def _local_candidates(db: Session, user: User, items: list[MediaItem]) -> list[dict]:
    """Embedded pictures (one per distinct image) and image files beside
    the tracks, for up to MAX_LOCAL_TRACKS tracks."""
    out, seen_hashes, seen_files = [], set(), set()
    for item in items[:MAX_LOCAL_TRACKS]:
        if not can_access_root(db, user, item.library_root_id):
            continue
        path = media_file_path(item)
        if path is None:
            continue
        embedded = extract_embedded_art(path)
        if embedded:
            digest = hashlib.sha1(embedded[0], usedforsecurity=False).hexdigest()
            if digest not in seen_hashes:
                seen_hashes.add(digest)
                out.append(_local(f"embedded:{item.id}", "Embedded", f"In “{item.title}”"))
        folder = path.parent
        if folder in seen_files:
            continue
        seen_files.add(folder)
        root = Path(item.library_root.path).resolve()
        for image in sorted(_folder_images(folder, root)):
            out.append(_local(f"folder:{item.id}:{image.name}", "Folder", image.name))
    return out


def _checked_image(entry: Path, root: Path) -> tuple[Path, str] | None:
    """The real file behind `entry` and its sniffed MIME - but only if it
    stays inside the library root (a symlink named cover.jpg must not reach
    elsewhere on the disk), is small enough and really is an image."""
    try:
        real = entry.resolve(strict=True)
        if not real.is_relative_to(root) or not real.is_file() or real.stat().st_size > MAX_FOLDER_IMAGE_BYTES:
            return None
        with real.open("rb") as handle:
            mime = sniff_mime(handle.read(16))
    except OSError:
        return None
    return (real, mime) if mime else None


def _folder_images(folder: Path, root: Path) -> list[Path]:
    try:
        entries = list(folder.iterdir())
    except OSError:
        return []
    return [
        entry
        for entry in entries
        if entry.suffix.lower() in IMAGE_SUFFIXES
        and entry.stem.lower() in FOLDER_IMAGE_NAMES
        and _checked_image(entry, root) is not None
    ]


def _tracks(db: Session, user: User, filters: list) -> list[MediaItem]:
    return list(
        db.execute(
            select(MediaItem)
            .where(
                MediaItem.library_root_id.in_(accessible_root_ids(db, user)),
                MediaItem.kind == MediaKind.track,
                *filters,
            )
            .order_by(MediaItem.track_number, MediaItem.id)
            .limit(MAX_LOCAL_TRACKS)
        ).scalars()
    )


def album_candidates(db: Session, user: User, owner: str | None, album: str, extra_items: list[MediaItem] | None = None) -> list[dict]:
    items = extra_items or _tracks(db, user, collection_filters("album", f"{owner or ''}{_ALBUM_SEP}{album}"))
    out = _local_candidates(db, user, items)
    album_row = find_album(db, owner, album)
    if album_row is not None and album_row.release_group_mbid:
        out += _caa_candidates(db, album_row.release_group_mbid)
    return out


def artist_candidates(db: Session, user: User, name: str) -> list[dict]:
    out = []
    mbid = artist_mbid(db, name)
    image = artist_image(db, mbid) if mbid else None
    if image:
        credit = " · ".join(part for part in (image.get("author"), image.get("license")) if part)
        out.append(_remote(image["url"], image["thumb"], "Wikimedia Commons", credit or "Wikimedia Commons"))
    # Folder images named artist.jpg beside any of the artist's tracks.
    items = _tracks(db, user, [artist_link_filter(name)])
    out += [c for c in _local_candidates(db, user, items) if c["source"] == "Folder" and c["label"].lower().startswith("artist")]
    return out


def candidates_for(db: Session, user: User, entity_type: str, entity_key: str) -> list[dict]:
    """Choices for an entity (already validated by the caller)."""
    if entity_type == "artist":
        return artist_candidates(db, user, entity_key)
    if entity_type == "album":
        owner, _, album = entity_key.partition(_ALBUM_SEP)
        return album_candidates(db, user, owner or None, album)
    if entity_type == "media_item":
        item = db.get(MediaItem, int(entity_key))
        if item is None or not item.album:
            return _local_candidates(db, user, [item]) if item else []
        owner = (item.album_artist or "").strip() or item.artist
        siblings = _tracks(db, user, collection_filters("album", f"{owner or ''}{_ALBUM_SEP}{item.album}"))
        # The song's own picture first.
        ordered = [item, *(s for s in siblings if s.id != item.id)]
        return album_candidates(db, user, owner, item.album, ordered)
    return []


def local_candidate_bytes(db: Session, user: User, token: str) -> tuple[bytes, str] | None:
    """The image behind a local candidate token, if the user can see it."""
    kind, _, rest = token.partition(":")
    media_id, _, name = rest.partition(":")
    if not media_id.isdecimal():
        return None
    item = db.get(MediaItem, int(media_id))
    if item is None or not can_access_root(db, user, item.library_root_id):
        return None
    path = media_file_path(item)
    if path is None:
        return None
    if kind == "embedded":
        return extract_embedded_art(path)
    if kind == "folder":
        # Only a recognised cover file name in the track's own folder.
        image = path.parent / name
        if (
            "/" in name
            or "\\" in name
            or image.suffix.lower() not in IMAGE_SUFFIXES
            or image.stem.lower() not in FOLDER_IMAGE_NAMES
        ):
            return None
        # The type comes from the bytes, not the file name.
        checked = _checked_image(image, Path(item.library_root.path).resolve())
        return (checked[0].read_bytes(), checked[1]) if checked else None
    return None


def candidate_origin(db: Session, entity_type: str, entity_key: str, candidate_id: str) -> tuple[str, dict | None] | None:
    """(source, credit) to store with a chosen candidate, or None when it is
    not one this entity offers. A Wikimedia Commons image must be the very
    one the artist's Wikidata entry names - its credit is stored with it
    and shown wherever the art is - so an arbitrary Commons URL is refused."""
    if not candidate_id.startswith("url:"):
        return "local", None
    url = candidate_id[4:]
    host = (urlparse(url).hostname or "").lower()
    if host.endswith("wikimedia.org"):
        if entity_type != "artist":
            return None
        mbid = artist_mbid(db, entity_key)
        image = artist_image(db, mbid) if mbid else None
        if not image or url not in (image.get("url"), image.get("thumb")):
            return None
        return "wikimedia", image_credit(image)
    return "coverartarchive", None


def apply_commons_photo(db: Session, artist_name: str, mbid: str | None) -> bool:
    """Save the artist's Wikimedia Commons photo (the one its Wikidata entry
    names) as its picture, with the credit its licence asks for. Only for an
    artist without art of its own, and only once per artist: art someone
    picked or uploaded is never replaced, and a photo removed with "Reset
    art" is not put back on the next run. True when a photo was saved."""
    if not mbid or get_custom_art(db, "artist", artist_name) is not None:
        return False
    marker = normalize_artist_name(artist_name)
    if peek(db, "artistphoto", "applied", marker):
        return False
    image = artist_image(db, mbid)
    if not image:
        return False
    try:
        fetched = _download_remote_art(image["url"])
    except Exception:  # noqa: BLE001 - a failed download: tried again next run
        return False
    if not fetched:
        return False
    try:
        store_custom_art(
            db, "artist", artist_name, fetched[0], bump=False, source="wikimedia", credit=image_credit(image)
        )
    except ValueError:  # not a usable image
        return False
    store(db, "artistphoto", "applied", marker, {"mbid": mbid})
    return True


def image_credit(image: dict) -> dict:
    """The credit fields of an artist_image answer."""
    return {k: image.get(k) for k in ("author", "license", "license_url", "page_url")}


def candidate_bytes(db: Session, user: User, candidate_id: str) -> bytes | None:
    """Download or read a chosen candidate. None if it's not allowed or
    couldn't be fetched."""
    if candidate_id.startswith("url:"):
        url = candidate_id[4:]
        host = (urlparse(url).hostname or "").lower()
        if urlparse(url).scheme != "https" or not any(host == h or host.endswith(f".{h}") for h in ALLOWED_HOSTS):
            return None
        try:
            fetched = _download_remote_art(url)
        except Exception:  # noqa: BLE001 - a refused or failed download
            return None
        return fetched[0] if fetched else None
    found = local_candidate_bytes(db, user, candidate_id)
    return found[0] if found else None

