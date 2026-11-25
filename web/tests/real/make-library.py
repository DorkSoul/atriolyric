"""Write a tiny tagged WAV album for the real two-device suite.

Usage (from server/): uv run --frozen python ../web/tests/real/make-library.py DIR
"""

import sys
import wave
from pathlib import Path

from mutagen.id3 import TALB, TIT2, TPE1, TRCK
from mutagen.wave import WAVE

# (title, seconds): distinct lengths; "Two" is short so a natural end is quick.
TRACKS = [("One", 20), ("Two", 3), ("Three", 20), ("Four", 20)]

out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)
for number, (title, seconds) in enumerate(TRACKS, start=1):
    path = out / f"{number:02d} {title}.wav"
    with wave.open(str(path), "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\x00\x00" * 8000 * seconds)
    audio = WAVE(str(path))
    audio.add_tags()
    audio.tags.add(TIT2(encoding=3, text=title))
    audio.tags.add(TPE1(encoding=3, text="Real Artist"))
    audio.tags.add(TALB(encoding=3, text="Real Album"))
    audio.tags.add(TRCK(encoding=3, text=str(number)))
    audio.save()
