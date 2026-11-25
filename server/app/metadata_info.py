"""Info modals for artists, albums and songs: MusicBrainz for the facts,
Wikipedia for bios, ListenBrainz for listener counts, similar artists and
tags - each optional, so a missing answer never means a blank modal.

Every provider answer is cached server-wide (app.external_cache); the
merged result is computed per request since it's cheap and depends on what
the viewer owns.
"""

from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.access import accessible_root_ids
from app.albums import _date, refresh_album_details, section_for
from app.artist_credits import normalize_artist_name
from app.custom_art import get_custom_art
from app.external_cache import cached
from app.library_cache import bump_library_version
from app.listenbrainz_data import (
    artist_popularity,
    listenbrainz_url,
    online_allowed,
    recording_popularity,
    release_group_popularity,
    similar_artist_rows,
)
from app.metadata_providers import musicbrainz, wikipedia
from app.metadata_providers.listenbrainz import first_mbid
from app.models import (
    Album,
    Artist,
    MediaItem,
    PlayHistory,
    TrackArtist,
    User,
)

MB_TTL = timedelta(days=30)


def _mb(db: Session, kind: str, key: str, fetch, *, refresh: bool = False, allow_fetch: bool = True):
    """A cached MusicBrainz lookup; None when it failed and nothing is cached."""

    def run():
        result = fetch()
        if result.error:
            return None
        return result.value or {}

    return cached(db, "musicbrainz", kind, key, run, ttl=MB_TTL, refresh=refresh, allow_fetch=allow_fetch)


def mb_artist(db: Session, mbid: str, *, refresh: bool = False) -> dict:
    return _mb(db, "artist", mbid, lambda: musicbrainz.lookup_artist(mbid), refresh=refresh) or {}


def mb_release(db: Session, mbid: str, *, refresh: bool = False) -> dict:
    return _mb(db, "release", mbid, lambda: musicbrainz.lookup_release(mbid), refresh=refresh) or {}


def mb_release_group(db: Session, mbid: str, *, refresh: bool = False) -> dict:
    return _mb(
        db, "release-group", mbid, lambda: musicbrainz.lookup_release_group(mbid), refresh=refresh
    ) or {}


# How many tags an info dialog lists.
INFO_TAGS = 12


def _tag_names(entity: dict) -> list[str]:
    """A MusicBrainz entity's genres and tags, most voted first, each once,
    at most INFO_TAGS."""
    rows: list[Any] = [*(entity.get("genres") or []), *(entity.get("tags") or [])]
    names = [
        row["name"]
        for row in sorted(rows, key=lambda row: -(row.get("count") or 0) if isinstance(row, dict) else 0)
        if isinstance(row, dict) and row.get("name")
    ]
    return list(dict.fromkeys(names))[:INFO_TAGS]


# --- artist matching -------------------------------------------------------


def find_artist(db: Session, name: str) -> Artist | None:
    return db.execute(
        select(Artist).where(Artist.normalized_name == normalize_artist_name(name))
    ).scalar_one_or_none()


def visible_artist(db: Session, user: User, name: str) -> Artist | None:
    """`find_artist`, but only when the user can see at least one of the
    artist's tracks. Artists are shared rows, so an unscoped match would
    reveal (through the canonical name, MBID and lock state) that someone's
    private library holds the artist; callers then answer as for an unknown
    name."""
    artist = find_artist(db, name)
    if artist is None:
        return None
    seen = db.execute(
        select(TrackArtist.media_item_id)
        .join(MediaItem, MediaItem.id == TrackArtist.media_item_id)
        .where(
            TrackArtist.artist_id == artist.id,
            MediaItem.library_root_id.in_(accessible_root_ids(db, user)),
        )
        .limit(1)
    ).first()
    return artist if seen is not None else None


def artist_candidates(db: Session, name: str) -> list[dict]:
    """MusicBrainz artists that could be `name`, best first, for Fix match."""
    found = _mb(db, "artist-search", normalize_artist_name(name), lambda: musicbrainz.search_artists(name, 10))
    return [
        {
            "mbid": row.get("id"),
            "name": row.get("name"),
            "disambiguation": row.get("disambiguation"),
            "type": row.get("type"),
            "country": row.get("country"),
            "begin": (row.get("life-span") or {}).get("begin"),
            "score": row.get("score"),
        }
        for row in (found or {}).get("artists") or []
        if isinstance(row, dict) and row.get("id")
    ]


def resolve_artist_mbid(db: Session, artist: Artist) -> str | None:
    """The artist's MBID: what tags/enrichment set, else the best MusicBrainz
    name match - an exact name preferred, and among several exact names the
    one whose MBID the library's own tracks carry. A hand-fixed match is
    never second-guessed."""
    if artist.mbid:
        return artist.mbid
    candidates = artist_candidates(db, artist.name)
    exact = [c for c in candidates if normalize_artist_name(c["name"] or "") == artist.normalized_name]
    if not exact:
        return None
    owned = set(
        db.execute(
            select(MediaItem.musicbrainz_artist_mbid)
            .join(TrackArtist, TrackArtist.media_item_id == MediaItem.id)
            .where(TrackArtist.artist_id == artist.id, MediaItem.musicbrainz_artist_mbid.is_not(None))
        ).scalars()
    )
    chosen = next((c for c in exact if c["mbid"] in owned), None)
    if chosen is None:
        # Several artists share the name and none is confirmed: only trust a
        # clear search winner rather than guess.
        if len(exact) > 1 and (exact[0].get("score") or 0) - (exact[1].get("score") or 0) < 5:
            return None
        chosen = exact[0]
    artist.mbid = chosen["mbid"]
    db.commit()
    return artist.mbid


def fix_artist_match(db: Session, artist: Artist, mbid: str | None) -> None:
    """Pin the artist to a MusicBrainz artist by hand (None unpins it and
    lets automatic matching try again). Cached info is refetched."""
    artist.mbid = mbid or None
    artist.mbid_locked = bool(mbid)
    artist.info_json = None
    artist.info_fetched_at = None
    db.commit()
    # The artist listings show the MBID-derived matching; drop cached pages.
    bump_library_version()


# --- artist info -----------------------------------------------------------


_LINK_LABELS = {
    "official homepage": "Official site",
    "wikipedia": "Wikipedia",
    "wikidata": "Wikidata",
    "bandcamp": "Bandcamp",
    "discogs": "Discogs",
    "youtube": "YouTube",
    "soundcloud": "SoundCloud",
    "allmusic": "AllMusic",
    "last.fm": "Last.fm",
    "listenbrainz": "ListenBrainz",
    "songkick": "Songkick",
    "streaming": "Streaming",
    "free streaming": "Streaming",
    "purchase for download": "Buy",
    "social network": "Social",
    "lyrics": "Lyrics",
    "IMDb": "IMDb",
}


def _links(relations: list) -> list[dict]:
    links, seen = [], set()
    for rel in relations or []:
        if not isinstance(rel, dict) or rel.get("target-type") != "url":
            continue
        url = ((rel.get("url") or {}).get("resource") or "").strip()
        if not url.startswith("http") or url in seen or rel.get("ended"):
            continue
        seen.add(url)
        kind = rel.get("type") or "link"
        label = _LINK_LABELS.get(kind, kind.capitalize())
        if kind == "social network":
            host = url.split("/")[2].removeprefix("www.")
            label = host.split(".")[0].capitalize()
        links.append({"type": kind, "label": label, "url": url})
    return links


def _members(relations: list, in_library: set[str]) -> tuple[list[dict], list[dict]]:
    """(members of this group, groups this artist is a member of)."""
    members, member_of = [], []
    for rel in relations or []:
        if not isinstance(rel, dict) or rel.get("type") != "member of band":
            continue
        other = rel.get("artist") or {}
        if not other.get("name"):
            continue
        entry = {
            "name": other["name"],
            "mbid": other.get("id"),
            "begin": rel.get("begin"),
            "end": rel.get("end"),
            "ended": bool(rel.get("ended")),
            "attributes": [a for a in rel.get("attributes") or [] if isinstance(a, str)],
            "in_library": normalize_artist_name(other["name"]) in in_library,
        }
        (members if rel.get("direction") == "backward" else member_of).append(entry)
    return members, member_of


def _library_artist_keys(db: Session, user: User, names: list[str | None]) -> set[str]:
    keys = {normalize_artist_name(n) for n in names if n}
    if not keys:
        return set()
    return set(
        db.execute(
            select(Artist.normalized_name)
            .join(TrackArtist, TrackArtist.artist_id == Artist.id)
            .join(MediaItem, MediaItem.id == TrackArtist.media_item_id)
            .where(
                Artist.normalized_name.in_(keys),
                MediaItem.library_root_id.in_(accessible_root_ids(db, user)),
            )
            .distinct()
        ).scalars()
    )


def artist_info(
    db: Session, user: User, name: str, *, refresh: bool = False, scoped: bool = True
) -> dict:
    """`scoped=False` is for the admin's root-scoped artist-info task only:
    it resolves the artist whether or not `user` can see its tracks, so a
    private root the admin has not granted themselves still gets its MBIDs.
    The API endpoints always use the scoped default."""
    artist = visible_artist(db, user, name) if scoped else find_artist(db, name)
    display = artist.name if artist else name
    mbid = (resolve_artist_mbid(db, artist) if artist else None)
    mb: dict = mb_artist(db, mbid, refresh=refresh) if mbid else {}
    online = online_allowed(user)
    # ListenBrainz is only asked with a well-formed MBID (tag values can hold several).
    lb_mbid = first_mbid(mbid)
    popularity = (artist_popularity(db, lb_mbid, refresh=refresh, allow_fetch=online) or {}) if lb_mbid else {}
    similar_names = _info_similar_artists(db, lb_mbid, refresh=refresh, online=online)

    relations = mb.get("relations") or []
    links = _links(relations)
    member_names = [((r.get("artist") or {}).get("name")) for r in relations if isinstance(r, dict)]
    in_library = _library_artist_keys(db, user, [*similar_names, *member_names])
    members, member_of = _members(relations, in_library)

    bio = None
    if links:
        wiki = cached(
            db,
            "wikipedia",
            "summary",
            mbid or normalize_artist_name(display),
            # {} (no article) is cached; None (a failed request) is not.
            lambda: wikipedia.summary_from_links(links),
            ttl=MB_TTL,
            refresh=refresh,
        )
        if wiki:
            bio = {"text": wiki["text"], "html": False, "source": "Wikipedia", "url": wiki.get("url")}
    if bio is None and mb.get("annotation"):
        bio = {"text": mb["annotation"], "html": False, "source": "MusicBrainz", "url": None}

    life = mb.get("life-span") or {}
    area = (mb.get("area") or {}).get("name")
    begin_area = (mb.get("begin-area") or {}).get("name")
    mb_tags = _tag_names(mb)
    return {
        "name": display,
        "mbid": mbid,
        "mbid_locked": bool(artist and artist.mbid_locked),
        "disambiguation": mb.get("disambiguation") or None,
        "type": mb.get("type"),
        "gender": mb.get("gender"),
        "country": mb.get("country"),
        "area": area,
        "begin_area": begin_area,
        "begin": life.get("begin"),
        "end": life.get("end"),
        "ended": bool(life.get("ended")),
        "listeners": popularity.get("listeners"),
        "playcount": popularity.get("playcount"),
        "listenbrainz_url": listenbrainz_url("artist", lb_mbid),
        "tags": mb_tags,
        "bio": bio,
        "members": members,
        "member_of": member_of,
        "links": links,
        "similar": [
            {"name": n, "in_library": normalize_artist_name(n) in in_library} for n in similar_names
        ],
        "image_credit": _image_credit(db, display, mbid),
        "sources": [
            s for s, used in (("MusicBrainz", bool(mb)), ("ListenBrainz", bool(popularity or similar_names))) if used
        ],
    }


def artist_image(db: Session, mbid: str, *, refresh: bool = False, allow_fetch: bool = True) -> dict | None:
    """The artist's photo on Wikimedia Commons (through its MusicBrainz
    Wikidata link): {"url", "thumb", "author", "license", "license_url",
    "page_url"}, or None. Cached; the credit is shown wherever it is used."""
    def fetch():
        mb = _mb(db, "artist", mbid, lambda: musicbrainz.lookup_artist(mbid))
        if mb is None:
            return None  # MusicBrainz failed: not "no photo", ask again later
        return wikipedia.image_from_links(_links(mb.get("relations") or []))

    return cached(
        db, "wikimedia", "artist-image", mbid, fetch, ttl=MB_TTL, refresh=refresh, allow_fetch=allow_fetch
    ) or None


def _image_credit(db: Session, name: str, mbid: str | None) -> dict | None:
    """The credit stored with the artist's picture that is actually shown:
    the library artist's own art, else (a wanted artist) its catalog art.
    Only an image whose licence asks for credit (Wikimedia Commons) has one."""
    row = get_custom_art(db, "artist", name)
    if row is None and mbid:
        row = get_custom_art(db, "catalog_artist", mbid)
    return row.credit_json if row is not None and row.credit_json else None


# How many similar artists the info dialog lists.
INFO_SIMILAR_ARTISTS = 20


def _info_similar_artists(db: Session, mbid: str | None, *, refresh: bool, online: bool) -> list[str]:
    """Similar artists, best first, from ListenBrainz by the artist's
    MusicBrainz ID (cached; looked up in the background like the dialog's
    other online data). Nothing without an MBID; only what is cached for a
    user who turned ListenBrainz off."""
    if not mbid:
        return []
    rows = similar_artist_rows(db, mbid, refresh=refresh, allow_fetch=online) or []
    return [row["name"] for row in rows if isinstance(row, dict) and row.get("name")][:INFO_SIMILAR_ARTISTS]


# --- album info ------------------------------------------------------------


def find_album(db: Session, artist: str | None, album: str) -> Album | None:
    return db.execute(
        select(Album).where(
            Album.owner_key == normalize_artist_name(artist or ""),
            Album.title_key == normalize_artist_name(album),
        )
    ).scalar_one_or_none()


def visible_album(db: Session, user: User, artist: str | None, album: str) -> Album | None:
    """`find_album` limited to albums with a track in a root the user can
    see (see `visible_artist`)."""
    found = find_album(db, artist, album)
    if found is None:
        return None
    seen = db.execute(
        select(MediaItem.id)
        .where(
            MediaItem.album_id == found.id,
            MediaItem.library_root_id.in_(accessible_root_ids(db, user)),
        )
        .limit(1)
    ).first()
    return found if seen is not None else None


def album_editions(db: Session, release_group_mbid: str, *, refresh: bool = False) -> list[dict]:
    group = mb_release_group(db, release_group_mbid, refresh=refresh)
    out = []
    for release in group.get("releases") or []:
        if not isinstance(release, dict) or not release.get("id"):
            continue
        media = release.get("media") or []
        out.append(
            {
                "mbid": release["id"],
                "title": release.get("title"),
                "date": release.get("date"),
                "country": release.get("country"),
                "status": release.get("status"),
                "disambiguation": release.get("disambiguation") or None,
                "track_count": sum((m.get("track-count") or 0) for m in media if isinstance(m, dict)) or None,
                "format": ", ".join(sorted({fmt for m in media if isinstance(m, dict) and (fmt := m.get("format"))})) or None,
            }
        )
    out.sort(key=lambda r: (r.get("date") or "9999", r.get("country") or ""))
    return out


def album_info(db: Session, user: User, artist: str | None, album_title: str, *, refresh: bool = False) -> dict:
    album = visible_album(db, user, artist, album_title)
    group_mbid = album.release_group_mbid if album else None
    popularity = (
        (release_group_popularity(db, group_mbid, refresh=refresh, allow_fetch=online_allowed(user)) or {})
        if group_mbid
        else {}
    )
    group = mb_release_group(db, group_mbid, refresh=refresh) if group_mbid else {}
    release = mb_release(db, album.release_mbid, refresh=refresh) if album and album.release_mbid else {}
    secondary = (album.secondary_types_json if album else None) or []
    label_info = [row for row in release.get("label-info") or [] if isinstance(row, dict)]
    labels = [
        {"label": (row.get("label") or {}).get("name"), "catalog_number": row.get("catalog-number")}
        for row in label_info
        if (row.get("label") or {}).get("name") or row.get("catalog-number")
    ]
    if not labels and album and (album.label or album.catalog_number):
        labels = [{"label": album.label, "catalog_number": album.catalog_number}]
    return {
        "artist": artist,
        "album": album_title,
        "release_group_mbid": album.release_group_mbid if album else None,
        "release_mbid": album.release_mbid if album else None,
        "primary_type": album.primary_type if album else None,
        "secondary_types": secondary,
        "section": section_for(album.primary_type if album else None, secondary),
        "original_release_date": album.first_release_date if album else None,
        "edition_date": album.release_date if album else None,
        "country": album.country if album else None,
        "barcode": release.get("barcode") or None,
        "labels": labels,
        "status": release.get("status"),
        "listeners": popularity.get("listeners"),
        "playcount": popularity.get("playcount"),
        "listenbrainz_url": listenbrainz_url("album", group_mbid),
        "tags": _tag_names(group),
        "wiki": None,
        "editions": album_editions(db, album.release_group_mbid, refresh=refresh)
        if album and album.release_group_mbid
        else [],
        "release_locked": bool(album and album.release_locked),
    }


def choose_edition(db: Session, album: Album, release_mbid: str | None) -> None:
    """Pin the album to one edition of its release group (None unpins)."""
    album.release_mbid = release_mbid
    album.release_locked = bool(release_mbid)
    if release_mbid:
        release = mb_release(db, release_mbid)
        album.release_date = _pad(release.get("date"))
        album.country = release.get("country")
        label_info = [row for row in release.get("label-info") or [] if isinstance(row, dict)]
        if label_info:
            album.label = (label_info[0].get("label") or {}).get("name")
            album.catalog_number = label_info[0].get("catalog-number")
    else:
        refresh_album_details(db, album)
    db.commit()
    # The album listings show the edition's date, label and country.
    bump_library_version()


def _pad(value: str | None) -> str | None:
    return _date(value)


# --- track info ------------------------------------------------------------

# Recording relationship types worth showing as credits, and their labels.
_CREDIT_LABELS = {
    "producer": "Producer",
    "co-producer": "Co-producer",
    "mix": "Mixing",
    "mix-DJ": "DJ mix",
    "recording": "Recording engineer",
    "engineer": "Engineer",
    "audio": "Audio engineer",
    "mastering": "Mastering",
    "remixer": "Remixer",
    "programming": "Programming",
    "vocal": "Vocals",
    "instrument": "Instruments",
    "performer": "Performer",
    "performing orchestra": "Orchestra",
    "conductor": "Conductor",
    "arranger": "Arranger",
    "orchestrator": "Orchestrator",
    "composer": "Composer",
    "lyricist": "Lyricist",
    "writer": "Writer",
}


def credits_from_recording(recording: dict) -> list[dict]:
    """[{"role", "name", "detail"}] from a recording's artist relations and
    its works' composer/lyricist/writer relations."""
    out, seen = [], set()

    def add(rel: dict) -> None:
        kind = rel.get("type")
        name = (rel.get("artist") or {}).get("name")
        if kind not in _CREDIT_LABELS or not name:
            return
        attributes = [a for a in rel.get("attributes") or [] if isinstance(a, str)]
        detail = ", ".join(attributes) or None
        key = (kind, name, detail)
        if key in seen:
            return
        seen.add(key)
        out.append({"role": _CREDIT_LABELS[kind], "name": name, "detail": detail})

    for rel in recording.get("relations") or []:
        if not isinstance(rel, dict):
            continue
        if rel.get("target-type") == "artist":
            add(rel)
        elif rel.get("target-type") == "work":
            for work_rel in (rel.get("work") or {}).get("relations") or []:
                if isinstance(work_rel, dict) and work_rel.get("target-type") == "artist":
                    add(work_rel)
    order = list(_CREDIT_LABELS.values())
    out.sort(key=lambda c: (order.index(c["role"]), c["name"]))
    return out


def track_info(db: Session, user: User, item: MediaItem, *, refresh: bool = False) -> dict:
    """The song's facts (MusicBrainz), ListenBrainz's global plays and
    listeners, and the user's own plays here."""
    recording = item.musicbrainz_metadata_json or {}
    mbid = first_mbid(item.musicbrainz_recording_mbid)
    popularity = (
        (recording_popularity(db, mbid, refresh=refresh, allow_fetch=online_allowed(user)) or {}) if mbid else {}
    )
    album = db.get(Album, item.album_id) if item.album_id else None
    plays = db.execute(
        select(func.count())
        .select_from(PlayHistory)
        .where(PlayHistory.user_id == user.id, PlayHistory.media_item_id == item.id)
    ).scalar()
    works = [
        (rel.get("work") or {}).get("title")
        for rel in recording.get("relations") or []
        if isinstance(rel, dict) and rel.get("target-type") == "work"
    ]
    return {
        "id": item.id,
        "title": item.title,
        "artist": item.artist,
        "album": item.album,
        "listeners": popularity.get("listeners"),
        "playcount": popularity.get("playcount"),
        "library_playcount": plays or 0,
        "listenbrainz_url": listenbrainz_url("track", mbid),
        "tags": _tag_names(recording),
        "wiki": None,
        "recording_mbid": item.musicbrainz_recording_mbid,
        "artist_mbid": item.musicbrainz_artist_mbid,
        "release_mbid": album.release_mbid if album else None,
        "release_group_mbid": album.release_group_mbid if album else None,
        "isrc": item.musicbrainz_isrc or next(iter(recording.get("isrcs") or []), None),
        "original_release_date": album.first_release_date if album else None,
        "release_date": item.release_date,
        "duration_seconds": item.duration_seconds,
        "credits": credits_from_recording(recording),
        "works": [w for w in works if w],
    }

