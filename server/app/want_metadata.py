"""Metadata for things on the want list (and the artists/albums shown while
searching it): cover art, artist pictures and the text info the modals show.

Nothing is hotlinked by the browser - art is downloaded here (Cover Art
Archive for albums, Wikimedia Commons for artist photos),
stored like custom art under the `catalog_*` entity types (keyed by MBID, so
it never mixes with library art) and served from /api/catalog/art. Provider
text answers go through app.external_cache, so adding an entry warms every
lookup its info page would make. Misses are remembered for a week - only
definitive ones: when any source failed, nothing is remembered.

The browser's art requests never download: they get what is stored, or a
204 while the fetch runs on app.provider_queue (`queue_catalog_art`).
"""

import logging
import threading
import weakref
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import provider_queue
from app.art import _download_remote_art
from app.catalog import MAX_GROUP_COVERS, catalog_fetched_at, release_groups, tracklist
from app.custom_art import get_custom_art, store_custom_art
from app.db import SessionLocal
from app.external_cache import cached, peek, store
from app.metadata_info import (
    album_info,
    artist_image,
    artist_info,
    mb_artist,
    mb_release_group,
)
from app.metadata_providers import musicbrainz
from app.metadata_providers.result import failed, recording_failures
from app.models import CustomArt, User, WantListItem, utcnow

logger = logging.getLogger(__name__)

ART_TYPES = {"artist": "catalog_artist", "album": "catalog_album"}
MISS_TTL = timedelta(days=7)
RECHECK = timedelta(days=7)
# A wanted artist's page serves what is saved at once; data older than this is
# re-checked in the background while the page is open.
REVALIDATE_AFTER = timedelta(days=1)

# One fetch per (kind, mbid) at a time: the provider queue already runs one
# job at a time, but the scheduled backfill runs beside it, and two downloads
# of one cover would collide on custom_art's unique key.
_fetch_locks: weakref.WeakValueDictionary[tuple[str, str], threading.RLock] = weakref.WeakValueDictionary()
_fetch_locks_guard = threading.Lock()


def _fetch_lock(kind: str, mbid: str) -> threading.RLock:
    with _fetch_locks_guard:
        return _fetch_locks.setdefault((kind, mbid), threading.RLock())


def _recent_miss(db: Session, kind: str, mbid: str) -> bool:
    found = peek(db, "catalogart", kind, mbid)
    if not found:
        return False
    fetched = found[1]
    if fetched.tzinfo is not None:
        fetched = fetched.astimezone(UTC).replace(tzinfo=None)
    return utcnow() - fetched < MISS_TTL


def _sources(db: Session, kind: str, mbid: str) -> list[tuple[str, str, dict | None]]:
    """(image URL, source, credit) to try, best first - always by MBID, so
    the names a request carries never choose the shared image."""
    if kind == "album":
        return [(f"https://coverartarchive.org/release-group/{mbid}/front-500", "coverartarchive", None)]
    image = artist_image(db, mbid)
    if not image:
        return []
    credit = {k: image.get(k) for k in ("author", "license", "license_url", "page_url")}
    return [(image["url"], "wikimedia", credit)]


def _resolved_names(db: Session, kind: str, mbid: str) -> tuple[str | None, str | None] | None:
    """(artist, album) as MusicBrainz names them, or None when it does not
    know `mbid` (or cannot be reached)."""
    if kind == "artist":
        name = mb_artist(db, mbid).get("name")
        return (name, None) if name else None
    group = mb_release_group(db, mbid)
    if not group.get("title"):
        return None
    credit = (group.get("artist-credit") or [{}])[0]
    return credit.get("name") or (credit.get("artist") or {}).get("name"), group["title"]


def _download(url: str) -> bytes | None:
    """The image at `url`; None when there is none there (a 404, not an
    image, too big). A transport error or another HTTP error is recorded as
    a provider failure (see catalog_art)."""
    try:
        fetched = _download_remote_art(url)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code not in (404, 410):
            failed(f"Art download HTTP {exc.response.status_code}")
        return None
    except ValueError:  # a URL the downloader refuses: never a usable source
        return None
    except Exception as exc:  # noqa: BLE001 - any transport failure is a retry
        failed(f"Art download failed: {type(exc).__name__}")
        return None
    return fetched[0] if fetched else None


def catalog_art(
    db: Session, kind: str, mbid: str, *, bump: bool = True, resolve_names: bool = False,
) -> CustomArt | None:
    """The stored art for a catalog artist/album, downloading it on first
    ask. None when no provider has any (remembered for a week, but only when
    every source answered: a failure leaves it to be tried again).

    Images are always chosen by MBID (Cover Art Archive release group,
    the artist's Wikimedia Commons photo), never by names a request
    carries. `resolve_names` is for requests from the browser: when
    MusicBrainz does not know `mbid`, nothing is fetched or stored."""
    entity = ART_TYPES[kind]
    row = get_custom_art(db, entity, mbid)
    if row is not None or _recent_miss(db, kind, mbid):
        return row
    with _fetch_lock(kind, mbid):
        # Fetched by whoever held the lock before us?
        row = get_custom_art(db, entity, mbid)
        if row is not None:
            return row
        with recording_failures() as failures:
            if resolve_names and _resolved_names(db, kind, mbid) is None:
                return None
            for url, source, credit in _sources(db, kind, mbid):
                data = _download(url)
                if data is None:
                    continue
                try:
                    return store_custom_art(db, entity, mbid, data, bump=False, source=source, credit=credit)
                except ValueError:  # not a decodable image: try the next source
                    continue
                except IntegrityError:  # stored meanwhile by another process
                    db.rollback()
                    return get_custom_art(db, entity, mbid)
        if failures:
            logger.info("Catalog art %s %s not fetched (%s); will retry", kind, mbid, failures[-1])
            return None
        store(db, "catalogart", kind, mbid, {"missing": True})
        return None


def stored_catalog_art(db: Session, kind: str, mbid: str) -> tuple[CustomArt | None, bool]:
    """(the stored art, whether it is known to have none) - no provider calls."""
    row = get_custom_art(db, ART_TYPES[kind], mbid)
    return row, row is None and _recent_miss(db, kind, mbid)


def artist_cover_fallbacks(
    db: Session, wanted: dict[str, tuple[str | None, list[str]]]
) -> dict[str, str | None]:
    """For each artist with no stored photo, a release group whose cover is
    stored, to show in the photo's place: {key: release group MBID or None}.

    `wanted` maps a key to (artist MBID, release groups to try first - the
    wanted albums and songs). After those come the artist's saved release
    groups, albums first, newest first. Stored data only, no provider calls."""
    artist_mbids = {mbid for mbid, _groups in wanted.values() if mbid}
    with_photo = (
        set(
            db.scalars(
                select(CustomArt.entity_key).where(
                    CustomArt.entity_type == ART_TYPES["artist"], CustomArt.entity_key.in_(artist_mbids)
                )
            )
        )
        if artist_mbids
        else set()
    )
    candidates: dict[str, list[str]] = {}
    for key, (artist_mbid, groups) in wanted.items():
        if artist_mbid and artist_mbid in with_photo:
            candidates[key] = []
            continue
        tried = [mbid for mbid in groups if mbid]
        if artist_mbid:
            saved = release_groups(db, artist_mbid, allow_fetch=False) or []
            saved = sorted(saved, key=lambda g: g.get("first_release_date") or "", reverse=True)
            saved.sort(key=lambda g: g.get("section") != "Album")
            tried += [g["mbid"] for g in saved if g.get("mbid")]
        candidates[key] = tried
    every = {mbid for tried in candidates.values() for mbid in tried}
    covered = (
        set(
            db.scalars(
                select(CustomArt.entity_key).where(
                    CustomArt.entity_type == ART_TYPES["album"], CustomArt.entity_key.in_(every)
                )
            )
        )
        if every
        else set()
    )
    return {key: next((mbid for mbid in tried if mbid in covered), None) for key, tried in candidates.items()}


def queue_catalog_art(kind: str, mbid: str, *, user_id: int | None = None) -> bool:
    """Fetch a catalog cover/picture on the background provider queue (one
    job per (kind, mbid) however many tiles ask). Names come from
    MusicBrainz, never from the request (see catalog_art). False when it is
    already queued, or the queue is full or holds too many of `user_id`'s
    jobs (the asking user): nothing is queued then."""
    def run():
        db = SessionLocal()
        try:
            catalog_art(db, kind, mbid, resolve_names=True)
        except Exception:
            logger.exception("Catalog art fetch failed for %s %s", kind, mbid)
        finally:
            db.close()

    return provider_queue.submit(f"catalog-art:{kind}:{mbid}", run, user_id=user_id)


def _fetch_entry(db: Session, entry: WantListItem, user: User | None) -> None:
    """Everything known about one entry: art, plus the cached provider
    lookups its info/catalog pages make."""
    if entry.artist_mbid:
        catalog_art(db, "artist", entry.artist_mbid)
    if entry.kind == "track" and entry.recording_mbid:
        recording_mbid = entry.recording_mbid  # a local, so the lambda keeps the str type
        cached(
            db, "musicbrainz", "recording-lookup", recording_mbid,
            lambda: musicbrainz.fetch_recording_result(recording_mbid).value or None,
            ttl=timedelta(days=30),
        )
    if entry.kind == "artist" and entry.artist_mbid:
        if user is not None:
            artist_info(db, user, entry.artist)
        for group in (release_groups(db, entry.artist_mbid) or [])[:MAX_GROUP_COVERS]:
            if group.get("mbid"):
                catalog_art(db, "album", group["mbid"])
                tracklist(db, group["mbid"])
            # Up to 150 release groups, several requests each: in the
            # scheduled fill, let covers and tracklists people are waiting
            # for go first (a no-op when a user's own job runs this).
            provider_queue.yield_to_user_jobs()
    elif entry.release_group_mbid:
        if entry.kind == "album" and user is not None:
            album_info(db, user, entry.artist, entry.title)
        catalog_art(db, "album", entry.release_group_mbid)
        tracklist(db, entry.release_group_mbid)


def fetch_entry_in_background(entry_id: int, *, user_id: int | None = None) -> None:
    """Queue an entry's metadata download on the background provider queue
    (one job at a time, behind page requests). When the queue is full (or
    holds too many of `user_id`'s jobs) nothing is queued: the scheduled
    fill (backfill_wanted_metadata) picks the entry up later."""
    def run():
        db = SessionLocal()
        try:
            entry = db.get(WantListItem, entry_id)
            if entry is not None:
                _fetch_entry(db, entry, db.get(User, entry.user_id))
                store(db, "wantmeta", "checked", str(entry_id), {"ok": True})
        except Exception:  # no cover at this source; try the next
            logger.exception("Want-list metadata fetch failed for entry %s", entry_id)
        finally:
            db.close()

    provider_queue.submit(f"want-meta:{entry_id}", run, user_id=user_id)


def backfill_wanted_metadata(db: Session, limit: int = 20) -> int:
    """Scheduled pass over the want list: fetch what entries added earlier
    (or interrupted fetches) are missing, and re-check each entry weekly so
    expired provider answers are renewed here rather than while someone is
    looking at the page. Bounded per run. On the provider queue it is a
    scheduled job: between entries (and between an artist's release groups,
    see _fetch_entry) it lets waiting user-triggered jobs run first."""
    done = 0
    for entry in db.scalars(
        select(WantListItem).where(WantListItem.excluded.is_(False)).order_by(WantListItem.id)
    ):
        key = str(entry.id)
        checked = peek(db, "wantmeta", "checked", key)
        if checked and _age_of(checked[1]) < RECHECK:
            continue
        _fetch_entry(db, entry, db.get(User, entry.user_id))
        store(db, "wantmeta", "checked", key, {"ok": True})
        done += 1
        if done >= limit:
            break
        provider_queue.yield_to_user_jobs()
    return done


def _age_of(fetched: datetime) -> timedelta:
    if fetched.tzinfo is not None:
        fetched = fetched.astimezone(UTC).replace(tzinfo=None)
    return utcnow() - fetched


# --- keeping a wanted artist's page fresh without making it wait ---------------


def _refresh_key(artist_mbid: str) -> str:
    return f"refresh-artist:{artist_mbid}"


def artist_refreshing(artist_mbid: str) -> bool:
    return provider_queue.pending(_refresh_key(artist_mbid))


def _artist_checked_recently(db: Session, artist_mbid: str) -> bool:
    stamps = [catalog_fetched_at(db, artist_mbid)]
    marker = peek(db, "wantmeta", "artist-checked", artist_mbid)
    stamps.append(marker[1] if marker else None)
    return any(stamp is not None and _age_of(stamp) < REVALIDATE_AFTER for stamp in stamps)


def revalidate_artist(
    db: Session, artist_mbid: str, name: str | None, *, force: bool = False, user_id: int | None = None
) -> bool:
    """Queue a background re-check of a catalog artist's data when it is
    over a day old (or `force`). The caller serves what is saved right now.
    Returns whether a re-check is queued or running - False too when the
    queue had no room for it (or for more of `user_id`'s jobs): the saved
    data is shown as it is and the next view asks again."""
    if artist_refreshing(artist_mbid):
        return True
    if not force and _artist_checked_recently(db, artist_mbid):
        return False

    def run():
        job_db = SessionLocal()
        try:
            mb_artist(job_db, artist_mbid, refresh=True)
            groups = release_groups(job_db, artist_mbid, refresh=True) or []
            catalog_art(job_db, "artist", artist_mbid)
            for group in groups[:MAX_GROUP_COVERS]:
                if group.get("mbid"):
                    catalog_art(job_db, "album", group["mbid"])
                    tracklist(job_db, group["mbid"])
        except Exception:  # no cover at this source; try the next
            logger.exception("Background re-check failed for artist %s", artist_mbid)
        finally:
            # Remember the attempt either way, so a provider outage doesn't
            # queue a new re-check on every page view.
            store(job_db, "wantmeta", "artist-checked", artist_mbid, {"ok": True})
            job_db.close()

    key = _refresh_key(artist_mbid)
    return provider_queue.submit(key, run, user_id=user_id) or provider_queue.pending(key)
