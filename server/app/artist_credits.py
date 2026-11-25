"""Who a track is by: split artist tags into individual artists with roles.

A track's `artist` / `album_artist` columns keep the display string exactly
as tagged ("Drake feat. Rihanna"). This module turns those strings - or the
multi-value ARTISTS / ALBUMARTISTS tags Picard and beets write - into
`Artist` rows linked through `TrackArtist` with one of three roles:

* ``main``: the performing artists.
* ``featured``: guests, from "feat./ft./featuring" in the artist or title.
* ``album_artist``: who owns the album the track is on. Tracks without an
  album artist tag fall back to their main artists, so "owns an album" is
  always one role to query.

Splitting is conservative by default (";" and "/" only, plus feat. markers),
admin-configurable, and never splits a name on the exception list.
"""

import json
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, cast

from sqlalchemy import (
    ColumnElement,
    CursorResult,
    delete,
    exists,
    func,
    insert,
    select,
    update,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Artist, MediaItem, MediaKind, SystemSetting, TrackArtist
from app.schemas import ArtistCreditOut

ROLE_MAIN = "main"
ROLE_FEATURED = "featured"
ROLE_ALBUM_ARTIST = "album_artist"
# Roles that make a track "by" the artist, for artist pages, Play all and
# likes/dislikes. Featured guests are linked but kept out of these.
OWNING_ROLES = (ROLE_MAIN, ROLE_ALBUM_ARTIST)
ALL_ROLES = (ROLE_MAIN, ROLE_FEATURED, ROLE_ALBUM_ARTIST)

SEPARATORS_KEY = "artist_separators"
EXCEPTIONS_KEY = "artist_split_exceptions"
DEFAULT_SEPARATORS = [";", "/"]
DEFAULT_EXCEPTIONS = [
    "AC/DC",
    "Tyler, The Creator",
    "Earth, Wind & Fire",
    "Crosby, Stills, Nash & Young",
    "Simon & Garfunkel",
    "Hall & Oates",
    "Florence + the Machine",
]

# "feat." markers. Matched case-insensitively on word boundaries so a name
# like "Defeater" or "Ftw" never trips them.
_FEAT_RE = re.compile(r"\s+(?:feat\.?|ft\.?|featuring)\s+", re.IGNORECASE)
# The guest list after a feat. marker is usually "A, B & C".
_GUEST_SPLIT_RE = re.compile(r"\s*(?:,|&|\band\b)\s*", re.IGNORECASE)
# "(feat. X)" / "[ft. X]" in a title.
_TITLE_FEAT_RE = re.compile(
    r"[\(\[]\s*(?:feat\.?|ft\.?|featuring)\s+([^\)\]]+)[\)\]]", re.IGNORECASE
)


def normalize_artist_name(name: str) -> str:
    """Identity key for an artist name: case-folded, accents and spacing
    collapsed, so "Beyoncé" and "beyonce " are one artist."""
    decomposed = unicodedata.normalize("NFKD", name)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return " ".join(stripped.casefold().split())


@dataclass(frozen=True)
class SplitRules:
    separators: tuple[str, ...]
    exceptions: tuple[str, ...]


def _load_list(db: Session, key: str, default: list[str]) -> list[str]:
    row = db.get(SystemSetting, key)
    if row is None:
        return list(default)
    try:
        value = json.loads(row.value)
    except (TypeError, json.JSONDecodeError):
        return list(default)
    if not isinstance(value, list):
        return list(default)
    return [str(v) for v in value if str(v).strip()]


def load_split_rules(db: Session) -> SplitRules:
    return SplitRules(
        separators=tuple(_load_list(db, SEPARATORS_KEY, DEFAULT_SEPARATORS)),
        exceptions=tuple(_load_list(db, EXCEPTIONS_KEY, DEFAULT_EXCEPTIONS)),
    )


def save_split_rules(db: Session, separators: list[str], exceptions: list[str]) -> SplitRules:
    # Separators keep their spaces (" x " differs from "x"); names don't.
    cleaned_lists = (
        (SEPARATORS_KEY, [s for s in separators if s.strip()]),
        (EXCEPTIONS_KEY, [s.strip() for s in exceptions if s.strip()]),
    )
    for key, cleaned in cleaned_lists:
        row = db.get(SystemSetting, key)
        payload = json.dumps(cleaned, ensure_ascii=False)
        if row is None:
            db.add(SystemSetting(key=key, value=payload))
        else:
            row.value = payload
    db.commit()
    return load_split_rules(db)


def _protect(text: str, rules: SplitRules) -> tuple[str, dict[str, str]]:
    """Swap exception names for placeholders so no separator splits them."""
    placeholders: dict[str, str] = {}
    for index, name in enumerate(sorted(rules.exceptions, key=len, reverse=True)):
        pattern = re.compile(re.escape(name), re.IGNORECASE)
        token = f"\x00{index}\x00"

        def keep(match: re.Match, token: str = token) -> str:
            placeholders[token] = match.group(0)
            return token

        text = pattern.sub(keep, text)
    return text, placeholders


def _restore(text: str, placeholders: dict[str, str]) -> str:
    for token, original in placeholders.items():
        text = text.replace(token, original)
    return text


def _split_on(text: str, separators: Iterable[str]) -> list[str]:
    parts = [text]
    for separator in separators:
        if not separator:
            continue
        parts = [piece for part in parts for piece in part.split(separator)]
    return [part.strip() for part in parts if part.strip()]


def split_artist_string(text: str | None, rules: SplitRules) -> tuple[list[str], list[str]]:
    """("A; B feat. C & D") -> (["A", "B"], ["C", "D"])."""
    if not text or not text.strip():
        return [], []
    protected, placeholders = _protect(text.strip(), rules)
    main_part, *guest_parts = _FEAT_RE.split(protected, maxsplit=1)
    main = [_restore(p, placeholders) for p in _split_on(main_part, rules.separators)]
    guests: list[str] = []
    for part in guest_parts:
        # A trailing "(feat. X)" written into the artist tag.
        part = part.strip().strip("()[]").strip()
        for piece in _split_on(part, rules.separators):
            guests.extend(
                _restore(g, placeholders)
                for g in _GUEST_SPLIT_RE.split(piece)
                if g.strip()
            )
    return main, guests


def featured_from_title(title: str | None, rules: SplitRules) -> list[str]:
    if not title:
        return []
    guests: list[str] = []
    for match in _TITLE_FEAT_RE.finditer(title):
        protected, placeholders = _protect(match.group(1), rules)
        for piece in _split_on(protected, rules.separators):
            guests.extend(
                _restore(g.strip(), placeholders)
                for g in _GUEST_SPLIT_RE.split(piece)
                if g.strip()
            )
    return guests


def _dedupe(names: Iterable[str], seen: set[str]) -> list[str]:
    out = []
    for name in names:
        key = normalize_artist_name(name)
        if key and key not in seen:
            seen.add(key)
            out.append(name.strip())
    return out


def compute_credits(
    *,
    title: str | None,
    artist: str | None,
    album_artist: str | None,
    artists_multi: list[str] | None,
    album_artists_multi: list[str] | None,
    rules: SplitRules,
) -> list[tuple[str, str]]:
    """Every (name, role) credit for one track, in display order."""
    display_main, display_guests = split_artist_string(artist, rules)
    if artists_multi and len(artists_multi) > 1:
        # A proper multi-value tag lists everyone; the display string still
        # says which of them are guests ("A feat. B").
        guest_keys = {normalize_artist_name(g) for g in display_guests}
        main_names = [n for n in artists_multi if normalize_artist_name(n) not in guest_keys]
        guest_names = [n for n in artists_multi if normalize_artist_name(n) in guest_keys]
        if not main_names:
            main_names, guest_names = artists_multi[:1], artists_multi[1:]
    else:
        main_names, guest_names = display_main, display_guests
    guest_names = [*guest_names, *featured_from_title(title, rules)]

    seen: set[str] = set()
    credits = [(name, ROLE_MAIN) for name in _dedupe(main_names, seen)]
    credits += [(name, ROLE_FEATURED) for name in _dedupe(guest_names, seen)]

    if album_artists_multi and len(album_artists_multi) > 1:
        owners = album_artists_multi
    elif album_artist and album_artist.strip():
        owners = split_artist_string(album_artist, rules)[0]
    else:
        owners = [name for name, role in credits if role == ROLE_MAIN]
    credits += [(name, ROLE_ALBUM_ARTIST) for name in _dedupe(owners, set())]
    return credits


def get_or_create_artist(db: Session, name: str, cache: dict[str, Artist] | None = None) -> Artist:
    key = normalize_artist_name(name)
    if cache is not None and key in cache:
        return cache[key]
    artist = db.execute(select(Artist).where(Artist.normalized_name == key)).scalar_one_or_none()
    if artist is None:
        # Two roots can scan at once: if the other one just added this
        # artist, use its row.
        try:
            with db.begin_nested():
                artist = Artist(name=name.strip(), normalized_name=key)
                db.add(artist)
        except IntegrityError:
            artist = db.execute(select(Artist).where(Artist.normalized_name == key)).scalar_one()
    if cache is not None:
        cache[key] = artist
    return artist


def sync_track_credits(
    db: Session,
    item: MediaItem,
    credits: list[tuple[str, str]],
    cache: dict[str, Artist] | None = None,
) -> None:
    """Replace one track's artist links (and its album owner). Videos keep
    none."""
    db.execute(delete(TrackArtist).where(TrackArtist.media_item_id == item.id))
    if item.kind != MediaKind.track:
        item.album_owner = None
        return
    mains = [name for name, role in credits if role == ROLE_MAIN]
    item.album_owner = (item.album_artist or "").strip() or ", ".join(mains) or item.artist
    for position, (name, role) in enumerate(credits):
        artist = get_or_create_artist(db, name, cache)
        db.add(
            TrackArtist(
                media_item_id=item.id, artist_id=artist.id, role=role, position=position
            )
        )


def artist_tags_payload(
    artists_multi: list[str] | None, album_artists_multi: list[str] | None
) -> dict[str, list[str]] | None:
    """What to keep in MediaItem.artist_tags_json: only multi-value lists
    (a single value is the same as the display string)."""
    payload = {}
    if artists_multi and len(artists_multi) > 1:
        payload["artists"] = artists_multi
    if album_artists_multi and len(album_artists_multi) > 1:
        payload["album_artists"] = album_artists_multi
    return payload or None


def _stored_tags(value: object) -> dict:
    """The stored multi-value tags as read from MediaItem.artist_tags_json
    ({} for none, or for anything that is not an object)."""
    return value if isinstance(value, dict) else {}


def credits_from_columns(item: MediaItem, rules: SplitRules) -> list[tuple[str, str]]:
    """Credits from what the row stores - the display strings plus any
    multi-value tags the last scan kept - without re-reading the file."""
    stored = _stored_tags(item.artist_tags_json)
    return compute_credits(
        title=item.title,
        artist=item.artist,
        album_artist=item.album_artist,
        artists_multi=stored.get("artists"),
        album_artists_multi=stored.get("album_artists"),
        rules=rules,
    )


_BULK_CHUNK = 2000


def rebuild_all_credits(db: Session, *, only_missing: bool = False) -> int:
    """Recompute links for every track from its stored strings - after the
    split rules change, or once on a database with no links. Multi-value
    tags are picked up again by the next scan's re-read.

    Bulk SQL rather than one ORM round trip per track: a library of 100k+
    tracks has to link in seconds, not an hour, since the first run happens
    during startup."""
    rules = load_split_rules(db)
    query = select(
        MediaItem.id, MediaItem.title, MediaItem.artist, MediaItem.album_artist, MediaItem.artist_tags_json
    ).where(MediaItem.kind == MediaKind.track)
    if only_missing:
        query = query.where(MediaItem.id.not_in(select(TrackArtist.media_item_id)))
    rows = db.execute(query).all()
    artist_ids: dict[str, int] = dict(db.execute(select(Artist.normalized_name, Artist.id)).all())
    count = 0
    for start in range(0, len(rows), _BULK_CHUNK):
        chunk = rows[start : start + _BULK_CHUNK]
        computed = []
        new_artists: dict[str, str] = {}
        for media_id, title, artist, album_artist, tags_json in chunk:
            stored = _stored_tags(tags_json)
            credits = compute_credits(
                title=title,
                artist=artist,
                album_artist=album_artist,
                artists_multi=stored.get("artists"),
                album_artists_multi=stored.get("album_artists"),
                rules=rules,
            )
            computed.append((media_id, album_artist, artist, credits))
            for name, _role in credits:
                key = normalize_artist_name(name)
                if key not in artist_ids:
                    new_artists.setdefault(key, name.strip())
        if new_artists:
            db.execute(
                insert(Artist),
                [{"name": name, "normalized_name": key, "mbid_locked": False} for key, name in new_artists.items()],
            )
            artist_ids.update(
                db.execute(
                    select(Artist.normalized_name, Artist.id).where(Artist.normalized_name.in_(list(new_artists)))
                ).all()
            )
        ids = [media_id for media_id, *_ in computed]
        db.execute(delete(TrackArtist).where(TrackArtist.media_item_id.in_(ids)))
        links, seen, owners = [], set(), []
        for media_id, album_artist, artist, credits in computed:
            for position, (name, role) in enumerate(credits):
                key = (media_id, artist_ids[normalize_artist_name(name)], role)
                if key in seen:
                    continue
                seen.add(key)
                links.append({"media_item_id": key[0], "artist_id": key[1], "role": role, "position": position})
            mains = [name for name, role in credits if role == ROLE_MAIN]
            owners.append(
                {"id": media_id, "album_owner": (album_artist or "").strip() or ", ".join(mains) or artist}
            )
        if links:
            db.execute(insert(TrackArtist), links)
        db.execute(update(MediaItem), owners)
        db.commit()
        count += len(chunk)
    prune_orphan_artists(db)
    return count


def prune_orphan_artists(db: Session) -> int:
    """Drop artists no track links to any more - unless they carry data
    worth keeping (a fixed MusicBrainz match or fetched info)."""
    linked = select(TrackArtist.artist_id)
    result = db.execute(
        delete(Artist).where(
            Artist.id.not_in(linked),
            Artist.mbid_locked.is_(False),
            Artist.info_fetched_at.is_(None),
        )
    )
    db.commit()
    # An UPDATE returns a CursorResult at runtime; Session.execute is typed as Result.
    return cast("CursorResult[Any]", result).rowcount or 0


def artist_link_filter(name: str, roles: Iterable[str] = OWNING_ROLES) -> ColumnElement[bool]:
    """SQL predicate on MediaItem: the track credits `name` in one of `roles`."""
    return exists().where(
        TrackArtist.media_item_id == MediaItem.id,
        TrackArtist.role.in_(list(roles)),
        TrackArtist.artist_id == Artist.id,
        Artist.normalized_name == normalize_artist_name(name),
    )


def album_owner_expr() -> ColumnElement[str]:
    """The album's owner for one track as displayed: the stored owner (album
    artist, else main artists without guests), falling back to the raw tags
    for rows not linked yet. Albums are grouped and opened by (owner, title)."""
    return func.coalesce(
        func.nullif(MediaItem.album_owner, ""),
        func.nullif(MediaItem.album_artist, ""),
        MediaItem.artist,
    )


_CREDIT_BATCH = 900


def credits_for(
    db: Session, media_item_ids: Iterable[int], roles: Iterable[str] | None = None
) -> dict[int, list[dict]]:
    """{media_item_id: [{"name", "role"}, ...]} in tag order."""
    ids = list(dict.fromkeys(media_item_ids))
    out: dict[int, list[dict]] = {}
    for start in range(0, len(ids), _CREDIT_BATCH):
        query = (
            select(TrackArtist.media_item_id, Artist.name, TrackArtist.role)
            .join(Artist, Artist.id == TrackArtist.artist_id)
            .where(TrackArtist.media_item_id.in_(ids[start : start + _CREDIT_BATCH]))
            .order_by(TrackArtist.media_item_id, TrackArtist.position)
        )
        if roles is not None:
            query = query.where(TrackArtist.role.in_(list(roles)))
        for media_item_id, name, role in db.execute(query):
            out.setdefault(media_item_id, []).append({"name": name, "role": role})
    return out


def annotate_credits(db: Session, outs: list) -> None:
    """Fill `.artists` on MediaItemOut-like objects with their track credits
    (album-artist links are left out: they describe the album, not the song
    - unless they are the only credit)."""
    credits = credits_for(db, [out.id for out in outs])
    for out in outs:
        rows = credits.get(out.id, [])
        track_rows = [row for row in rows if row["role"] != ROLE_ALBUM_ARTIST]
        # Models, not the dicts: the field is declared list[ArtistCreditOut]
        # and assignment is not validated.
        out.artists = [ArtistCreditOut(**row) for row in track_rows or rows]
