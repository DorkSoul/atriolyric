"""What several discovery modules share: the insufficient-matches error, shared limits and the clock."""

from datetime import UTC, datetime


class InsufficientMatchesError(Exception):
    """Raised by generate_playlist_from_track when there aren't enough
    similar-enough tracks in the library to make a real mix - the seed
    track alone isn't worth creating/overwriting the managed "Song Mix"
    slot for."""


# Most tracks one artist may contribute to a 40-track genre or specified mix
# while others are available (7.5% of it).
MIX_MAX_PER_ARTIST = 3
# The same for the 25-track mixes - Discovery, Song Mix, artist and album
# mixes - and for each 25-track autoplay batch: two keeps about the same
# share (8%), and with the 20 similar artists those draw from, the first
# round already gives most artists one track. The cap is soft: it is lifted
# only when too few artists are left to fill the mix.
SHORT_MIX_MAX_PER_ARTIST = 2

# The most one track's draw weight may reach once likes, listen outcomes,
# familiarity and a mix's own weights are multiplied (see
# app.discovery.familiarity): twice a liked track's boost, so no single
# track or artist can take over a mix.
MAX_TRACK_WEIGHT = 3.0


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)
