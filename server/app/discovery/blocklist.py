"""Labels that are not a kind of music: never an automatic mix, never
evidence that two artists sound alike.

MusicBrainz and ListenBrainz folksonomy tags mix genres with listeners' own
bookkeeping - "seen live", "favourites", "albums I own", "under 2000
listeners" - and with judgements ("awesome", "beautiful"). A mix called
"seen live" is a random draw from everything, and two artists sharing
"favorites" are not alike, so automatic choices (the daily mixes, a mix's
co-occurring fill, a cluster mix's name) and the label similarity of
app.discovery.similar leave these out. Specify mix still offers every label:
what the user picks on purpose is theirs.

What is blocked, matched on the normalised name (app.metadata_taxonomy
.normalise_name - lower case, single spaces):

* BLOCKED_LABELS, an explicit list;
* a name that is only digits (a year, a count) or contains one of
  BLOCKED_SUBSTRINGS ("favo", "seen live", "listeners", "spotify");
* a name with one of BLOCKED_WORDS as a whole word ("seen", "own", "owned",
  "love", "loved", ...);
* the names in settings.mix_blocked_labels (ATRIOLYRIC_MIX_BLOCKED_LABELS,
  a JSON list), for a library's own oddities.

Decade tags ("80s", "1990s", "'70s") are blocked as automatic MIX labels
only: an "80s" mix jumps from synth-pop to thrash to hip hop, so it is not a
coherent daily mix. They stay similarity evidence: alongside an artist's
genres the era does say something about its sound ("80s" + "metal" is not
"2010s" + "metal"), and the label index already weighs a label by rarity, so
a decade carried by a quarter of the library counts little.
"Male vocalists" / "female vocalists" stay allowed everywhere: they describe
the music, and are common enough to count little anyway."""

import re
from functools import lru_cache

from app.config import settings
from app.metadata_taxonomy import normalise_name

BLOCKED_LABELS = frozenset(
    {
        "seen live",
        "favorites",
        "favourites",
        "favorite",
        "favourite",
        "favorite songs",
        "favourite songs",
        "favorite artists",
        "favourite artists",
        "my favorites",
        "my favourites",
        "favs",
        "fav",
        "love",
        "loved",
        "loved tracks",
        "love it",
        "awesome",
        "amazing",
        "beautiful",
        "albums i own",
        "my music",
        "spotify",
        "check out",
        "to check out",
        "under 2000 listeners",
        "wishlist",
        "to listen",
        "best",
        "good",
        "great",
        "cool",
        "all",
        "music",
        "misc",
        "unknown",
        "various",
        "various artists",
    }
)
BLOCKED_SUBSTRINGS = ("favo", "seen live", "listeners", "spotify")
BLOCKED_WORDS = ("seen", "own", "owned", "love", "loved", "favs", "fav", "wishlist")

_NUMERIC = re.compile(r"^\d+$")
_WORDS = re.compile(r"\b(?:" + "|".join(re.escape(word) for word in BLOCKED_WORDS) + r")\b")
# "80s", "80's", "'80s", "1980s", "1980's".
_DECADE = re.compile(r"^'?(?:\d0|1[0-9]\d0|20\d0)'?s$")


@lru_cache(maxsize=16384)
def _not_music(key: str) -> bool:
    if not key:
        return True
    if key in BLOCKED_LABELS or _NUMERIC.match(key):
        return True
    if any(part in key for part in BLOCKED_SUBSTRINGS):
        return True
    return bool(_WORDS.search(key))


@lru_cache(maxsize=4)
def _extra_keys(names: tuple[str, ...]) -> frozenset[str]:
    return frozenset(normalise_name(name) for name in names)


def _extra(key: str) -> bool:
    return key in _extra_keys(tuple(settings.mix_blocked_labels))


def is_decade(name: str) -> bool:
    return bool(_DECADE.match(normalise_name(name)))


def blocked_for_similarity(name: str) -> bool:
    """Not evidence that two artists sound alike (decades are)."""
    key = normalise_name(name)
    return _not_music(key) or _extra(key)


def blocked_for_mixes(name: str) -> bool:
    """Never an automatic mix label (decades included)."""
    key = normalise_name(name)
    return _not_music(key) or bool(_DECADE.match(key)) or _extra(key)
