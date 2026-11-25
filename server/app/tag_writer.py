from pathlib import Path

import mutagen
from mutagen.id3 import ID3, TALB, TCON, TIT2, TPE1, TRCK

_EASY_FIELDS = {
    "title": "title",
    "artist": "artist",
    "album": "album",
    "genre": "genre",
}
_ID3_FRAMES = {"title": TIT2, "artist": TPE1, "album": TALB, "genre": TCON}


def _is_cleared(value: str) -> bool:
    return not value.strip()


def write_tags(
    path: Path,
    *,
    title: str | None = None,
    artist: str | None = None,
    album: str | None = None,
    genre: str | None = None,
    track_number: int | None = None,
) -> bool:
    """Write tags back to the file itself via mutagen. Only fields passed
    (not None) are touched; an empty or whitespace-only value removes the
    tag, so the file reads back exactly like one that never had it (the
    scanner stores a missing tag as NULL). Returns whether the write
    actually succeeded - never raises, so a read-only mount or unsupported
    format surfaces as a clean error to the caller instead of a 500.

    Two paths, same as the scanner's read side: mutagen's "easy" dict-style
    API works for MP3/FLAC/OGG/M4A, but raises TypeError on WAV/AIFF's raw
    ID3 tags object (`audio['title'] = x` there fails with "not a Frame
    instance"), so those fall back to setting real ID3
    frames instead.
    """
    values = {"title": title, "artist": artist, "album": album, "genre": genre}
    fields_to_set = {k: v for k, v in values.items() if v is not None}
    if not fields_to_set and track_number is None:
        return True  # nothing requested

    try:
        audio = mutagen.File(path, easy=True)
        if audio is None:
            return False
        if audio.tags is None:
            audio.add_tags()
        if isinstance(audio.tags, ID3):
            # WAV/AIFF: a delete on the raw ID3 object would quietly do
            # nothing (it has no "title" key), so go straight to frames.
            raise TypeError("raw ID3 tags")
        for key, value in fields_to_set.items():
            if not _is_cleared(value):
                audio[_EASY_FIELDS[key]] = value
            elif _EASY_FIELDS[key] in audio:
                del audio[_EASY_FIELDS[key]]
        if artist is not None:
            # A hand-typed artist replaces the multi-value list too, or the
            # next scan would read the old ARTISTS tag back (app.scanner).
            for key in ("artists",):
                try:
                    if key in audio:
                        del audio[key]
                except (KeyError, ValueError):
                    pass
        if track_number is not None:
            audio["tracknumber"] = str(track_number)
        audio.save()
        return True
    except TypeError:
        pass  # raw ID3 tags object (WAV/AIFF) - fall through to the frame-based path
    except Exception:  # noqa: BLE001 - mutagen raises many different errors on a corrupt or odd file; treat all as "nothing written"
        return False

    try:
        audio = mutagen.File(path)
        if audio is None:
            return False
        if audio.tags is None:
            audio.add_tags()
        tags = audio.tags
        assert tags is not None  # add_tags() above guarantees a tag object
        for key, value in fields_to_set.items():
            frame = _ID3_FRAMES[key]
            if _is_cleared(value):
                tags.delall(frame.__name__)
            else:
                tags.setall(frame.__name__, [frame(encoding=3, text=value)])
        if artist is not None:
            tags.delall("TXXX:ARTISTS")
        if track_number is not None:
            tags.setall("TRCK", [TRCK(encoding=3, text=str(track_number))])
        audio.save()
        return True
    except Exception:  # noqa: BLE001 - mutagen raises many different errors on a corrupt or odd file; treat all as "nothing written"
        return False
