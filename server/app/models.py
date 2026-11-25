from __future__ import annotations

import enum
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import (
    Enum,
    ForeignKey,
    Index,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
    validates,
)

from app.audio_quality import format_label as quality_label
from app.json_column import JSONText

EQ_BANDS = 10


def utcnow() -> datetime:
    """Naive UTC, deliberately — SQLite's DateTime type strips tzinfo on
    round-trip, so an aware `now()` here would compare unequal to (and
    crash against) whatever comes back out of the database."""
    return datetime.now(UTC).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class SystemSetting(Base):
    __tablename__ = "system_settings"

    key: Mapped[str] = mapped_column(primary_key=True)
    value: Mapped[str]


class MediaKind(str, enum.Enum):  # noqa: UP042 - StrEnum would change str()/format() output of the members
    track = "track"
    music_video = "music_video"
    video = "video"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(unique=True, index=True)
    password_hash: Mapped[str]
    is_admin: Mapped[bool] = mapped_column(default=False)
    # Appearance preference that follows the account across devices, unlike
    # the per-device localStorage settings (theme, tile size, etc.). Kept as
    # a plain string so any preset or custom hex round-trips unchanged.
    accent_color: Mapped[str | None] = mapped_column(default=None)
    # Notices the user chose "Never show again" for, as a JSON list of ids
    # (e.g. "lastfm_removed"). The Last.fm removal migration also puts the marker
    # "lastfm_was_linked" here for users whose Last.fm link it removed.
    dismissed_notices_json: Mapped[list[str] | None] = mapped_column(
        JSONText(empty=list, varchar=True), default=None
    )
    # Audio preferences that follow the account across devices (volume
    # levelling on/off, equaliser preset and its ten band gains in dB).
    volume_leveling: Mapped[bool] = mapped_column(default=False)
    eq_preset: Mapped[str] = mapped_column(default="flat")
    eq_gains_json: Mapped[list[float] | None] = mapped_column(JSONText(varchar=True), default=None)
    # Privacy: whether what this user plays may be looked up on ListenBrainz
    # (similar tracks and artists) for recommendations. On by default.
    online_recommendations: Mapped[bool] = mapped_column(default=True, server_default="1")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    @property
    def dismissed_notices(self) -> list[str]:
        notices = self.dismissed_notices_json
        return [str(n) for n in notices] if isinstance(notices, list) else []

    @property
    def eq_gains(self) -> list[float]:
        """The ten equaliser band gains in dB; flat (all zero) until saved."""
        if self.eq_gains_json is None:
            return [0.0] * EQ_BANDS
        return [float(g) for g in self.eq_gains_json]


class Session(Base):
    __tablename__ = "sessions"

    token: Mapped[str] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    # Indexed so the scheduler's purge of expired rows is not a table scan.
    expires_at: Mapped[datetime] = mapped_column(index=True)


class LibraryRoot(Base):
    __tablename__ = "library_roots"

    id: Mapped[int] = mapped_column(primary_key=True)
    path: Mapped[str] = mapped_column(unique=True)
    display_name: Mapped[str]
    is_shared: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    last_scanned_at: Mapped[datetime | None] = mapped_column(default=None)
    last_scan_error: Mapped[str | None] = mapped_column(default=None)

    media_items: Mapped[list[MediaItem]] = relationship(back_populates="library_root")
    tasks: Mapped[list[LibraryTask]] = relationship(back_populates="library_root")


class IgnoredLibraryFolder(Base):
    """A folder whose tracks remain playable but are excluded from
    metadata-provider work and automatically generated discovery content."""

    __tablename__ = "ignored_library_folders"
    __table_args__ = (UniqueConstraint("library_root_id", "relative_path"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    library_root_id: Mapped[int] = mapped_column(ForeignKey("library_roots.id"))
    relative_path: Mapped[str]


class LibraryTask(Base):
    """Persistent progress for a scan or MusicBrainz enrichment run."""

    __tablename__ = "library_tasks"

    id: Mapped[int] = mapped_column(primary_key=True)
    library_root_id: Mapped[int] = mapped_column(ForeignKey("library_roots.id"), index=True)
    kind: Mapped[str] = mapped_column(index=True)  # scan | mb_isrc | lb_tags | enrich | cover_art ...
    status: Mapped[str] = mapped_column(
        index=True
    )  # running | stopping | stopped | completed | failed
    total: Mapped[int] = mapped_column(default=0)
    processed: Mapped[int] = mapped_column(default=0)
    added: Mapped[int] = mapped_column(default=0)
    updated: Mapped[int] = mapped_column(default=0)
    removed: Mapped[int] = mapped_column(default=0)
    matched: Mapped[int] = mapped_column(default=0)
    error: Mapped[str | None] = mapped_column(default=None)
    # A full setup scan's current step, e.g. "2/6 MusicBrainz IDs".
    stage: Mapped[str | None] = mapped_column(default=None)
    # Missing tracks a scan did not remove because the evidence was weak (an
    # empty or unreadable folder, or a large share of the root), and why.
    missing_held: Mapped[int] = mapped_column(default=0)
    note: Mapped[str | None] = mapped_column(default=None)
    started_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(default=None)

    library_root: Mapped[LibraryRoot] = relationship(back_populates="tasks")


class UserLibraryAccess(Base):
    __tablename__ = "user_library_access"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    library_root_id: Mapped[int] = mapped_column(
        ForeignKey("library_roots.id"), primary_key=True
    )


class MediaItem(Base):
    __tablename__ = "media_items"
    __table_args__ = (
        UniqueConstraint("library_root_id", "relative_path"),
        # Every one of /artists, /albums, /genres/mixes, and filtered track
        # listings filters by (library_root_id, kind) and then groups or
        # filters by artist/album or genre - without an index, each of
        # those would be a full table scan, cheap at a few thousand rows, a
        # multi-second cost once a library reaches the tens/hundreds of
        # thousands. See app.main.ensure_media_item_indexes for how this
        # gets added to a database that already has this table.
        Index(
            "idx_media_items_root_kind_artist_album",
            "library_root_id",
            "kind",
            "artist",
            "album",
        ),
        Index("idx_media_items_root_kind_genre", "library_root_id", "kind", "genre"),
        # Want-list matching (app.owned_match.match_owned) looks tracks up
        # by recording MBID and by lower(artist). Root and kind ride along:
        # without statistics SQLite prefers any index matching more
        # equality columns, so a single-column index would lose to the
        # (root, kind, ...) ones above and each lookup would scan the library.
        Index(
            "idx_media_items_recording_mbid_root_kind",
            "musicbrainz_recording_mbid",
            "library_root_id",
            "kind",
        ),
        Index(
            "idx_media_items_lower_artist_root_kind",
            func.lower(text("artist")),
            "library_root_id",
            "kind",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    library_root_id: Mapped[int] = mapped_column(ForeignKey("library_roots.id"))
    kind: Mapped[MediaKind] = mapped_column(Enum(MediaKind), default=MediaKind.track)
    relative_path: Mapped[str]
    video_folder: Mapped[str | None] = mapped_column(default=None, index=True)
    title: Mapped[str]
    artist: Mapped[str | None] = mapped_column(default=None)
    album_artist: Mapped[str | None] = mapped_column(default=None)
    # One-time marker so existing files are re-read for album artist after
    # this field is added, even when their file modification time is stable.
    album_artist_scanned_at: Mapped[datetime | None] = mapped_column(default=None)
    # Same, for the multi-value ARTISTS/ALBUMARTISTS tags behind the
    # track_artists links (app.artist_credits).
    artists_scanned_at: Mapped[datetime | None] = mapped_column(default=None)
    # The multi-value tags themselves, {"artists": [...], "album_artists":
    # [...]}, so a change to the split rules can rebuild the links exactly
    # without re-reading every file.
    artist_tags_json: Mapped[dict[str, list[str]] | None] = mapped_column(
        JSONText(ensure_ascii=False), default=None, deferred=True
    )
    # Who the album belongs to, as displayed: the album artist tag, else the
    # track's main artists without any "feat." guests - so "A" and
    # "A feat. B" tracks share one album. Set with the artist links.
    album_owner: Mapped[str | None] = mapped_column(default=None)
    album: Mapped[str | None] = mapped_column(default=None)
    # The Album row for (album owner, album) - see app.albums.
    album_id: Mapped[int | None] = mapped_column(
        ForeignKey("albums.id"), default=None, index=True
    )
    genre: Mapped[str | None] = mapped_column(default=None)
    track_number: Mapped[int | None] = mapped_column(default=None)
    # Release date, parsed from whatever date tag the file has (see
    # app.scanner._parse_release_date) and normalized to "YYYY-MM-DD" -
    # zero-padded to the 1st of the month/year when the tag itself only had
    # year or year-month precision, so plain string comparison still sorts
    # correctly. Only ever set for audio tracks - used to sort Albums/
    # Artists by "date published" (most tagged files carry a full date, not
    # just a year, and artists often release several singles in one year).
    release_date: Mapped[str | None] = mapped_column(default=None)
    performer: Mapped[str | None] = mapped_column(default=None)
    description: Mapped[str | None] = mapped_column(default=None)
    duration_seconds: Mapped[float | None] = mapped_column(default=None)
    # ReplayGain adjustments in dB against the -18 LUFS reference: read from
    # tags at scan time, or measured with ffmpeg by app.loudness when absent.
    replaygain_track_gain: Mapped[float | None] = mapped_column(default=None)
    replaygain_album_gain: Mapped[float | None] = mapped_column(default=None)
    # Track peak as a linear sample value (1.0 = digital full scale), from
    # the REPLAYGAIN_TRACK_PEAK tag or ffmpeg's true-peak measurement.
    replaygain_track_peak: Mapped[float | None] = mapped_column(default=None)
    # Set once tags or ffmpeg have been consulted - even when neither gave a
    # value - so the background pass never retries the same file forever.
    loudness_scanned_at: Mapped[datetime | None] = mapped_column(default=None)
    file_mtime: Mapped[float] = mapped_column(default=0.0)
    # Byte size at last scan; with the mtime it detects in-place tag edits.
    # NULL on rows from before the column existed.
    file_size: Mapped[int | None] = mapped_column(default=None)
    # The file_mtime the startup art backfill last probed and found no art
    # for, so it skips the track until the file changes. NULL: not probed.
    art_checked_mtime: Mapped[float | None] = mapped_column(default=None)
    # Audio quality from the file's stream header (app.audio_quality), for
    # the format label ("FLAC 16/44", "MP3 320") and Library info. Tracks
    # only; NULL until a scan has read them (a normal scan fills rows from
    # before these columns existed). bit_depth is NULL for lossy codecs.
    codec: Mapped[str | None] = mapped_column(default=None)
    bitrate_kbps: Mapped[int | None] = mapped_column(default=None)
    sample_rate_hz: Mapped[int | None] = mapped_column(default=None)
    bit_depth: Mapped[int | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    # Metadata enrichment - all optional, populated on demand
    # rather than during every scan, to respect provider rate limits.
    musicbrainz_recording_mbid: Mapped[str | None] = mapped_column(default=None)
    musicbrainz_artist_mbid: Mapped[str | None] = mapped_column(default=None)
    # ISRC is an exact recording identifier embedded by many taggers. It is
    # preferred to a fuzzy artist/title search when asking MusicBrainz.
    musicbrainz_isrc: Mapped[str | None] = mapped_column(default=None)
    # Lets missing-metadata runs distinguish a genuine catalogue miss from a
    # transient provider failure that should be retried.
    musicbrainz_lookup_status: Mapped[str | None] = mapped_column(default=None)
    musicbrainz_last_attempt_at: Mapped[datetime | None] = mapped_column(default=None)
    musicbrainz_last_error: Mapped[str | None] = mapped_column(default=None)
    # Existing libraries need one tag read, even if a file's mtime has not
    # changed, to backfill embedded ISRCs.
    musicbrainz_isrc_scanned_at: Mapped[datetime | None] = mapped_column(default=None)
    # The complete enriched recording response is kept instead of reducing
    # MusicBrainz to the small set of fields the current UI happens to show.
    # Normalised genres/tags below make the parts useful for mixes queryable.
    # Deferred, like the other large text columns (artist_tags_json, the
    # lyrics): a select(MediaItem) leaves them
    # out, so listings, mix pools and bulk jobs don't drag 5-20 KB per track
    # along. A reader that needs one undefer()s it in
    # its query; touching it otherwise costs one extra query per row.
    musicbrainz_metadata_json: Mapped[dict[str, Any] | None] = mapped_column(
        JSONText(ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        default=None,
        deferred=True,
    )
    musicbrainz_fetched_at: Mapped[datetime | None] = mapped_column(default=None)
    # ListenBrainz artist tags are additive too; this only records that they
    # were fetched.
    online_tags_fetched_at: Mapped[datetime | None] = mapped_column(default=None)
    cover_art_url: Mapped[str | None] = mapped_column(default=None)
    lyrics_plain: Mapped[str | None] = mapped_column(default=None, deferred=True, deferred_group="lyrics")
    lyrics_synced: Mapped[str | None] = mapped_column(default=None, deferred=True, deferred_group="lyrics")
    lyrics_fetched_at: Mapped[datetime | None] = mapped_column(default=None)

    library_root: Mapped[LibraryRoot] = relationship(back_populates="media_items")
    tags: Mapped[list[Tag]] = relationship(
        secondary="media_item_tags", back_populates="media_items"
    )
    genres: Mapped[list[Genre]] = relationship(
        secondary="media_item_genres", back_populates="media_items"
    )
    musicbrainz_tags: Mapped[list[MusicBrainzTag]] = relationship(
        secondary="media_item_musicbrainz_tags", back_populates="media_items"
    )

    @validates("relative_path")
    def _derive_video_folder(self, key: str, value: str) -> str:
        self.video_folder = Path(value).parent.name or "Unsorted"
        return value

    @property
    def format_label(self) -> str | None:
        """Short format label for MediaItemOut ("FLAC 24/96", "MP3 320"),
        or the file extension when the quality columns are not filled."""
        return quality_label(
            self.codec, self.bit_depth, self.sample_rate_hz, self.bitrate_kbps,
            Path(self.relative_path).suffix if self.relative_path else None,
        )


class Artist(Base):
    """One artist, however many tracks credit them and in what role. Built
    from tags by app.artist_credits; the name is the display spelling first
    seen, `normalized_name` the identity (case/accent-insensitive)."""

    __tablename__ = "artists"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]
    normalized_name: Mapped[str] = mapped_column(unique=True)
    sort_name: Mapped[str | None] = mapped_column(default=None)
    mbid: Mapped[str | None] = mapped_column(default=None, index=True)
    # Set when a user picked the MusicBrainz artist by hand ("Fix match"),
    # so automatic matching never overwrites it.
    mbid_locked: Mapped[bool] = mapped_column(default=False)
    # Legacy merged artist info; nothing writes it any more (info dialogs
    # are built per request in app.metadata_info).
    info_json: Mapped[dict[str, Any] | None] = mapped_column(JSONText(), default=None)
    info_fetched_at: Mapped[datetime | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class Album(Base):
    """One album: its owner (album artist, else track artist, as displayed)
    plus title, case/accent-insensitively. Release details come from the
    tracks' MusicBrainz recordings (app.albums) - the release group decides
    album/EP/single/live/compilation and the original release date."""

    __tablename__ = "albums"
    __table_args__ = (UniqueConstraint("owner_key", "title_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    owner: Mapped[str | None] = mapped_column(default=None)
    title: Mapped[str]
    owner_key: Mapped[str] = mapped_column(default="")
    title_key: Mapped[str]
    release_group_mbid: Mapped[str | None] = mapped_column(default=None, index=True)
    # The edition the library's tracks match best; a user can pick another.
    release_mbid: Mapped[str | None] = mapped_column(default=None)
    release_locked: Mapped[bool] = mapped_column(default=False)
    # MusicBrainz primary type (Album, EP, Single, Broadcast, Other) and
    # secondary types (Live, Compilation, Soundtrack, Remix, ...).
    primary_type: Mapped[str | None] = mapped_column(default=None)
    secondary_types_json: Mapped[list[str] | None] = mapped_column(
        JSONText(empty=list, varchar=True), default=None
    )
    # "YYYY-MM-DD", padded like MediaItem.release_date: the release group's
    # first release (original) and the chosen edition's own date.
    first_release_date: Mapped[str | None] = mapped_column(default=None)
    release_date: Mapped[str | None] = mapped_column(default=None)
    country: Mapped[str | None] = mapped_column(default=None)
    label: Mapped[str | None] = mapped_column(default=None)
    catalog_number: Mapped[str | None] = mapped_column(default=None)
    # Legacy album info; nothing writes it any more (see app.metadata_info).
    info_json: Mapped[dict[str, Any] | None] = mapped_column(JSONText(), default=None)
    info_fetched_at: Mapped[datetime | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class TrackArtist(Base):
    """A track's credit for one artist. `role` is main, featured or
    album_artist (see app.artist_credits); `position` keeps tag order."""

    __tablename__ = "track_artists"
    __table_args__ = (
        Index("idx_track_artists_artist_role", "artist_id", "role"),
    )

    media_item_id: Mapped[int] = mapped_column(
        ForeignKey("media_items.id"), primary_key=True
    )
    artist_id: Mapped[int] = mapped_column(ForeignKey("artists.id"), primary_key=True)
    role: Mapped[str] = mapped_column(primary_key=True)
    position: Mapped[int] = mapped_column(default=0)

    artist: Mapped[Artist] = relationship()


class Tag(Base):
    __tablename__ = "tags"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(unique=True)

    media_items: Mapped[list[MediaItem]] = relationship(
        secondary="media_item_tags", back_populates="tags"
    )


class MediaItemTag(Base):
    __tablename__ = "media_item_tags"

    media_item_id: Mapped[int] = mapped_column(
        ForeignKey("media_items.id"), primary_key=True
    )
    tag_id: Mapped[int] = mapped_column(ForeignKey("tags.id"), primary_key=True)


class Genre(Base):
    """A canonical genre name, shared by embedded and MusicBrainz metadata."""

    __tablename__ = "genres"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]
    normalized_name: Mapped[str] = mapped_column(unique=True, index=True)
    media_items: Mapped[list[MediaItem]] = relationship(
        secondary="media_item_genres", back_populates="genres"
    )


class MediaItemGenre(Base):
    """A genre assignment and its source.

    Keeping the source lets a rescan replace the file's embedded genres
    without deleting the independently-fetched MusicBrainz genres.
    """

    __tablename__ = "media_item_genres"

    media_item_id: Mapped[int] = mapped_column(
        ForeignKey("media_items.id"), primary_key=True
    )
    genre_id: Mapped[int] = mapped_column(ForeignKey("genres.id"), primary_key=True)
    source: Mapped[str] = mapped_column(primary_key=True)


class MusicBrainzTag(Base):
    """A non-genre folksonomy tag supplied by MusicBrainz."""

    __tablename__ = "musicbrainz_tags"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]
    normalized_name: Mapped[str] = mapped_column(unique=True, index=True)
    media_items: Mapped[list[MediaItem]] = relationship(
        secondary="media_item_musicbrainz_tags", back_populates="musicbrainz_tags"
    )


class MediaItemMusicBrainzTag(Base):
    __tablename__ = "media_item_musicbrainz_tags"

    media_item_id: Mapped[int] = mapped_column(
        ForeignKey("media_items.id"), primary_key=True
    )
    musicbrainz_tag_id: Mapped[int] = mapped_column(
        ForeignKey("musicbrainz_tags.id"), primary_key=True
    )
    vote_count: Mapped[int] = mapped_column(default=0)
    # "musicbrainz" or "listenbrainz"; lets a refresh
    # from one source leave the other's tags alone.
    source: Mapped[str] = mapped_column(default="musicbrainz", server_default="musicbrainz")


class Playlist(Base):
    __tablename__ = "playlists"
    __table_args__ = (
        # Favourites is one per user: it mirrors the user's likes.
        Index(
            "uq_playlists_one_favorites_per_user",
            "user_id",
            unique=True,
            sqlite_where=text("auto_kind = 'favorites'"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    name: Mapped[str]
    description: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    is_auto_generated: Mapped[bool] = mapped_column(default=False)
    # Distinguishes the auto-managed playlist "slots" ("discovery" /
    # "generated" / "mix") from each other and from ordinary user playlists -
    # None for anything the user created themselves.
    auto_kind: Mapped[str | None] = mapped_column(default=None)
    # Which genre this auto_kind="mix" playlist was generated for - there
    # can be several of these at once (one per featured genre), unlike
    # discovery/generated which are single slots, so this is what
    # disambiguates them from each other. None for every other auto_kind.
    mix_genre: Mapped[str | None] = mapped_column(default=None)
    # Materialized snapshot of the Home collage ids (up to 4, distinct
    # artists) chosen when the mix was generated/refreshed, so /mixes can
    # serve it directly instead of the client fetching every mix's full
    # track list. Nullable keeps rows from before this read model valid.
    preview_media_item_ids_json: Mapped[list[int] | None] = mapped_column(
        JSONText(empty=list), nullable=True, default=None
    )
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    items: Mapped[list[PlaylistItem]] = relationship(
        back_populates="playlist",
        order_by="PlaylistItem.position",
        cascade="all, delete-orphan",
    )


class PlaylistItem(Base):
    __tablename__ = "playlist_items"
    __table_args__ = (
        # A playlist's items in order (detail view, first-track covers):
        # without it every playlist read would scan the whole table.
        Index("idx_playlist_items_playlist_position", "playlist_id", "position"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    playlist_id: Mapped[int] = mapped_column(ForeignKey("playlists.id"))
    # media_item_id is indexed on every table that references a track, so
    # app.cleanup.delete_media_records' dependent-row deletes are lookups,
    # not one full scan per table (an index leading with user_id can't help).
    media_item_id: Mapped[int] = mapped_column(ForeignKey("media_items.id"), index=True)
    position: Mapped[int]

    playlist: Mapped[Playlist] = relationship(back_populates="items")
    media_item: Mapped[MediaItem] = relationship()


class Favorite(Base):
    __tablename__ = "favorites"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    media_item_id: Mapped[int] = mapped_column(
        ForeignKey("media_items.id"), primary_key=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class TrackDislike(Base):
    """A user's dislike of one song - the counterpart of Favorite. Kept in
    its own table so `favorites` (and the Favourites playlist synced from
    it) stays exactly what it was: the liked songs."""

    __tablename__ = "track_dislikes"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    media_item_id: Mapped[int] = mapped_column(
        ForeignKey("media_items.id"), primary_key=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class CollectionPreference(Base):
    """A user's like or dislike of a whole album or artist. Albums and
    artists are only text on each track (see MediaItem), so this stores the
    names rather than media ids - which also lets it survive rescans. An
    album is keyed by its owner (album_artist, else the track artist) plus
    its title; both compare case-insensitively (see app.preferences)."""

    __tablename__ = "collection_preferences"
    __table_args__ = (
        Index(
            "uq_collection_preferences_user_kind_name",
            "user_id",
            "kind",
            func.lower(text("artist")),
            func.lower(func.coalesce(text("album"), "")),
            unique=True,
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    # "album" or "artist"
    kind: Mapped[str]
    artist: Mapped[str]
    album: Mapped[str | None] = mapped_column(default=None)
    # "liked" or "disliked"
    state: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class UserIntegration(Base):
    """A user's link to their own ListenBrainz account, for pushing
    scrobbles - the opposite direction from the metadata_providers, which
    only ever read public data with the app's own credentials."""

    __tablename__ = "user_integrations"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    provider: Mapped[str] = mapped_column(primary_key=True)  # "listenbrainz"
    credential: Mapped[str]  # encrypted {"token": ...}
    external_username: Mapped[str | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class PlayHistory(Base):
    __tablename__ = "play_history"
    __table_args__ = (
        Index("idx_play_history_user_played_at", "user_id", "played_at"),
        Index("idx_play_history_user_media_played_at", "user_id", "media_item_id", "played_at"),
        # One row per play: a client retrying a report it could
        # not confirm sends the same started_at again. Rows from before
        # started_at existed (and old clients' reports) are NULL, which a
        # SQLite unique index never treats as equal.
        Index(
            "uq_play_history_user_media_started_at",
            "user_id",
            "media_item_id",
            "started_at",
            unique=True,
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    media_item_id: Mapped[int] = mapped_column(ForeignKey("media_items.id"), index=True)
    played_at: Mapped[datetime] = mapped_column(default=utcnow)
    # When the listening began, as the client reported it (naive UTC).
    started_at: Mapped[datetime | None] = mapped_column(default=None)


class ListenEvent(Base):
    """How one listen on the device producing the sound ended (see
    app.listening): every track or music video that was started and then
    left, whether or not it counted as a play. `started_at` is the same
    value the play report sends, so a counted play joins its play_history
    row on (user, item, started_at). Bounded: pruned with the play-history
    retention and to a fixed number of rows per user (app.cleanup)."""

    __tablename__ = "listen_events"
    __table_args__ = (
        # One row per listen: a client retrying a report sends the same
        # started_at again. Also serves "this user's listens of these items".
        Index(
            "uq_listen_events_user_media_started_at",
            "user_id",
            "media_item_id",
            "started_at",
            unique=True,
        ),
        Index("idx_listen_events_user_started_at", "user_id", "started_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    media_item_id: Mapped[int] = mapped_column(ForeignKey("media_items.id"), index=True)
    # When the listening began and ended (naive UTC, the client's clock).
    started_at: Mapped[datetime]
    ended_at: Mapped[datetime]
    listened_seconds: Mapped[float]
    duration_seconds: Mapped[float | None] = mapped_column(default=None)
    position_seconds: Mapped[float | None] = mapped_column(default=None)
    # finished / skipped / stopped / transferred / error / unknown.
    end_reason: Mapped[str]
    # auto / user, or NULL when the client could not tell.
    start_kind: Mapped[str | None] = mapped_column(default=None)
    # The play source the user started the queue from (album, mix, ...).
    source_kind: Mapped[str | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class ScrobbleQueueItem(Base):
    """A scrobble ListenBrainz could not take right now (network, rate limit,
    5xx, or a token it stopped accepting), kept for
    `scrobbling.retry_queued_scrobbles`. Only what the
    submission needs is stored, so a later rescan or deleted track does not
    lose it. Dropped once sent, permanently rejected, or older than 14 days."""

    __tablename__ = "scrobble_queue"
    __table_args__ = (Index("idx_scrobble_queue_due", "next_attempt_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    artist: Mapped[str]
    title: Mapped[str]
    album: Mapped[str | None] = mapped_column(default=None)
    duration: Mapped[float | None] = mapped_column(default=None)
    # The track's MusicBrainz IDs when it was played (artist IDs comma
    # separated), so a retried listen is as precise as a direct one.
    recording_mbid: Mapped[str | None] = mapped_column(default=None)
    artist_mbids: Mapped[str | None] = mapped_column(default=None)
    started_at: Mapped[datetime]
    attempts: Mapped[int] = mapped_column(default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(default=utcnow)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class PlaySourceHistory(Base):
    """What was actually played (an album, artist, playlist, genre mix, or
    radio station), not just the individual track - so "Recently played"
    can resurface the whole thing to resume, not a single song stranded
    out of its original context. One row per distinct (user, kind,
    payload) - starting the same album/playlist/station again just bumps
    `updated_at` rather than growing a new row every time."""

    __tablename__ = "play_source_history"
    __table_args__ = (
        Index(
            "idx_play_source_history_user_updated_at",
            "user_id",
            "updated_at",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    kind: Mapped[str] = (
        mapped_column()
    )  # "album" | "artist" | "mix" | "playlist" | "radio" | "library"
    label: Mapped[str] = mapped_column()
    payload_json: Mapped[dict[str, Any] | None] = mapped_column(
        JSONText(sort_keys=True, varchar=True), nullable=False
    )
    # The playlist a history row points to can be replaced (or removed)
    # when an auto-generated mix is refreshed.  Keep the last real cover
    # around so the history card does not suddenly degrade to its emoji
    # fallback just because that managed playlist slot has changed.
    art_media_item_id: Mapped[int | None] = mapped_column(default=None)
    # Snapshot of the collage chosen when this source was recorded. Nullable
    # keeps rows from before this materialized read model valid.
    preview_media_item_ids_json: Mapped[list[int] | None] = mapped_column(
        JSONText(empty=list), nullable=True, default=None
    )
    updated_at: Mapped[datetime] = mapped_column(default=utcnow)


class Device(Base):
    """A browser installation which may temporarily lose its WebSocket.

    ``client_token`` is generated once by the browser and lets a backgrounded
    phone reconnect as the same playback target.  A WebSocket is only a
    control channel, not proof that its media element has stopped playing.
    """

    __tablename__ = "devices"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    name: Mapped[str]
    device_type: Mapped[str] = mapped_column(default="web")
    client_token: Mapped[str | None] = mapped_column(default=None)
    is_connected: Mapped[bool] = mapped_column(default=False)
    connected_at: Mapped[datetime] = mapped_column(default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(default=utcnow)


class PlaybackState(Base):
    """One canonical row per user - the single source of truth every
    connected device's UI reflects, rather than trusting its own local
    state."""

    __tablename__ = "playback_state"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    media_item_id: Mapped[int | None] = mapped_column(
        ForeignKey("media_items.id"), default=None
    )
    position_seconds: Mapped[float] = mapped_column(default=0.0)
    is_playing: Mapped[bool] = mapped_column(default=False)
    active_device_id: Mapped[int | None] = mapped_column(
        ForeignKey("devices.id"), default=None
    )
    queue_index: Mapped[int] = mapped_column(default=0)
    is_shuffled: Mapped[bool] = mapped_column(default=False)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow)

    # Windowed queue support (see app.playback._ensure_window) - the queue
    # is not the *whole* played-from list, just a materialized tail a
    # few hundred items deep. These describe how to keep generating more of
    # it: `queue_context_json` is the full played-from set ({"kind": "ids",
    # "ids": [...]} for a small explicit list - an album, playlist, etc. -
    # or {"kind": "library", "filters": {...}} for a large filterable one,
    # e.g. the whole Songs tab), `queue_context_count` is its total size
    # (cached so exhaustion never needs a recount), `tail_cursor` is how
    # many natural-order items of it have been consumed into the window so
    # far, and `shuffle_seed` seeds the deterministic scramble order used
    # when `is_shuffled` is on (re-rolled each time shuffle turns on, or a
    # repeat-all loop wraps around, so repeats don't replay in the same
    # order every pass).
    shuffle_seed: Mapped[int | None] = mapped_column(default=None)
    repeat_mode: Mapped[str] = mapped_column(default="off")  # "off" | "all" | "one"
    queue_context_json: Mapped[dict[str, Any] | None] = mapped_column(JSONText(varchar=True), default=None)
    queue_context_count: Mapped[int] = mapped_column(default=0)
    # Where the current queue was started from (an album, artist, playlist,
    # mix or Songs-tab filter, shaped like a PlaySourceIn) so Now Playing can
    # link back to it. Cleared when a queue starts without one, and when
    # autoplay moves on past the end of what was started.
    queue_origin_json: Mapped[dict[str, Any] | None] = mapped_column(JSONText(varchar=True), default=None)
    tail_cursor: Mapped[int] = mapped_column(default=0)
    # Bumped once for every broadcast state change (see
    # app.routers.playback._broadcast_state), so a device can drop a
    # snapshot older than one it already applied: an unchanged revision
    # means nothing changed.
    revision: Mapped[int] = mapped_column(default=0, server_default="0")


class PlaybackQueueItem(Base):
    __tablename__ = "playback_queue_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    position: Mapped[int]
    # The pre-shuffle order - `position` is what's actually played and
    # moves around under shuffle, `original_position` never changes once
    # set, so turning shuffle off can restore exactly what it was before.
    original_position: Mapped[int] = mapped_column(default=0)
    media_item_id: Mapped[int] = mapped_column(ForeignKey("media_items.id"), index=True)
    # "explicit" = user-inserted (play next / add to queue end), "generated"
    # = pulled from queue_context by the windowing top-up. Explicit rows are
    # never auto-trimmed; generated ones are, once played, to bound growth.
    source: Mapped[str] = mapped_column(default="generated")


class RadioStation(Base):
    __tablename__ = "radio_stations"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]
    stream_url: Mapped[str]
    description: Mapped[str | None] = mapped_column(default=None)
    # Which curated source this came from (e.g. "SomaFM", "Radio Paradise") -
    # None for anything a user added themselves, regardless of is_shared.
    # That's what actually distinguishes "pre-added" from "user added" in
    # the UI; owner_user_id alone can't, since an admin-shared user station
    # also has owner_user_id None.
    provider: Mapped[str | None] = mapped_column(default=None)
    is_shared: Mapped[bool] = mapped_column(default=True)
    owner_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), default=None
    )
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class RadioStationFavorite(Base):
    __tablename__ = "radio_station_favorites"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    station_id: Mapped[int] = mapped_column(
        ForeignKey("radio_stations.id"), primary_key=True
    )
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class WantListItem(Base):
    """Something a user wants but doesn't own: a whole artist (live - their
    future releases count too), an album (MusicBrainz release group) or a
    song. Songs noted from the radio are track entries whose
    `station_name` says where they were heard. Just the note itself,
    not any acquisition automation. See app.want_list."""

    __tablename__ = "want_list_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    # "artist", "album" or "track".
    kind: Mapped[str] = mapped_column(default="track")
    artist: Mapped[str]
    # The album or song title; the artist's name again for an artist entry.
    title: Mapped[str]
    # For a song: the album it was picked from, when known.
    album: Mapped[str | None] = mapped_column(default=None)
    station_name: Mapped[str | None] = mapped_column(default=None)
    artist_mbid: Mapped[str | None] = mapped_column(default=None, index=True)
    release_group_mbid: Mapped[str | None] = mapped_column(default=None, index=True)
    recording_mbid: Mapped[str | None] = mapped_column(default=None)
    # Release-group primary type of an album entry (Album, EP, Single...).
    release_type: Mapped[str | None] = mapped_column(default=None)
    # An album or song left out of a wanted artist ("not this one").
    excluded: Mapped[bool] = mapped_column(default=False)
    # The library track this became once a scan found it (see
    # app.want_list); cleared again if that track is removed.
    media_item_id: Mapped[int | None] = mapped_column(
        ForeignKey("media_items.id"), default=None, index=True
    )
    # For an album: owned/total tracks of its edition the last time it was
    # matched (total stays None until the tracklist is known).
    owned_count: Mapped[int | None] = mapped_column(default=None)
    total_count: Mapped[int | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class ExternalCache(Base):
    """Server-wide cache of provider answers (ListenBrainz, MusicBrainz,
    Wikipedia) keyed by what was asked - shared by every user, since the
    answers don't depend on who asked. A row with an empty payload is a
    remembered miss. See app.external_cache."""

    __tablename__ = "external_cache"

    provider: Mapped[str] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(primary_key=True)
    payload_json: Mapped[str] = mapped_column(Text)
    fetched_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)


class ArtistMetadataCache(Base):
    """Caches what ListenBrainz said about an artist name (its similar
    artists), so repeated discovery/autoplay lookups do not re-hit that API.
    Entries use a longer success TTL and a short negative-cache TTL, and are
    shared across users. `mbid` is the MusicBrainz ID it was asked by;
    `similar_artists_json` holds the cached names."""

    __tablename__ = "artist_metadata_cache"

    artist: Mapped[str] = mapped_column(primary_key=True)
    mbid: Mapped[str | None] = mapped_column(default=None)
    similar_artists_json: Mapped[list[str] | None] = mapped_column(JSONText(varchar=True), default=None)
    # {name: ListenBrainz match 0..1} for the same artists, weighting the
    # similar-artist bonus (app.discovery._artists_similar_by_genre).
    similar_scores_json: Mapped[dict[str, float] | None] = mapped_column(
        JSONText(sort_keys=True, varchar=True), default=None
    )
    fetched_at: Mapped[datetime] = mapped_column(default=utcnow)


class DiscoveryState(Base):
    """Persists the last "Recommended"/"Discover new artists" results and
    the daily auto-refresh schedule per user, so they survive a page
    refresh or tab switch instead of living only in component state."""

    __tablename__ = "discovery_state"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)

    discovery_seed_artists_json: Mapped[list[str]] = mapped_column(JSONText(empty=list, varchar=True), default=list)
    discovery_similar_artists_json: Mapped[list[str]] = mapped_column(JSONText(empty=list, varchar=True), default=list)
    discovery_tracks_added: Mapped[int] = mapped_column(default=0)
    discovery_updated_at: Mapped[datetime | None] = mapped_column(default=None)

    explore_seed_artists_json: Mapped[list[str]] = mapped_column(JSONText(empty=list, varchar=True), default=list)
    explore_results_json: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONText(empty=list, varchar=True), default=list
    )
    explore_updated_at: Mapped[datetime | None] = mapped_column(default=None)

    auto_refresh_enabled: Mapped[bool] = mapped_column(default=True)
    auto_refresh_hour: Mapped[int] = mapped_column(default=6)
    last_auto_refresh_date: Mapped[str | None] = mapped_column(default=None)


class CustomArt(Base):
    """User-uploaded artwork, shared library-wide and kept only in the app
    (never written into audio files). One row per (entity_type, entity_key):
    media_item/playlist use the row id as text; album is "artist\x1falbum"
    (empty artist for none); artist is the name. `path` and `thumb_path` are
    file names inside ``settings.custom_art_dir``."""

    __tablename__ = "custom_art"
    __table_args__ = (UniqueConstraint("entity_type", "entity_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    entity_type: Mapped[str]
    entity_key: Mapped[str]
    path: Mapped[str]
    thumb_path: Mapped[str]
    # Where the image came from: "upload", "local" (a file's own picture),
    # "coverartarchive" or "wikimedia"; NULL for older art whose source was
    # not recorded.
    source: Mapped[str | None] = mapped_column(default=None)
    # Credit a licence asks to show with the image (Wikimedia Commons):
    # {"author", "license", "license_url", "page_url"}.
    credit_json: Mapped[dict[str, Any] | None] = mapped_column(JSONText(), default=None)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow)


class AudioFeatures(Base):
    """What a track sounds like, from local audio analysis
    (app.audio_features; filled by app.audio_analysis in the background).
    One row per analysed track, failures included so a broken file is not
    retried until it changes (its mtime or size) or the analyser's version
    goes up. Deleted with the track (app.cleanup)."""

    __tablename__ = "audio_features"

    media_item_id: Mapped[int] = mapped_column(ForeignKey("media_items.id"), primary_key=True)
    # app.audio_features.ANALYSIS_VERSION that produced the row.
    version: Mapped[int]
    # Lets app.audio_similarity load only rows newer than its last refresh.
    analysed_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    # The file the vector came from: a scan that sees the file change updates
    # media_items' mtime/size, and the mismatch queues the track again.
    source_mtime: Mapped[float]
    source_size: Mapped[int | None] = mapped_column(default=None)
    # Why analysis failed ("ffmpeg exited 1", "too short or silent", ...);
    # NULL when it worked.
    error: Mapped[str | None] = mapped_column(default=None)
    # Little-endian float32 values in app.audio_features.FEATURE_NAMES
    # order (180 bytes in version 1); NULL on failure.
    vector: Mapped[bytes | None] = mapped_column(default=None)
    # A few values from the vector worth filtering or sorting on directly.
    tempo_bpm: Mapped[float | None] = mapped_column(default=None)
    loudness_db: Mapped[float | None] = mapped_column(default=None)
    brightness_hz: Mapped[float | None] = mapped_column(default=None)
    # Tonic pitch class (0 = C ... 11 = B) and mode (1 major, 0 minor).
    musical_key: Mapped[int | None] = mapped_column(default=None)
    mode: Mapped[int | None] = mapped_column(default=None)
    # Seconds of audio the analysis looked at (at most 90).
    analysed_seconds: Mapped[float | None] = mapped_column(default=None)


class ActivityEvent(Base):
    """One line of the admin's stored activity log (app.activity_log):
    sign-ins, library tasks, edits and server errors. A short human-readable
    summary only - never a password, token, key, cookie or request body.
    Bounded by app.cleanup.prune_activity_events (age and a row cap)."""

    __tablename__ = "activity_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Naive UTC, like every other timestamp here.
    created_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    # The app.activity event name: "auth.login", "task.finish", "server.error"...
    kind: Mapped[str]
    # "info", "warning" or "error".
    level: Mapped[str]
    # The signed-in username behind the action; NULL for a failed sign-in,
    # a background task or a request nobody was signed in for.
    actor: Mapped[str | None] = mapped_column(default=None)
    summary: Mapped[str]
    # The library root the event is about, and its display name at the time
    # (kept as text: the root may be deleted later).
    root_id: Mapped[int | None] = mapped_column(default=None)
    context: Mapped[str | None] = mapped_column(default=None)
    # The X-Request-ID of the request that caused it, to find its log lines.
    request_id: Mapped[str | None] = mapped_column(default=None)
