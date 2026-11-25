"""Write the extra tagged WAV albums the Android phone suite needs.

Usage (from server/): uv run --frozen python ../web/tests/android/make-phone-albums.py DIR

The real suite's make-library.py writes one fixed album at import time, so it
cannot be reused for other titles; this is the same recipe with other tracks:
- "Gapless Album": four 8 s tracks, so several track boundaries pass while the
  phone's screen is off;
- "Threshold Album": three 24 s tracks (half = 12 s), so a play past half and
  an early skip can be told apart.
"""

import sys
import wave
from pathlib import Path

from mutagen.id3 import TALB, TIT2, TPE1, TRCK
from mutagen.wave import WAVE

ARTIST = "Phone Artist"
ALBUMS = {
    "Gapless Album": [("Gap One", 8), ("Gap Two", 8), ("Gap Three", 8), ("Gap Four", 8)],
    "Threshold Album": [("Half One", 24), ("Half Two", 24), ("Half Three", 24)],
}

out = Path(sys.argv[1])
for album, tracks in ALBUMS.items():
    folder = out / album
    folder.mkdir(parents=True, exist_ok=True)
    for number, (title, seconds) in enumerate(tracks, start=1):
        path = folder / f"{number:02d} {title}.wav"
        with wave.open(str(path), "w") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(8000)
            # A quiet tone rather than digital silence, in case a decoder or
            # the OS treats an all-zero stream as "not playing".
            frame = bytearray()
            for i in range(8000 * seconds):
                frame += (300 if (i // 20) % 2 else -300).to_bytes(2, "little", signed=True)
            w.writeframes(bytes(frame))
        audio = WAVE(str(path))
        audio.add_tags()
        audio.tags.add(TIT2(encoding=3, text=title))
        audio.tags.add(TPE1(encoding=3, text=ARTIST))
        audio.tags.add(TALB(encoding=3, text=album))
        audio.tags.add(TRCK(encoding=3, text=str(number)))
        audio.save()
