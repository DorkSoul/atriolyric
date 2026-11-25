import re
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.albums import assign_album, assign_artist_mbids, refresh_album_details
from app.deferred import defer_fetch
from app.metadata_providers import coverart, lyrics, musicbrainz
from app.metadata_providers.result import Failed, Found
from app.metadata_taxonomy import (
    LISTENBRAINZ_SOURCE,
    clean_names,
    normalise_name,
    replace_item_genres,
    replace_item_listenbrainz_tags,
    replace_item_musicbrainz_tags,
)
from app.models import Album, Genre, MediaItem, utcnow

LYRICS_MISS_TTL = timedelta(hours=6)

_VERSION_SUFFIX = re.compile(
    r"\s*(?:[-–—]\s*|\(\s*)(?:acoustic(?:\s+version)?|"
    r"edit(?:ed)?(?:\s+version)?|radio\s+edit|single\s+version|"
    r"\d{4}\s+version|remaster(?:ed)?(?:\s+\d{4})?|live\s+version)\)?\s*$",
    re.IGNORECASE,
)


_ALBUM_REFRESH_BATCH = "atriolyric_album_refresh_batch"


@contextmanager
def album_refresh_batch(db: Session) -> Iterator[None]:
    """Inside the block, enrich_media_item notes the album it touched instead
    of re-deriving it at once: a bulk pass over an album's twelve tracks
    would otherwise re-read all twelve recordings twelve times. The caller
    runs refresh_batched_albums before committing a batch."""
    db.info[_ALBUM_REFRESH_BATCH] = set()
    try:
        yield
    finally:
        db.info.pop(_ALBUM_REFRESH_BATCH, None)


def refresh_batched_albums(db: Session) -> int:
    """Re-derive each album noted since the last call, once. Returns how
    many were refreshed."""
    pending: set[int] = db.info.get(_ALBUM_REFRESH_BATCH) or set()
    refreshed = 0
    if pending:
        db.flush()  # the sessions do not autoflush; the recordings are read back
    for album_id in sorted(pending):
        album = db.get(Album, album_id)
        if album is not None:
            refresh_album_details(db, album)
            refreshed += 1
    pending.clear()
    return refreshed


def _fallback_title(title: str) -> str | None:
    """Remove only an unambiguous trailing version label for a second lookup."""
    fallback = _VERSION_SUFFIX.sub("", title).strip()
    return fallback if fallback and fallback != title else None


def _lookup_recording(item: MediaItem) -> musicbrainz.LookupResult:
    if item.musicbrainz_isrc:
        result = musicbrainz.lookup_recording_by_isrc(item.musicbrainz_isrc)
        if result.value:
            return result

    search = musicbrainz.search_recording_result
    result = search(item.artist or "", item.title)
    if result.value:
        return result
    fallback = _fallback_title(item.title)
    if fallback:
        fallback_result = search(item.artist or "", fallback)
        if fallback_result.value or not result.error:
            return fallback_result
    return result


def _record_lookup_state(
    item: MediaItem, status: str, error: str | None = None
) -> None:
    item.musicbrainz_lookup_status = status
    item.musicbrainz_last_attempt_at = utcnow()
    item.musicbrainz_last_error = error


def split_known_genres(db: Session, tags: list[tuple[str, int]]) -> tuple[list[str], list[tuple[str, int]]]:
    """(the tags naming a genre the library already knows, the rest) - done
    once per artist by the ListenBrainz tags task, not once per track."""
    known = set(
        db.scalars(
            select(Genre.normalized_name).where(
                Genre.normalized_name.in_([n for _name, n in clean_names(t for t, _c in tags)])
            )
        )
    )
    genres = [name for name, _count in tags if normalise_name(name) in known]
    return genres, [(name, count) for name, count in tags if normalise_name(name) not in known]


def apply_listenbrainz_tags(
    db: Session,
    item: MediaItem,
    tags: list[tuple[str, int]],
    split: tuple[list[str], list[tuple[str, int]]] | None = None,
) -> None:
    """Add the track's artist tags (ListenBrainz: the artist's MusicBrainz
    community tags) to a track, and mark it asked (online_tags_fetched_at),
    also when the artist has none - that is final until the next "all" run.

    Purely additive: file tags and the recording's own MusicBrainz data are
    never replaced. A tag naming a genre the library already knows is
    stored as a genre, the rest as tags - as MusicBrainz's are.
    """
    genres, other = split if split is not None else split_known_genres(db, tags)
    replace_item_genres(db, item, genres, LISTENBRAINZ_SOURCE)
    replace_item_listenbrainz_tags(db, item, other)
    item.online_tags_fetched_at = utcnow()


def enrich_media_item(
    db: Session,
    item: MediaItem,
    *,
    commit: bool = True,
    fetch_cover_art: bool = False,
) -> dict:
    """Fetch and retain rich MusicBrainz metadata for one library track.

    The raw recording response prevents data loss as MusicBrainz grows, while
    its genres and folksonomy tags are normalised for mix selection. Existing
    file tags remain authoritative for the visible artist/album/primary genre.
    """
    match = None
    recording_mbid = item.musicbrainz_recording_mbid
    if not recording_mbid:
        lookup = _lookup_recording(item)
        match = lookup.value
        if match is None:
            status = "retry" if lookup.error else "no_match"
            _record_lookup_state(item, status, lookup.error)
            if commit:
                db.commit()
            return {"matched": False, "metadata_saved": False, "status": status}
        recording_mbid = match.get("recording_mbid")
    if not recording_mbid:
        _record_lookup_state(item, "no_match")
        if commit:
            db.commit()
        return {"matched": False, "metadata_saved": False, "status": "no_match"}

    detail_result = musicbrainz.fetch_recording_result(recording_mbid)
    detail = detail_result.value
    # A failed detail request must not discard a successful lightweight
    # match, but does mean there is no genre/tag data to update yet.
    source = detail or match or {}
    artist_credit = (source.get("artist-credit") or [{}])[0]
    releases = source.get("releases") or []
    release = releases[0] if releases else {}

    item.musicbrainz_recording_mbid = recording_mbid
    item.musicbrainz_artist_mbid = (
        (artist_credit.get("artist") or {}).get("id")
        or source.get("artist_mbid")
        or item.musicbrainz_artist_mbid
    )
    artist_name = artist_credit.get("name") or source.get("artist")
    album_name = release.get("title") or source.get("album")
    if not item.artist and artist_name:
        item.artist = artist_name
    if not item.album and album_name:
        item.album = album_name

    release_mbid = release.get("id") or source.get("release_mbid")
    if fetch_cover_art and release_mbid:
        front = coverart.front_image(release_mbid, (release.get("release-group") or {}).get("id"))
        if not isinstance(front, Failed):  # a failure keeps what was known
            item.cover_art_url = front.data if isinstance(front, Found) else None

    if detail is not None:
        genres = [row.get("name") for row in detail.get("genres") or []]
        replace_item_genres(db, item, genres, "musicbrainz")
        genre_names = {normalized for _name, normalized in clean_names(genres)}
        tags = [
            (row.get("name"), row.get("count"))
            for row in detail.get("tags") or []
            if isinstance(row, dict)
            and normalise_name(str(row.get("name") or "")) not in genre_names
        ]
        replace_item_musicbrainz_tags(db, item, tags)
        if not item.genre and genres:
            item.genre = clean_names(genres)[0][0]
        item.musicbrainz_metadata_json = detail
        item.musicbrainz_fetched_at = utcnow()
        _record_lookup_state(item, "complete")
        # The recording names each credited artist and the release it is on:
        # enough to fill artist MBIDs and the album's release group/type.
        assign_artist_mbids(db, item)
        album = assign_album(db, item)
        if album is not None:
            db.flush()
            batch: set[int] | None = db.info.get(_ALBUM_REFRESH_BATCH)
            if batch is not None:
                batch.add(album.id)
            else:
                refresh_album_details(db, album)
    else:
        status = "retry" if detail_result.error else "no_match"
        _record_lookup_state(item, status, detail_result.error)

    if commit:
        db.commit()
    return {
        "matched": True,
        "metadata_saved": detail is not None,
        "status": item.musicbrainz_lookup_status,
        "musicbrainz_recording_mbid": item.musicbrainz_recording_mbid,
        "genres": len(detail.get("genres") or []) if detail else 0,
        "tags": len(detail.get("tags") or []) if detail else 0,
    }


def fetch_cover_art_for_item(
    db: Session, item: MediaItem, *, commit: bool = True
) -> dict:
    """Fetch cover art using the release id in the cached MusicBrainz record.

    Album art is intentionally separate from metadata enrichment. This keeps
    the MusicBrainz job limited to MusicBrainz requests and means a slow or
    unavailable Cover Art Archive does not slow metadata collection.
    """
    detail = item.musicbrainz_metadata_json
    if not isinstance(detail, dict):
        return {"matched": False, "fetched": False}

    releases = detail.get("releases") or []
    release = releases[0] if releases and isinstance(releases[0], dict) else {}
    release_mbid = release.get("id")
    if not release_mbid:
        return {"matched": False, "fetched": False}

    front = coverart.front_image(release_mbid, (release.get("release-group") or {}).get("id"))
    if isinstance(front, Failed):
        # Not "this release has no art": leave the item to be tried again.
        return {"matched": True, "fetched": False, "failed": True, "error": front.reason}
    item.cover_art_url = front.data if isinstance(front, Found) else None
    if commit:
        db.commit()
    return {"matched": True, "fetched": bool(item.cover_art_url)}


def get_or_fetch_lyrics(
    db: Session,
    item: MediaItem,
    file_path: Path,
    *,
    allow_provider_fetch: bool = True,
) -> dict:
    """Cached on the media item after the first successful fetch - never
    re-fetches once we have something, embedded-tag or provider-sourced."""
    if item.lyrics_plain or item.lyrics_synced:
        plain, synced = lyrics.normalize_lyrics(item.lyrics_plain, item.lyrics_synced)
        return {"plain": plain, "synced": synced, "source": "cached"}
    if item.lyrics_fetched_at and utcnow() - item.lyrics_fetched_at < LYRICS_MISS_TTL:
        return {"plain": None, "synced": None, "source": None}

    embedded = lyrics.get_embedded_lyrics(file_path)
    if embedded:
        item.lyrics_plain, item.lyrics_synced = lyrics.normalize_lyrics(embedded, None)
        item.lyrics_fetched_at = utcnow()
        db.commit()
        return {"plain": item.lyrics_plain, "synced": item.lyrics_synced, "source": "embedded"}

    if not allow_provider_fetch or defer_fetch():
        return {"plain": None, "synced": None, "source": None}

    fetched = lyrics.fetch_lrclib_lyrics(
        item.artist or "",
        item.title,
        album=item.album,
        duration_seconds=item.duration_seconds,
    )
    if fetched is None:
        # lrclib could not be asked: not a miss, so not remembered as one
        # (a timeout does not hide lyrics for LYRICS_MISS_TTL).
        return {"plain": None, "synced": None, "source": None}
    item.lyrics_fetched_at = utcnow()
    if not fetched:
        db.commit()
        return {"plain": None, "synced": None, "source": None}

    item.lyrics_plain, item.lyrics_synced = lyrics.normalize_lyrics(
        fetched.get("plain"), fetched.get("synced")
    )
    db.commit()
    return {
        "plain": item.lyrics_plain,
        "synced": item.lyrics_synced,
        "source": "lrclib",
    }
