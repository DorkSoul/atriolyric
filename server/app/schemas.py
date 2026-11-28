from __future__ import annotations

import json
from datetime import datetime
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from app.models import EQ_BANDS, MediaKind


def _printable_username(value: str) -> str:
    # Control and invisible characters (newlines, zero-width spaces...) have
    # no place in a name and are how a username forges a log line. Plain
    # spaces at the ends are trimmed by the handlers. Existing
    # accounts are untouched: only create, rename and setup validate.
    if not value.isprintable():
        raise ValueError("Username cannot contain control or invisible characters")
    return value


# A MusicBrainz id is always a UUID; anything else is refused up front.
MBID_PATTERN = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"


# One shared rule for every place a username is chosen.
NewUsername = Annotated[
    str, Field(min_length=1, max_length=100), AfterValidator(_printable_username)
]


class OutModel(BaseModel):
    """Base for response models. A field with a default is still always in
    the JSON FastAPI sends, so the OpenAPI schema marks it required and the
    generated web/src/api-types.ts says `x: T` rather than `x?: T`. Schema
    only: validation and serialization are unchanged. Not for models sent
    with response_model_exclude_unset, where a defaulted key can be absent
    (WantEntryOut, DiscoveryRefreshOut)."""

    model_config = ConfigDict(json_schema_serialization_defaults_required=True)


class ArtistCreditOut(OutModel):
    name: str
    # The roles of app.artist_credits.
    role: Literal["main", "featured", "album_artist"]


class MediaItemOut(OutModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    library_root_id: int
    kind: MediaKind
    title: str
    artist: str | None
    album: str | None
    genre: str | None
    track_number: int | None
    performer: str | None = None
    description: str | None = None
    duration_seconds: float | None
    replaygain_track_gain: float | None = None
    replaygain_album_gain: float | None = None
    # Linear track peak (1.0 = full scale) for headroom-aware levelling.
    replaygain_track_peak: float | None = None
    is_favorite: bool = False
    is_disliked: bool = False
    # "album" or "artist" when the song's own state is neutral but its album
    # or artist is disliked - it's left out of mixes for that reason.
    excluded_by: Literal["album", "artist"] | None = None
    # Only set when this item is returned as part of a playlist's own
    # detail view - identifies *this occurrence* in that playlist (not the
    # track itself), so "remove from playlist" can target the right row
    # even if the same track appears in the playlist more than once.
    playlist_item_id: int | None = None
    # Library-wide uploaded art. custom_art is true when this item has art of
    # its own (so "Reset art" applies); art_version is set whenever custom
    # art (own, or cascaded from its album) is in effect - epoch ms to
    # cache-bust the art URL with.
    custom_art: bool = False
    art_version: int | None = None
    # Every credited artist, in tag order (see app.artist_credits); `artist`
    # stays the display string as tagged.
    artists: list[ArtistCreditOut] = []
    album_artist: str | None = None
    # "FLAC 16/44", "MP3 320", "Opus 128" (app.audio_quality.format_label);
    # the upper-cased file extension until a scan has read the file's
    # stream header; None when neither is known.
    format_label: str | None = None
    # The year the album page dates this track by: the album's original
    # release, else the file's own date tag (app.library_query
    # .track_release_date). Only filled by the Songs list (/library/tracks).
    year: int | None = None
    # How many times the requesting user has played this track - only
    # filled by the Songs list (/library/tracks), null elsewhere.
    play_count: int | None = None


class LibraryRootSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    display_name: str


class BrowseDirOut(OutModel):
    name: str
    path: str
    is_ignored: bool = False
    ignored_directly: bool = False
    ignored_by: str | None = None


class BrowseOut(OutModel):
    root_id: int
    path: str
    parent_path: str | None
    directories: list[BrowseDirOut]
    items: list[MediaItemOut]


class FileSearchItemOut(MediaItemOut):
    relative_path: str


class FileSearchOut(OutModel):
    root_id: int
    folders: list[BrowseDirOut]
    items: list[FileSearchItemOut]
    truncated: bool = False


class IgnoredFolderIn(BaseModel):
    path: str = Field(min_length=1, max_length=4096)


class IgnoredFolderOut(OutModel):
    path: str


class AlbumOut(OutModel):
    # The album's owner as displayed: album artist tag, else track artist.
    artist: str | None
    album: str
    track_count: int
    representative_media_item_id: int
    # The owners split into individual artists, for the multi-artist picker.
    artists: list[ArtistCreditOut] = []
    # MusicBrainz release group type ("Album", "EP", "Single", ...) and
    # secondary types ("Live", "Compilation", ...); `section` is the artist
    # page heading it belongs under (app.albums.section_for).
    primary_type: str | None = None
    secondary_types: list[str] = []
    section: str = "Album"
    # Original release date ("YYYY-MM-DD"), else the files' date tag.
    release_date: str | None = None
    # MusicBrainz release group, once the tracks have been matched - the
    # key for its full tracklist (/api/catalog/release-group/{mbid}).
    release_group_mbid: str | None = None
    # Route key for /api/art/{album|artist}/{art_key}; set on list responses.
    art_key: str | None = None
    custom_art: bool = False
    art_version: int | None = None


class ArtistOut(OutModel):
    artist: str
    track_count: int
    album_count: int
    representative_media_item_id: int
    # Route key for /api/art/{album|artist}/{art_key}; set on list responses.
    art_key: str | None = None
    custom_art: bool = False
    art_version: int | None = None


class VideoFolderOut(OutModel):
    folder: str
    path: str
    video_count: int
    representative_media_item_id: int


class LabelOut(OutModel):
    name: str
    track_count: int


class LabelsOut(OutModel):
    genres: list[LabelOut]
    tags: list[LabelOut]


class AllLabelsOut(OutModel):
    """Every genre and tag with the ids of the tracks that carry it."""

    genres: dict[str, list[int]]
    tags: dict[str, list[int]]


class MixOptionOut(OutModel):
    kind: Literal["genre", "tag"]
    name: str
    track_count: int


class MixOptionIn(BaseModel):
    kind: str = Field(max_length=32)
    name: str = Field(max_length=1000)


class SpecifiedMixIn(BaseModel):
    """Body of POST /api/library/mixes/specified. A single {kind, name}
    form is also accepted and counts as one include."""

    kind: str | None = Field(default=None, max_length=32)
    name: str | None = Field(default=None, max_length=1000)
    include: list[MixOptionIn] = Field(default=[], max_length=200)
    exclude: list[MixOptionIn] = Field(default=[], max_length=200)


class MixPlaylistOut(OutModel):
    playlist_id: int
    genre: str
    name: str
    track_count: int
    # Materialized collage ids for the Home rail - the client renders these
    # directly instead of fetching each mix's full playlist. Empty when the
    # mix predates the snapshot or has no items.
    preview_media_item_ids: list[int] = []
    # A temporary mix the user asked for by name (POST /mixes/specified).
    # Home keeps these visible on top of its featured-mix cap instead of
    # counting them against it.
    is_specified: bool = False
    # Only on the mix POST /mixes/specified has just built: how many of its
    # tracks carry every chosen include. Fewer than track_count means the
    # rest carry only some of them, which the app tells the user.
    full_match_count: int | None = None
    custom_art: bool = False
    art_version: int | None = None


class ExploreTrackOut(OutModel):
    artist: str
    title: str | None
    artwork_url: str | None
    preview_url: str
    view_url: str | None


class DiscoveryExploreOut(OutModel):
    seed_artists: list[str]
    results: list[ExploreTrackOut]


class DiscoveryStateOut(OutModel):
    discovery_seed_artists: list[str]
    discovery_similar_artists: list[str]
    discovery_tracks_added: int
    discovery_updated_at: datetime | None
    explore_seed_artists: list[str]
    explore_results: list[ExploreTrackOut]
    # True while missing Deezer previews are looked up in the background:
    # ask again shortly (Deezer answers are kept in memory only).
    explore_pending: bool = False
    explore_updated_at: datetime | None


class DiscoverySettingsOut(OutModel):
    auto_refresh_enabled: bool
    auto_refresh_hour: int


class DiscoverySettingsIn(BaseModel):
    auto_refresh_enabled: bool
    auto_refresh_hour: int = Field(ge=0, le=23)


# Every value Playlist.auto_kind has ever been given (the column is plain text).
PlaylistAutoKind = Literal["discovery", "generated", "mix", "favorites"]


class PlaylistOut(OutModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    description: str | None = None
    created_at: datetime
    item_count: int = 0
    is_auto_generated: bool = False
    auto_kind: PlaylistAutoKind | None = None
    # Up to 4 media item ids (distinct artists where possible) for a
    # cover-art collage - same idea as a genre mix's collage on Home,
    # rather than a generic icon for every playlist regardless of content.
    preview_media_item_ids: list[int] = []
    custom_art: bool = False
    art_version: int | None = None


class LibrarySearchOut(OutModel):
    """Home search: up to SEARCH_SECTION_LIMIT matches per section."""

    artists: list[ArtistOut]
    albums: list[AlbumOut]
    songs: list[MediaItemOut]
    playlists: list[PlaylistOut]


class PlaylistCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class PlaylistUpdate(BaseModel):
    """Only the fields sent are changed; an empty description clears it."""

    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=1000)


class GeneratePlaylistIn(BaseModel):
    media_item_id: int


class GenerateCollectionPlaylistIn(BaseModel):
    label: str = Field(max_length=200)
    artist: str | None = Field(default=None, max_length=1000)
    album: str | None = Field(default=None, max_length=1000)


class GeneratePlaylistOut(OutModel):
    playlist_id: int
    name: str
    track_count: int


class SaveAutoPlaylistIn(BaseModel):
    name: str | None = Field(default=None, max_length=200)


class PlaylistItemsAdd(BaseModel):
    media_item_ids: list[int] = Field(min_length=1, max_length=500)
    position: int | None = Field(default=None, ge=0)  # None = append; 0 = head


class PlaylistDetailOut(OutModel):
    id: int
    name: str
    description: str | None = None
    created_at: datetime
    items: list[MediaItemOut]
    # How many items the playlist shows in all; `items` is one page of them
    # when GET /api/playlists/{id} is given `limit`/`offset`.
    total_count: int = 0
    custom_art: bool = False
    art_version: int | None = None


class PlaylistReorder(BaseModel):
    ordered_item_ids: list[int] = Field(max_length=5000)


class PreferenceIn(BaseModel):
    state: Literal["none", "liked", "disliked"]


class PreferenceOut(OutModel):
    state: Literal["none", "liked", "disliked"]


class CollectionPreferenceOut(OutModel):
    kind: Literal["album", "artist"]
    artist: str
    album: str | None = None
    state: Literal["none", "liked", "disliked"]


PLAY_SOURCE_KINDS = {"album", "artist", "mix", "playlist", "radio", "library"}


class PlaySourceIn(BaseModel):
    kind: str = Field(max_length=32)
    label: str = Field(max_length=200)
    payload: dict = Field(default_factory=dict, max_length=10)

    @model_validator(mode="after")
    def validate_payload(self) -> PlaySourceIn:
        if self.kind not in PLAY_SOURCE_KINDS:
            raise ValueError("Invalid play source kind")
        payload = self.payload
        # Stored as JSON per play; the real payloads are a few short strings.
        if len(json.dumps(payload)) > 4000:
            raise ValueError("Play source payload is too large")
        valid = False
        if self.kind == "album":
            valid = isinstance(payload.get("album"), str) and (
                payload.get("artist") is None or isinstance(payload.get("artist"), str)
            )
        elif self.kind == "artist":
            valid = isinstance(payload.get("artist"), str)
        elif self.kind == "playlist":
            valid = type(payload.get("playlist_id")) is int
        elif self.kind == "radio":
            valid = type(payload.get("station_id")) is int
        elif self.kind == "mix":
            valid = isinstance(payload.get("genre"), str)
        elif self.kind == "library":
            filters = payload.get("filters")
            valid = isinstance(filters, dict) and all(
                key in {"q", "genre", "tag", "artist", "album"}
                and (value is None or isinstance(value, str))
                for key, value in filters.items()
            )
        if not valid:
            raise ValueError("Invalid play source payload")
        return self


class PlaySourceOut(OutModel):
    kind: str
    label: str
    payload: dict
    updated_at: datetime
    art_media_item_id: int | None = None
    preview_media_item_ids: list[int] = []


# Events per debug batch the web app may send (app.routers.client_log).
MAX_DEBUG_EVENTS = 50


# --- for app.routers.client_log ---------------------------------------------------------


class ClientLogIn(BaseModel):
    level: Literal["error", "warning"] = "error"
    message: str = Field(max_length=500)
    source: str | None = Field(default=None, max_length=300)
    stack: str | None = Field(default=None, max_length=2000)
    page: str | None = Field(default=None, max_length=300)


class ClientDebugEvent(BaseModel):
    at: str = Field(max_length=40)
    event: str = Field(max_length=80)
    fields: dict[str, str | int | float | bool | None] = Field(
        default_factory=dict, max_length=60
    )


class ClientDebugIn(BaseModel):
    session: str = Field(max_length=40)
    events: list[ClientDebugEvent] = Field(max_length=MAX_DEBUG_EVENTS)


# --- for app.routers.art ----------------------------------------------------------------


class CandidateIn(BaseModel):
    candidate_id: str = Field(min_length=1, max_length=4096)


# --- for app.routers.auth ---------------------------------------------------------------


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=1024)


class UserOut(OutModel):
    model_config = {"from_attributes": True}

    id: int
    username: str
    is_admin: bool
    accent_color: str | None = None
    volume_leveling: bool = False
    eq_preset: str = "flat"
    eq_gains: list[float] = [0.0] * EQ_BANDS
    # Notice ids the user chose "Never show again" for.
    dismissed_notices: list[str] = []
    online_recommendations: bool = True


class NoticeIn(BaseModel):
    notice: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9_]+$")


class AccentColorIn(BaseModel):
    # A CSS hex color (#rgb or #rrggbb). Custom (non-preset) colors are
    # allowed on purpose - the swatch grid is a UI affordance, not a
    # server-side allowlist.
    accent_color: str = Field(pattern=r"^#[0-9a-fA-F]{3}([0-9a-fA-F]{3})?$")


class AudioSettingsOut(OutModel):
    volume_leveling: bool
    eq_preset: str
    eq_gains: list[float]


class AudioSettingsIn(BaseModel):
    """Only the fields sent are changed. eq_gains is exactly ten band gains
    in dB (31 Hz - 16 kHz); values beyond +/-12 dB are clamped."""

    volume_leveling: bool | None = None
    eq_preset: str | None = Field(default=None, pattern=r"^[A-Za-z0-9 _-]{1,32}$")
    eq_gains: (
        list[Annotated[float, Field(allow_inf_nan=False)]] | None
    ) = Field(default=None, min_length=EQ_BANDS, max_length=EQ_BANDS)


class PrivacySettings(BaseModel):
    # Ask ListenBrainz about what I play (similar tracks and artists) for
    # recommendations. Off: plays and look-ups stay local; the user's own
    # linked ListenBrainz account (scrobbling) is unaffected.
    online_recommendations: bool


class UsernameChangeIn(BaseModel):
    current_password: str = Field(min_length=1, max_length=1024)
    new_username: NewUsername


class PasswordChangeIn(BaseModel):
    current_password: str = Field(min_length=1, max_length=1024)
    new_password: str = Field(min_length=8, max_length=1024)


# --- for app.routers.admin --------------------------------------------------------------


class UserCreateIn(BaseModel):
    username: NewUsername
    password: str = Field(min_length=8, max_length=1024)
    is_admin: bool = False


class UserUpdateIn(BaseModel):
    is_admin: bool


class PasswordResetIn(BaseModel):
    new_password: str = Field(min_length=8, max_length=1024)


class AdminUserOut(OutModel):
    model_config = {"from_attributes": True}

    id: int
    username: str
    is_admin: bool
    accent_color: str | None = None
    accessible_root_ids: list[int] = []


class LibraryRootIn(BaseModel):
    path: str = Field(min_length=1, max_length=4096)
    display_name: str = Field(min_length=1, max_length=200)
    is_shared: bool = True


class LibraryRootOut(OutModel):
    model_config = {"from_attributes": True}

    id: int
    path: str
    display_name: str
    is_shared: bool
    last_scanned_at: datetime | None = None
    last_scan_error: str | None = None
    active_task: LibraryTaskOut | None = None
    last_task: LibraryTaskOut | None = None


class LibraryTaskOut(OutModel):
    model_config = {"from_attributes": True}

    id: int
    kind: str
    status: str
    total: int
    processed: int
    added: int
    updated: int
    removed: int
    matched: int
    error: str | None = None
    started_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None
    stage: str | None = None
    missing_held: int = 0
    note: str | None = None
    items_per_second: float | None = None
    eta_seconds: int | None = None


class ActivityEventOut(OutModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    # Naive UTC, like every other timestamp the API returns.
    created_at: datetime
    kind: str
    level: Literal["info", "warning", "error"]
    actor: str | None = None
    summary: str
    root_id: int | None = None
    # The library folder's display name at the time, when the event is about one.
    context: str | None = None
    request_id: str | None = None


class ActivityLogOut(OutModel):
    # Newest first.
    events: list[ActivityEventOut]
    # Pass as before_id for the next (older) page; None on the last page.
    next_before_id: int | None = None
    # Every kind in the stored log, for the filter menu.
    kinds: list[str] = []


class LibraryRootUpdate(BaseModel):
    path: str | None = Field(default=None, min_length=1, max_length=4096)
    display_name: str | None = Field(default=None, min_length=1, max_length=200)
    is_shared: bool | None = None


class MetadataFetchIn(BaseModel):
    mode: Literal[
        "mbids", "musicbrainz_missing", "isrc_mbid", "musicbrainz_fallback",
        "listenbrainz_missing", "listenbrainz_all", "artist_info",
    ] = "musicbrainz_missing"


class TagEditIn(BaseModel):
    title: str | None = Field(default=None, max_length=1000)
    artist: str | None = Field(default=None, max_length=1000)
    album: str | None = Field(default=None, max_length=1000)
    genre: str | None = Field(default=None, max_length=1000)
    track_number: int | None = Field(default=None, ge=1, le=9999)


class ArtistSplitRulesIn(BaseModel):
    separators: list[Annotated[str, Field(max_length=100)]] = Field(max_length=50)
    exceptions: list[Annotated[str, Field(max_length=500)]] = Field(max_length=2000)


class BulkTagEditIn(TagEditIn):
    media_item_ids: list[int] = Field(max_length=500)


class BulkRemoveIn(BaseModel):
    media_item_ids: list[int] = Field(max_length=500)


# --- for app.routers.integrations -------------------------------------------------------


class ListenBrainzIn(BaseModel):
    # The user token from listenbrainz.org/settings: a UUID (spaces around
    # it, as pasted, are allowed and trimmed).
    token: str = Field(pattern=r"^\s*[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\s*$")


# --- for app.routers.metadata -----------------------------------------------------------


class ArtistMatchIn(BaseModel):
    name: str = Field(min_length=1, max_length=500)
    # None clears a hand-picked match.
    mbid: str | None = Field(default=None, pattern=MBID_PATTERN)


class AlbumEditionIn(BaseModel):
    artist: str | None = Field(default=None, max_length=500)
    album: str = Field(min_length=1, max_length=500)
    release_mbid: str | None = Field(default=None, pattern=MBID_PATTERN)


# --- for app.routers.radio --------------------------------------------------------------


class StationIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    stream_url: str = Field(min_length=1, max_length=2048)
    description: str | None = Field(default=None, max_length=280)
    is_shared: bool = True


class StationOut(OutModel):
    model_config = {"from_attributes": True}

    id: int
    name: str
    stream_url: str
    description: str | None
    provider: str | None
    is_shared: bool
    owner_user_id: int | None
    created_at: datetime
    is_favorite: bool = False


class WantListIn(BaseModel):
    artist: str = Field(min_length=1, max_length=1000)
    title: str = Field(min_length=1, max_length=1000)
    station_name: str | None = Field(default=None, max_length=200)


class WantListOut(OutModel):
    model_config = {"from_attributes": True}

    id: int
    artist: str
    title: str
    station_name: str | None
    created_at: datetime
    # The library track this song turned out to be, once a scan found it.
    media_item_id: int | None = None


# --- for app.routers.setup --------------------------------------------------------------


class SetupIn(BaseModel):
    code: str = Field(min_length=1, max_length=100)
    username: NewUsername
    password: str = Field(min_length=8, max_length=1024)


# --- for app.routers.stream -------------------------------------------------------------


class PlayedReport(BaseModel):
    """A play the client counted: a track or music video once it had been
    listened to (half the track or four minutes, whichever comes first), a
    plain video when it loaded."""

    # When this play began (ISO 8601; naive means UTC). Required. The
    # scrobble (ListenBrainz listen) timestamp, and what makes a repeated report of the same play a
    # no-op.
    started_at: datetime
    queue_item_id: int | None = None
    listened_seconds: float | None = Field(default=None, ge=0)


# How a listen ended; see app.listening for what each one means.
ListenEndReason = Literal["finished", "skipped", "stopped", "transferred", "error", "unknown"]
# What started it: the queue on its own (`auto`) or the user picking it.
ListenStartKind = Literal["auto", "user"]
# The play source the user started the queue from (the web app's
# PlaySourceKind without radio, which is never a listen).
ListenSourceKind = Literal["album", "artist", "mix", "playlist", "library"]

# A day: no single listen is longer, and no track or position larger than a week.
_LISTEN_MAX_SECONDS = 86_400.0
_MEDIA_MAX_SECONDS = 7 * 86_400.0


class ListenedReport(BaseModel):
    """How one listen of a track or music video on the device producing the
    sound ended. Sent once per listen, also for one that never counted as a
    play; `started_at` is the same value as that play's report."""

    started_at: datetime
    # When it was left (ISO 8601; naive means UTC). Default: when received.
    ended_at: datetime | None = None
    listened_seconds: float = Field(ge=0, le=_LISTEN_MAX_SECONDS)
    duration_seconds: float | None = Field(default=None, ge=0, le=_MEDIA_MAX_SECONDS)
    position_seconds: float | None = Field(default=None, ge=0, le=_MEDIA_MAX_SECONDS)
    end_reason: ListenEndReason
    start_kind: ListenStartKind | None = None
    source_kind: ListenSourceKind | None = None


# --- for app.routers.want_list ----------------------------------------------------------


class WantIn(BaseModel):
    kind: Literal["artist", "album", "track"]
    artist: str = Field(min_length=1, max_length=1000)
    # Album or song title; ignored for an artist.
    title: str | None = Field(default=None, max_length=1000)
    album: str | None = Field(default=None, max_length=1000)
    station_name: str | None = Field(default=None, max_length=200)
    artist_mbid: str | None = Field(default=None, pattern=MBID_PATTERN)
    release_group_mbid: str | None = Field(default=None, pattern=MBID_PATTERN)
    recording_mbid: str | None = Field(default=None, pattern=MBID_PATTERN)
    release_type: str | None = Field(default=None, max_length=64)
    # True records "not this one" under a wanted artist.
    excluded: bool = False


# --- simple acknowledgements -------------------------------------------------


class OkOut(OutModel):
    ok: bool


class RemovedCountOut(OutModel):
    removed: int


class UnrecordedArtOut(OutModel):
    """Library art whose source was not recorded (an
    upload, a file's picture, or an online pick - possibly Deezer's)."""

    count: int


class IsFavoriteOut(OutModel):
    is_favorite: bool


class SetupStatusOut(OutModel):
    needs_setup: bool


class AndroidAppOut(OutModel):
    version: str
    size: int


class VersionOut(OutModel):
    # None when no built frontend is served (development).
    version: str | None
    debug_log: bool
    # The Android app this server offers for download, if it carries one.
    android_app: AndroidAppOut | None = None
    # Changes when existing tracks' art may have (app.art_epoch); the web
    # app appends it to art addresses so day-long caches do not show old art.
    art_epoch: int = 0


class CustomArtResultOut(OutModel):
    ok: bool
    custom_art: bool
    art_version: int | None


class PlayedOut(OutModel):
    ok: bool
    # False when this play was already recorded (a retried report).
    recorded: bool


class ListenedOut(OutModel):
    ok: bool
    # False when this listen was already recorded (a retried report).
    recorded: bool


class NowPlayingOut(OutModel):
    title: str | None
    updated_at: float | None


# --- artist, album and song info (app.metadata_info) ------------------------
# Built from provider data: most fields are null when no provider knows them.


class InfoTextOut(OutModel):
    # Only sent when there is text; the field itself is null otherwise.
    text: str
    source: str


class ArtistBioOut(OutModel):
    text: str
    # Always False (Wikipedia and MusicBrainz text is plain); kept so
    # older apps keep reading the field.
    html: bool
    source: Literal["Wikipedia", "MusicBrainz"]
    url: str | None


class ArtistMemberOut(OutModel):
    name: str
    mbid: str | None
    begin: str | None
    end: str | None
    ended: bool
    attributes: list[str]
    in_library: bool


class ArtistLinkOut(OutModel):
    type: str
    label: str
    url: str


class SimilarArtistOut(OutModel):
    name: str
    in_library: bool


class ImageCreditOut(OutModel):
    """Who made an artist photo from Wikimedia Commons and its licence -
    shown with the photo, as the licence asks."""

    author: str | None
    license: str | None
    license_url: str | None
    page_url: str | None


class ArtistInfoOut(OutModel):
    name: str
    mbid: str | None
    mbid_locked: bool
    disambiguation: str | None
    type: str | None
    gender: str | None
    country: str | None
    area: str | None
    begin_area: str | None
    begin: str | None
    end: str | None
    ended: bool
    listeners: int | None
    playcount: int | None
    listenbrainz_url: str | None
    tags: list[str]
    bio: ArtistBioOut | None
    members: list[ArtistMemberOut]
    member_of: list[ArtistMemberOut]
    links: list[ArtistLinkOut]
    similar: list[SimilarArtistOut]
    image_credit: ImageCreditOut | None = None
    sources: list[str]
    # True while outside look-ups for this answer still run in the
    # background (app.deferred): it was built from stored data only, so ask
    # again shortly for the rest. False once nothing is missing or nothing
    # more can be fetched for now.
    pending: bool = False


class ArtistCandidateOut(OutModel):
    mbid: str
    name: str | None
    disambiguation: str | None
    type: str | None
    country: str | None
    begin: str | None
    score: int | None


class AlbumLabelOut(OutModel):
    label: str | None
    catalog_number: str | None


class AlbumEditionOut(OutModel):
    mbid: str
    title: str | None
    date: str | None
    country: str | None
    status: str | None
    disambiguation: str | None
    track_count: int | None
    format: str | None


class AlbumInfoOut(OutModel):
    artist: str | None
    album: str
    release_group_mbid: str | None
    release_mbid: str | None
    primary_type: str | None
    secondary_types: list[str]
    section: str
    original_release_date: str | None
    edition_date: str | None
    country: str | None
    barcode: str | None
    labels: list[AlbumLabelOut]
    status: str | None
    listeners: int | None
    playcount: int | None
    listenbrainz_url: str | None
    tags: list[str]
    wiki: InfoTextOut | None
    editions: list[AlbumEditionOut]
    release_locked: bool
    # True while outside look-ups for this answer still run in the
    # background (app.deferred): it was built from stored data only, so ask
    # again shortly for the rest. False once nothing is missing or nothing
    # more can be fetched for now.
    pending: bool = False


class TrackCreditOut(OutModel):
    role: str
    name: str
    detail: str | None


class TrackInfoOut(OutModel):
    id: int
    title: str
    artist: str | None
    album: str | None
    listeners: int | None
    playcount: int | None
    library_playcount: int
    listenbrainz_url: str | None
    tags: list[str]
    wiki: InfoTextOut | None
    recording_mbid: str | None
    artist_mbid: str | None
    release_mbid: str | None
    release_group_mbid: str | None
    isrc: str | None
    original_release_date: str | None
    release_date: str | None
    duration_seconds: float | None
    credits: list[TrackCreditOut]
    works: list[str]
    # True while outside look-ups for this answer still run in the
    # background (app.deferred): it was built from stored data only, so ask
    # again shortly for the rest. False once nothing is missing or nothing
    # more can be fetched for now.
    pending: bool = False


class TopTrackOut(OutModel):
    title: str
    mbid: str | None
    playcount: int | None
    listeners: int | None
    rank: int
    # The library track when it is owned.
    media_item: MediaItemOut | None


class ArtistTopTracksOut(OutModel):
    # False when the artist has no MusicBrainz ID to ask ListenBrainz by.
    available: bool
    tracks: list[TopTrackOut]
    # True while outside look-ups for this answer still run in the
    # background (app.deferred): it was built from stored data only, so ask
    # again shortly for the rest. False once nothing is missing or nothing
    # more can be fetched for now.
    pending: bool = False


# --- the want list and catalog pages (app.want_list, app.catalog) -----------
# Some keys appear only in some answers (`user` with all_users); those
# routes use response_model_exclude_unset so an absent key stays absent.


class WantEntryOut(BaseModel):
    id: int
    kind: Literal["artist", "album", "track"]
    artist: str
    title: str | None
    album: str | None
    station_name: str | None
    artist_mbid: str | None
    release_group_mbid: str | None
    recording_mbid: str | None
    release_type: str | None
    excluded: bool
    media_item_id: int | None
    owned_count: int | None
    total_count: int | None
    owned: bool
    created_at: str | None
    # Who wants it; only in an admin's all-users view.
    user: str | None = None


class WantedArtistOut(OutModel):
    artist: str
    artist_mbid: str | None
    wants_artist: bool
    album_count: int
    track_count: int
    owned_count: int
    # Who wants something by them; null outside an admin's all-users view.
    users: list[str] | None
    in_library: bool
    library_name: str | None
    representative_media_item_id: int | None
    # A release group whose cover stands in for a missing artist photo.
    cover_release_group_mbid: str | None = None
    updated_at: str | None


class MissingReleaseGroupOut(OutModel):
    mbid: str
    title: str
    primary_type: str | None
    section: str
    first_release_date: str | None
    status: Literal["owned", "partial", "missing"]
    owned_count: int | None
    total_count: int | None


class WantedArtistDetailOut(OutModel):
    artist: str
    artist_mbid: str | None
    artist_entry: WantEntryOut | None
    # Null when the artist itself is not wanted or its catalog is unknown.
    missing_release_groups: list[MissingReleaseGroupOut] | None
    refreshing: bool
    albums: list[WantEntryOut]
    tracks: list[WantEntryOut]
    in_library: bool
    # A release group whose cover stands in for a missing artist photo.
    cover_release_group_mbid: str | None = None
    # True while outside look-ups for this answer still run in the
    # background (app.deferred): it was built from stored data only, so ask
    # again shortly for the rest. False once nothing is missing or nothing
    # more can be fetched for now.
    pending: bool = False
    # True while a background re-match of the want list against the
    # library runs (after a library or access change): the entries are as
    # last matched. Poll /api/want-list/match-status and reload when false.
    matching: bool = False


class OwnedAlbumOut(OutModel):
    artist: str | None
    album: str | None


class WantSearchArtistOut(OutModel):
    name: str | None
    mbid: str
    disambiguation: str | None
    type: str | None
    country: str | None
    score: int | None
    in_library: bool
    library_name: str | None
    wanted: bool


class WantSearchAlbumOut(OutModel):
    title: str | None
    artist: str
    artist_mbid: str | None
    release_group_mbid: str
    primary_type: str | None
    section: str
    year: str | None
    in_library: bool
    owned_album: OwnedAlbumOut | None
    wanted: bool


class WantSearchTrackOut(OutModel):
    title: str | None
    artist: str
    artist_mbid: str | None
    recording_mbid: str
    album: str | None
    release_group_mbid: str | None
    year: str | None
    length_seconds: int | None
    wanted: bool
    media_item_id: int | None
    in_library: bool


class WantSearchOut(OutModel):
    artists: list[WantSearchArtistOut]
    albums: list[WantSearchAlbumOut]
    tracks: list[WantSearchTrackOut]


class RefreshingOut(OutModel):
    refreshing: bool


class CatalogReleaseGroupOut(OutModel):
    """One release group on an artist's catalog page."""

    mbid: str
    title: str
    primary_type: str | None
    secondary_types: list[str]
    section: str
    first_release_date: str | None
    disambiguation: str | None
    status: Literal["owned", "partial", "missing"]
    owned_count: int | None
    total_count: int | None
    owned_album: OwnedAlbumOut | None
    wanted: bool
    excluded: bool
    want_entry_id: int | None


class CatalogArtistOut(OutModel):
    artist: str | None
    artist_mbid: str
    in_library: bool
    release_groups: list[CatalogReleaseGroupOut]
    tracklists_known: int
    tracklists_total: int
    refreshing: bool
    artist_wanted: bool
    artist_name: str | None
    # A release group whose cover stands in for a missing artist photo.
    cover_release_group_mbid: str | None = None
    # True while outside look-ups for this answer still run in the
    # background (app.deferred): it was built from stored data only, so ask
    # again shortly for the rest. False once nothing is missing or nothing
    # more can be fetched for now.
    pending: bool = False


class CatalogTrackOut(OutModel):
    disc: int
    position: int | str
    title: str
    recording_mbid: str | None
    length_seconds: int | None
    artist: str | None
    media_item_id: int | None
    wanted: bool
    want_entry_id: int | None


class CatalogAlbumOut(OutModel):
    mbid: str
    title: str | None
    artist: str
    artist_mbid: str | None
    primary_type: str | None
    secondary_types: list[str]
    section: str
    first_release_date: str | None
    release_mbid: str | None
    tracks: list[CatalogTrackOut]
    owned_count: int
    wanted: bool
    want_entry_id: int | None
    artist_wanted: bool
    excluded: bool
    owned_album: OwnedAlbumOut | None
    # True while outside look-ups for this answer still run in the
    # background (app.deferred): it was built from stored data only, so ask
    # again shortly for the rest. False once nothing is missing or nothing
    # more can be fetched for now.
    pending: bool = False


class LyricsOut(OutModel):
    plain: str | None
    synced: str | None
    # "embedded", "cached", or the provider that supplied them; null when none.
    source: str | None
    # True while outside look-ups for this answer still run in the
    # background (app.deferred): it was built from stored data only, so ask
    # again shortly for the rest. False once nothing is missing or nothing
    # more can be fetched for now.
    pending: bool = False


class BackgroundPassOut(OutModel):
    """One background pass over the library (app.background_status)."""

    # running: working through tracks now; waiting: audio analysis waits
    # for the loudness pass to finish; idle: not running (up to date when
    # nothing is pending); disabled: turned off by a setting.
    state: Literal["running", "waiting", "idle", "disabled"]
    total: int
    # Tracks with a result, tracks tried that gave none (not retried until
    # the file changes), and tracks not yet tried; they add up to total.
    done: int
    failed: int
    pending: int
    # Tracks handled and values stored so far in the current run; null
    # when the pass is not running.
    run_checked: int | None
    run_stored: int | None
    # Tracks per minute over the current run, once it has run long enough
    # for the figure to mean something; null otherwise.
    rate_per_minute: float | None


class BackgroundStatusOut(OutModel):
    loudness: BackgroundPassOut
    audio_analysis: BackgroundPassOut


class ArtistSplitRulesOut(OutModel):
    separators: list[str]
    exceptions: list[str]


class ArtistSplitRulesSavedOut(ArtistSplitRulesOut):
    # Tracks whose artist links were rebuilt with the new rules.
    relinked: int


class BulkTagFailureOut(OutModel):
    id: int
    reason: str


class BulkTagEditOut(OutModel):
    updated: int
    succeeded: list[int]
    failed: list[BulkTagFailureOut]


class BulkRemoveOut(OutModel):
    removed: list[int]
    failed: list[int]


class DiscoveryRefreshOut(BaseModel):
    # Absent when there was nothing to seed the playlist from.
    playlist_id: int | None = None
    seed_artists: list[str]
    similar_artists: list[str]
    tracks_added: int


class PlaybackModeOut(OutModel):
    # "hls" for a video whose codecs a browser cannot decode.
    mode: Literal["direct", "hls"]
    url: str


class IntegrationOut(OutModel):
    # app.scrobbling.PROVIDERS
    provider: Literal["listenbrainz"]
    linked: bool
    # A stored credential that can no longer be decrypted.
    needs_reconnect: bool
    external_username: str | None
    available: bool


class ArtCandidateOut(OutModel):
    """One image offered by "Choose artwork..." (app.art_candidates)."""

    id: str
    source: str
    label: str
    url: str
    thumb_url: str


class DownloadEntryOut(OutModel):
    id: int
    # The file's path inside the download.
    path: str
    size: int
    url: str


class RenditionOut(OutModel):
    quality: Literal["1080", "720", "480"]
    status: Literal["none", "queued", "preparing", "ready", "failed"]
    # 0..1 while preparing.
    progress: float | None
    # Bytes, once ready.
    size: int | None
    # The MP4 itself.
    url: str


class RenditionListOut(OutModel):
    source_height: int | None
    source_size: int
    renditions: list[RenditionOut]


# --- Library info (/api/library/stats) ------------------------------------------
# Counts cover the tracks in the user's accessible library roots outside
# ignored folders, unless a field says otherwise. Dates are naive UTC; "this
# month"/"this year" are UTC calendar periods and "this week" the last 7 days.


class StatsTotalsOut(OutModel):
    albums: int
    songs: int
    # Linked artists (any credit), as the Artists tab lists them.
    artists: int
    videos: int
    # Songs and videos together.
    total_bytes: int
    # Songs' play time.
    total_seconds: float
    # The latest scan of any accessible root.
    last_scanned_at: datetime | None = None


class StatsRecentAlbumOut(OutModel):
    artist: str | None
    album: str
    track_count: int
    representative_media_item_id: int
    # Route key for /api/art/album/{art_key}, as on AlbumOut.
    art_key: str | None = None
    custom_art: bool = False
    art_version: int | None = None
    # Original release date ("YYYY-MM-DD") and its year.
    release_date: str | None = None
    year: int | None = None
    # The album's most common format among its tracks ("FLAC 16/44"); for
    # lossy files the tracks' average bitrate.
    format_label: str | None = None
    # When its first track was added.
    added_at: datetime


class StatsFormatOut(OutModel):
    # Codec ("flac", "mp3", "opus"...) or, for files not read yet, the extension.
    format: str
    # Display name: "FLAC", "MP3", "Opus".
    label: str
    lossless: bool
    songs: int
    bytes: int


class StatsContentsOut(OutModel):
    # Most songs first.
    formats: list[StatsFormatOut]
    with_lyrics: int
    loudness_measured: int
    # This user's liked songs.
    liked_songs: int
    genre_count: int


class StatsQualityOut(OutModel):
    # All of these count only songs whose quality has been read
    # (quality_scanned of quality_total); a library from before the
    # quality columns needs a scan ("Rescan metadata" also works) to fill
    # them. Shares and averages are None when nothing has been read.
    lossless_songs: int
    lossless_share: float | None = None
    # Lossless with bit depth >= 24 or a sample rate above 48 kHz.
    hi_res_songs: int
    average_mp3_kbps: int | None = None
    # Lossy songs below 192 kbps.
    below_192_kbps_songs: int
    longest_song_seconds: float | None = None
    average_album_seconds: float | None = None
    quality_scanned: int
    quality_total: int


class StatsPlayedArtistOut(OutModel):
    name: str
    plays: int


class StatsPlayedAlbumOut(OutModel):
    artist: str | None
    album: str
    plays: int


class StatsListeningOut(OutModel):
    # Counted plays (play history) of songs in accessible roots.
    plays_this_week: int
    plays_this_year: int
    # From the listen log, which is bounded (per-user cap, the play-history
    # retention, and it may not reach back to the start of the year);
    # listening_capped is true when that may leave this year's figure short.
    listening_seconds_this_year: float
    listening_capped: bool
    # All-time, by counted plays: the main credited artist, and the album.
    most_played_artist: StatsPlayedArtistOut | None = None
    most_played_album: StatsPlayedAlbumOut | None = None
    never_played_songs: int


class StatsCountOut(OutModel):
    name: str
    songs: int


class StatsGenresOut(OutModel):
    # The 12 genres with the most songs, most first (case-insensitive names).
    top: list[StatsCountOut]
    distinct: int


class StatsDecadeOut(OutModel):
    # "1990s"
    decade: str
    songs: int


class StatsDecadesOut(OutModel):
    # Oldest first.
    decades: list[StatsDecadeOut]
    # Songs without a release date.
    unknown: int


class StatsLargestAlbumOut(OutModel):
    artist: str | None
    album: str
    bytes: int


class StatsStorageOut(OutModel):
    # Display names of the accessible roots on this filesystem.
    roots: list[str]
    # None when the folder could not be read (an unmounted share, say).
    free_bytes: int | None = None
    total_bytes: int | None = None


class StatsGrowthOut(OutModel):
    albums_added_this_month: int
    albums_added_this_year: int
    largest_album: StatsLargestAlbumOut | None = None
    video_bytes: int


class StatsNavCountsOut(OutModel):
    # What each tab lists, so the sidebar can show it: unlike the rest of
    # this response these include ignored folders, as the tabs do.
    artists: int
    albums: int
    songs: int
    videos: int
    # This user's playlists (the Playlists tab).
    playlists: int
    # Stations this user can see: shared ones and their own.
    radio_stations: int
    # This user's want-list entries, without the albums/songs left out of
    # a wanted artist.
    want_list: int


class LibraryStatsOut(OutModel):
    totals: StatsTotalsOut
    # Newest first, at most 50.
    recently_added: list[StatsRecentAlbumOut]
    contents: StatsContentsOut
    quality: StatsQualityOut
    listening: StatsListeningOut
    by_genre: StatsGenresOut
    by_decade: StatsDecadesOut
    growth: StatsGrowthOut
    nav_counts: StatsNavCountsOut
