"""Normalised, queryable genre and tag metadata for library tracks."""

from collections.abc import Iterable

from sqlalchemy import ColumnElement, delete, func, or_, select
from sqlalchemy.orm import Session

from app.models import (
    Genre,
    MediaItem,
    MediaItemGenre,
    MediaItemMusicBrainzTag,
    MusicBrainzTag,
    Tag,
)

LISTENBRAINZ_SOURCE = "listenbrainz"


def normalise_name(value: str) -> str:
    """Make metadata matching insensitive to case and incidental whitespace."""
    return " ".join(value.split()).casefold()


def clean_names(values: Iterable[str | None]) -> list[tuple[str, str]]:
    """Return unique ``(display_name, normalized_name)`` pairs in input order."""
    seen: set[str] = set()
    result: list[tuple[str, str]] = []
    for raw in values:
        if not isinstance(raw, str):
            continue
        name = " ".join(raw.split())
        normalized = normalise_name(name)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append((name, normalized))
    return result


def genre_filter(name: str) -> ColumnElement[bool]:
    """Match a genre through normalized metadata, with legacy-column fallback."""
    return or_(
        MediaItem.genre == name,
        MediaItem.genres.any(Genre.normalized_name == normalise_name(name)),
    )


def tag_filter(name: str) -> ColumnElement[bool]:
    """Match a track carrying this tag, from the library's own tags or a
    MusicBrainz/ListenBrainz folksonomy tag. Names compare case- and
    whitespace-insensitively (custom tags have no stored normalised form, so
    they are folded in SQL)."""
    return or_(
        MediaItem.tags.any(
            or_(Tag.name == name, func.lower(func.trim(Tag.name)) == normalise_name(name))
        ),
        MediaItem.musicbrainz_tags.any(
            MusicBrainzTag.normalized_name == normalise_name(name)
        ),
    )


def replace_item_genres(
    db: Session, item: MediaItem, values: Iterable[str | None], source: str
) -> None:
    """Replace one source's genres while retaining genres from other sources."""
    db.execute(
        delete(MediaItemGenre).where(
            MediaItemGenre.media_item_id == item.id,
            MediaItemGenre.source == source,
        ),
        execution_options={"synchronize_session": False},
    )
    for name, normalized in clean_names(values):
        genre = db.scalars(select(Genre).where(Genre.normalized_name == normalized)).one_or_none()
        if genre is None:
            genre = Genre(name=name, normalized_name=normalized)
            db.add(genre)
            db.flush()
        db.add(
            MediaItemGenre(media_item_id=item.id, genre_id=genre.id, source=source)
        )


def replace_item_musicbrainz_tags(
    db: Session,
    item: MediaItem,
    values: Iterable[tuple[str | None, int | None]],
) -> None:
    """Replace MusicBrainz's non-genre folksonomy tags for one track.

    The recording's own tags win: a tag the track already has from its
    artist (ListenBrainz) becomes MusicBrainz's row with the recording's
    votes.
    """
    db.execute(
        delete(MediaItemMusicBrainzTag).where(
            MediaItemMusicBrainzTag.media_item_id == item.id,
            MediaItemMusicBrainzTag.source == "musicbrainz",
        ),
        execution_options={"synchronize_session": False},
    )
    _store_tags(db, item, values, "musicbrainz", take_over=True)


def replace_item_listenbrainz_tags(
    db: Session,
    item: MediaItem,
    values: Iterable[tuple[str | None, int | None]],
) -> None:
    """Replace this track's tags from its artist (ListenBrainz, i.e. the
    artist's MusicBrainz community tags). A tag the recording itself
    carries (source musicbrainz) keeps that row."""
    db.execute(
        delete(MediaItemMusicBrainzTag).where(
            MediaItemMusicBrainzTag.media_item_id == item.id,
            MediaItemMusicBrainzTag.source == LISTENBRAINZ_SOURCE,
        ),
        execution_options={"synchronize_session": False},
    )
    _store_tags(db, item, values, LISTENBRAINZ_SOURCE, take_over=False)


def stored_musicbrainz_tags(item: MediaItem) -> list[tuple[str | None, int | None]]:
    """MusicBrainz's non-genre tags from the cached recording response."""
    detail = item.musicbrainz_metadata_json
    if not isinstance(detail, dict):
        return []
    genre_names = {
        normalized
        for _name, normalized in clean_names(
            row.get("name") for row in detail.get("genres") or [] if isinstance(row, dict)
        )
    }
    return [
        (row.get("name"), row.get("count"))
        for row in detail.get("tags") or []
        if isinstance(row, dict) and normalise_name(str(row.get("name") or "")) not in genre_names
    ]


def _store_tags(
    db: Session,
    item: MediaItem,
    values: Iterable[tuple[str | None, int | None]],
    source: str,
    *,
    take_over: bool,
) -> None:
    """Add `values` as `source`'s tags. One row per (track, tag): when the
    track already has the tag from another source, `take_over` makes the
    row this source's, otherwise the existing row stays."""
    # Rows added by an earlier call in this transaction must be visible to
    # the db.get below (sessions here don't autoflush).
    db.flush()
    counts: dict[str, tuple[str, int]] = {}
    for raw_name, raw_count in values:
        for name, normalized in clean_names([raw_name]):
            count = raw_count if isinstance(raw_count, int) else 0
            previous = counts.get(normalized)
            if previous is None or count > previous[1]:
                counts[normalized] = (name, count)
    for normalized, (name, count) in counts.items():
        tag = db.scalars(
            select(MusicBrainzTag).where(MusicBrainzTag.normalized_name == normalized)
        ).one_or_none()
        if tag is None:
            tag = MusicBrainzTag(name=name, normalized_name=normalized)
            db.add(tag)
            db.flush()
        existing = db.get(MediaItemMusicBrainzTag, (item.id, tag.id))
        if existing is not None:
            if take_over and existing.source != source:
                existing.source = source
                existing.vote_count = count
            continue
        db.add(
            MediaItemMusicBrainzTag(
                media_item_id=item.id,
                musicbrainz_tag_id=tag.id,
                vote_count=count,
                source=source,
            )
        )
