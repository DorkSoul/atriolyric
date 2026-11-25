from pathlib import Path
from typing import Any, Literal, Self

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.version import default_user_agent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ATRIOLYRIC_")

    database_path: Path = Path("data/atriolyric.db")
    # Set in the production image so FastAPI can serve the compiled SPA.
    # Local development leaves this unset and continues to use Vite.
    web_dist_path: Path | None = None
    # The Android app's signed APK, offered for download when the file exists
    # (see app/android_app.py). The image sets it; development has none.
    android_apk_path: Path | None = None
    default_username: str = "admin"
    # Verbosity of the app's own log lines in `docker logs`: DEBUG, INFO
    # (default), WARNING or ERROR (any case). DEBUG adds routine 401s and read
    # requests that INFO leaves out. Anything else fails at startup rather
    # than silently logging at the wrong level.
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    # Tracing for chasing playback glitches: logs every API request (reads
    # and streams included, with address, Range and user agent), every
    # playback WebSocket frame and the web app's own debug events
    # (POST /api/client-log/debug). Very chatty and a per-user listening
    # history, so it is off unless someone turns it on to debug.
    verbose_logging: bool = False
    # Set both to create the first admin on a fresh database without the
    # setup screen (headless installs). Left unset, startup logs a one-time
    # setup code and the web app asks for it to create the first account.
    default_password: str | None = None
    audio_extensions: tuple[str, ...] = (
        ".mp3",
        ".flac",
        ".ogg",
        ".m4a",
        ".wav",
        ".opus",
        ".wma",
        ".aac",
    )
    video_extensions: tuple[str, ...] = (
        ".mp4",
        ".mkv",
        ".mov",
        ".webm",
        ".avi",
        ".m4v",
    )

    # Metadata/discovery providers - all optional, all degrade gracefully
    # when unset (see app/metadata_providers/).
    # Sent on every outbound request. Left unset, it names this release
    # (from the built frontend's version.json, see app.version).
    user_agent: str = ""
    # Whether the session cookie carries the Secure flag (sent over HTTPS
    # only) and responses carry Strict-Transport-Security. "auto" decides per
    # request: on when it arrived over HTTPS, directly or through a proxy that
    # sets X-Forwarded-Proto, so plain-HTTP home installs and HTTPS ones both
    # work. "true" and "false" force it either way.
    cookie_secure: Literal["auto", "true", "false"] = "auto"
    allow_private_radio_hosts: bool = False
    cache_max_bytes: int = 20 * 1024 * 1024 * 1024
    # Hardware H.264 encoding for on-the-fly video transcoding (HLS):
    # "auto" uses VAAPI when /dev/dri/renderD128 exists and works; "vaapi"
    # is the same but logs an error when it cannot be used (it still falls
    # back to software so playback keeps working); "none" always encodes in
    # software. Anything else fails at startup.
    hwaccel: Literal["auto", "vaapi", "none"] = "auto"
    # Video and its audio-only background rendition each take a session.
    hls_max_sessions: int = 4
    # Cap on cached transcoded segments (kept ~10 minutes, never permanently).
    hls_cache_max_bytes: int = 4 * 1024 * 1024 * 1024
    # Cap on the cached video download renditions (rendition_dir), least
    # recently used evicted first.
    rendition_cache_max_bytes: int = 20 * 1024 * 1024 * 1024
    # Extra origins allowed to call the API and open the playback WebSocket
    # besides the page's own host. Empty by default: the Vite dev server
    # proxies /api and /ws same-origin, so nothing else is needed. Set as a
    # JSON list, e.g. ATRIOLYRIC_ALLOWED_ORIGINS='["https://music.example.com"]'.
    allowed_origins: tuple[str, ...] = ()
    # Play history older than this many days is deleted by the scheduled
    # maintenance. 0 keeps it forever (the default): play counts, most played,
    # recently played and discovery all read it, so pruning changes them.
    play_history_retention_days: int = Field(default=0, ge=0)
    # The admin's stored activity log (sign-ins, scans, edits, server errors)
    # keeps this many days; 0 keeps it forever. Either way it never holds
    # more than activity_log_max_rows rows (the oldest go first).
    activity_log_retention_days: int = Field(default=30, ge=0)
    activity_log_max_rows: int = Field(default=50_000, ge=100)
    # Mixes, Discovery, Song Mix and autoplay leave out tracks shorter than
    # this many seconds (intros, skits, interludes, silent hidden tracks
    # rarely run past 45 s; a real song rarely runs under it) ...
    mix_min_track_seconds: float = Field(default=45.0, ge=0)
    # ... and longer than this (continuous DJ mixes, full-album rips and
    # concert recordings stored as one file; few songs run past 20 minutes).
    # A track whose length is unknown is kept. What the user picks
    # themselves - a Song Mix's seed, an album, a playlist - is never
    # filtered.
    mix_max_track_seconds: float = Field(default=1200.0, gt=0)
    # Whether tracks from albums MusicBrainz types as Live may appear in
    # those automatic picks. Allowed by default: many libraries hold live
    # albums on purpose. Spoken word, interview, audiobook and audio drama
    # albums are always left out.
    mix_allow_live_albums: bool = True
    # Autoplay (what plays once a queue runs out) skips tracks played in the
    # last this many hours, besides whatever is already in the queue: a day
    # covers a long listening session and the next morning's, so autoplay
    # does not bring back what was just heard. 0 turns the play-history check
    # off.
    autoplay_recent_hours: float = Field(default=24.0, ge=0)

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper(cls, value: Any) -> Any:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("hwaccel", mode="before")
    @classmethod
    def _lower(cls, value: Any) -> Any:
        return value.strip().lower() if isinstance(value, str) else value

    @field_validator("cookie_secure", mode="before")
    @classmethod
    def _cookie_secure_word(cls, value: Any) -> Any:
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, str):
            word = value.strip().lower()
            return {"1": "true", "yes": "true", "on": "true",
                    "0": "false", "no": "false", "off": "false"}.get(word, word)
        return value

    @model_validator(mode="after")
    def _default_user_agent(self) -> Self:
        if not self.user_agent.strip():
            self.user_agent = default_user_agent(self.web_dist_path)
        return self

    @property
    def scan_extensions(self) -> tuple[str, ...]:
        return self.audio_extensions + self.video_extensions

    @property
    def thumbnails_dir(self) -> Path:
        return self.database_path.parent / "thumbnails"

    @property
    def art_dir(self) -> Path:
        return self.database_path.parent / "art"

    @property
    def custom_art_dir(self) -> Path:
        return self.database_path.parent / "custom_art"

    @property
    def art_thumb_dir(self) -> Path:
        return self.database_path.parent / "art_thumbs"

    @property
    def transcode_dir(self) -> Path:
        return self.database_path.parent / "transcodes"

    @property
    def rendition_dir(self) -> Path:
        return self.database_path.parent / "renditions"

    @property
    def hls_dir(self) -> Path:
        return self.database_path.parent / "hls"

    @property
    def credential_key_path(self) -> Path:
        return self.database_path.parent / ".credential-key"

    # Local audio analysis (app.audio_analysis): a background pass decodes
    # 90 seconds of each track with ffmpeg and stores a "sounds like" vector
    # for similarity and smooth ordering. Nothing leaves the server. It
    # starts once the loudness backfill has nothing left to do; false turns
    # it off (stored vectors stay and are still used).
    audio_analysis: bool = True
    # How many tracks are analysed at once (each is one niced ffmpeg plus
    # numpy work on one core). With 1, analysis also pauses while the
    # loudness backfill measures, so only one heavy ffmpeg runs at a time;
    # each step above 1 adds a worker that runs alongside it.
    audio_analysis_concurrency: int = Field(default=1, ge=1, le=16)
    # Extra genre/tag names that must never become an automatic mix or count
    # as two artists being alike, on top of the built-in list of non-genre
    # tags ("seen live", "favourites", ... - app.discovery.blocklist). A JSON
    # list, e.g. ATRIOLYRIC_MIX_BLOCKED_LABELS='["my uploads", "car"]'.
    # Specify mix still offers them.
    mix_blocked_labels: tuple[str, ...] = ()


settings = Settings()


def thumbnail_path(media_item_id: int) -> Path:
    return settings.thumbnails_dir / f"{media_item_id}.jpg"


_ART_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".gif")


def find_art_path(media_item_id: int) -> Path | None:
    for ext in _ART_EXTENSIONS:
        path = settings.art_dir / f"{media_item_id}{ext}"
        if path.is_file():
            return path
    return None


def art_thumb_path(media_item_id: int) -> Path:
    # Always .jpg regardless of the original's format - one predictable
    # output format for a resize step that doesn't need to preserve
    # transparency (album art never has any).
    return settings.art_thumb_dir / f"{media_item_id}.jpg"


def is_https(scheme: str, forwarded_proto: str | None) -> bool:
    """Whether the session cookie should be Secure for a request with this
    scheme and X-Forwarded-Proto header. A client faking the header over plain
    HTTP only makes its own cookie unusable, so the header is believed from
    any peer."""
    mode = settings.cookie_secure
    if mode in (True, "true"):
        return True
    if mode in (False, "false"):
        return False
    proto = (forwarded_proto or "").split(",")[0].strip().lower()
    return scheme in ("https", "wss") or proto == "https"
