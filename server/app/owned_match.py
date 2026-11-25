"""Matching provider answers ({"artist", "title", "mbid"} rows) back to
tracks the library owns. Shared by similar tracks and top tracks
(app.listenbrainz_data), the want list and the catalogue."""

import string
from collections.abc import Collection, Iterable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.artist_credits import normalize_artist_name
from app.models import Artist, MediaItem, MediaKind, TrackArtist
from app.title_match import normalize_title

__all__ = ["match_owned", "normalize_title", "sqlite_lower"]

_ASCII_LOWER = str.maketrans(string.ascii_uppercase, string.ascii_lowercase)


def sqlite_lower(text: str) -> str:
    """SQLite's built-in lower(): ASCII letters only."""
    return text.translate(_ASCII_LOWER)


def match_owned(
    db: Session, accessible: Collection[int], rows: Iterable[dict]
) -> list[int | None]:
    """For each {"artist", "title", "mbid"}: the id of an owned track, or
    None. A recording MBID match wins; otherwise the artist must be one the
    track credits and the titles must agree once version suffixes are
    stripped. Lowest id wins among duplicates."""
    rows = list(rows)
    if not rows or not accessible:
        return [None] * len(rows)
    mbids = {row["mbid"] for row in rows if row.get("mbid")}
    by_mbid: dict[str, int] = {}
    if mbids:
        for media_id, mbid in db.execute(
            select(MediaItem.id, MediaItem.musicbrainz_recording_mbid)
            .where(
                MediaItem.musicbrainz_recording_mbid.in_(mbids),
                MediaItem.library_root_id.in_(accessible),
                MediaItem.kind == MediaKind.track,
            )
            .order_by(MediaItem.id.desc())
        ):
            assert mbid is not None  # in_() never matches NULL
            by_mbid[mbid] = media_id
    artist_keys = {normalize_artist_name(row["artist"]) for row in rows if row.get("artist")}
    by_artist_title: dict[tuple[str, str], int] = {}
    if artist_keys:
        for media_id, title, artist_key in db.execute(
            select(MediaItem.id, MediaItem.title, Artist.normalized_name)
            .join(TrackArtist, TrackArtist.media_item_id == MediaItem.id)
            .join(Artist, Artist.id == TrackArtist.artist_id)
            .where(
                Artist.normalized_name.in_(artist_keys),
                MediaItem.library_root_id.in_(accessible),
                MediaItem.kind == MediaKind.track,
            )
            .order_by(MediaItem.id.desc())
        ):
            title_key = normalize_title(title)
            if title_key:  # a blank title is never matched by name
                by_artist_title[(artist_key, title_key)] = media_id
        # The track's own artist string too, for rows a scan hasn't linked
        # to artists yet. The lookup values are folded the way the
        # lower(artist) index is: SQLite's lower() folds ASCII only (the
        # Unicode str.lower() form is asked for too, for names already in
        # lower case). Candidates are confirmed by normalize_artist_name below.
        names = {
            folded
            for row in rows
            if row.get("artist")
            for folded in (sqlite_lower(row["artist"].strip()), row["artist"].strip().lower())
        }
        for media_id, title, artist_name in db.execute(
            select(MediaItem.id, MediaItem.title, MediaItem.artist)
            .where(
                func.lower(MediaItem.artist).in_(names),
                MediaItem.library_root_id.in_(accessible),
                MediaItem.kind == MediaKind.track,
            )
            .order_by(MediaItem.id.desc())
        ):
            assert artist_name is not None  # lower(NULL) is never in_() a list
            title_key = normalize_title(title)
            if title_key:
                by_artist_title.setdefault((normalize_artist_name(artist_name), title_key), media_id)
    out: list[int | None] = []
    for row in rows:
        found = by_mbid.get(row.get("mbid") or "")
        title_key = normalize_title(row.get("title"))
        if found is None and row.get("artist") and title_key:
            found = by_artist_title.get((normalize_artist_name(row["artist"]), title_key))
        out.append(found)
    return out
