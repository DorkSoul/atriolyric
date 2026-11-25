"""Telling two tracks' titles apart from two versions of one song.

Shared by owned-track matching (app.owned_match), the want-list catalogue and
mix de-duplication (app.discovery.eligibility), so they agree on what counts
as "the same recording"."""

import re
import unicodedata
from functools import lru_cache

# Notes that name the same recording differently ("Song - 2011 Remaster",
# "Song (feat. X)"): a bracket, or the last " - " part of a title, holding
# one of these is dropped.
_BRACKET_NOTE = (
    r"remaster(?:ed)?|feat\.?|ft\.?|featuring|explicit|clean|"
    r"deluxe|bonus track|single version|album version|mono|stereo"
)
_DASH_NOTE = r"remaster(?:ed)?|single version|album version|mono|stereo"
# ... unless it also names another performance or arrangement: "Song (Live
# at Wembley; 2011 Remaster)", "Song (Clean Bandit Remix)" and "Song (Live
# in Stereo)" are not the studio song, and stay whole. ("Mono mix" and
# "stereo mix" are the same recording; any other mix is not.)
_OTHER_PERFORMANCE_RE = re.compile(
    r"\b(?:live|concert|unplugged|acoustic|remix(?:e[sd])?|rmx|(?<!mono )(?<!stereo )mix|dub|"
    r"demo|instrumental|karaoke|a\s?cappella|edit|extended|cover|sessions?|rehearsal|"
    r"outtake|take|alternate|alternative|orchestral|piano|radio|re-?recorded|reprise)\b",
    re.IGNORECASE,
)
_BRACKET_RE = re.compile(r"\s*[\(\[]([^\)\]]*)[\)\]]")
_BRACKET_NOTE_RE = re.compile(rf"\b(?:{_BRACKET_NOTE})(?!\w)", re.IGNORECASE)
_DASH_NOTE_RE = re.compile(rf"\b(?:{_DASH_NOTE})\b", re.IGNORECASE)
# A bracket that opens with a guest credit is a list of names up to its
# first ";", " - " or " / ": "(feat. Take That)" and "(feat. 2 Live Crew)"
# are credits, "(feat. X; Live)" is a live take.
_CREDIT_RE = re.compile(r"\s*(?:feat\.?|ft\.?|featuring)(?!\w)(?:(?!;|\s-\s|\s/\s).)*", re.IGNORECASE)
_PUNCTUATION_RE = re.compile(r"[^\w\s]")


def _drop_bracket(match: re.Match[str]) -> str:
    inside = match.group(1)
    if not _BRACKET_NOTE_RE.search(inside):
        return match.group(0)
    credit = _CREDIT_RE.match(inside)
    rest = inside[credit.end():] if credit else inside
    return match.group(0) if _OTHER_PERFORMANCE_RE.search(rest) else ""


def _strip_version_notes(title: str) -> str:
    stripped = _BRACKET_RE.sub(_drop_bracket, title)
    # "Song - Single Version - 2011 Remaster": each trailing part in turn.
    while True:
        head, dash, tail = stripped.rpartition(" - ")
        if not dash or not _DASH_NOTE_RE.search(tail) or _OTHER_PERFORMANCE_RE.search(tail):
            return stripped
        stripped = head.rstrip()


def normalize_title(title: str | None) -> str:
    """A title with version notes, accents, case and punctuation removed:
    "Song (2011 Remaster)" and "song" give the same key, "Song (Live)" and
    "Song (Live; 2011 Remaster)" do not. Only a missing or blank title gives
    "": a title that is all punctuation ("+", "÷", "!!!") keeps itself as its
    key, so two of them never match each other by having no key at all."""
    if not title:
        return ""
    return _normalize_title(title)


# Mixes normalise the titles of every artist they draw from, again for each
# mix and autoplay batch: a bounded memo of recent titles saves the work.
@lru_cache(maxsize=65536)
def _normalize_title(title: str) -> str:
    stripped = _strip_version_notes(title)
    if stripped.isascii():
        text = stripped.casefold()  # nothing to decompose
    else:
        decomposed = unicodedata.normalize("NFKD", stripped)
        text = "".join(ch for ch in decomposed if not unicodedata.combining(ch)).casefold()
    text = text.replace("&", "and")
    key = " ".join(_PUNCTUATION_RE.sub("", text).split())
    if key:
        return key
    # Nothing but punctuation (or nothing but a version note): the title
    # itself, folded, with its notes dropped when anything is left.
    return " ".join((stripped if stripped.strip() else title).casefold().split())
