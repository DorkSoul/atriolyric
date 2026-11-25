"""Radio stations seeded into a fresh install (see
``app.main.seed_default_radio_stations``)."""

# Free, keyless, no-login internet radio streams - a starting point so a
# fresh install isn't a blank Radio tab. Every stream URL is verified live
# against the actual station before being added here. SomaFM is deliberately
# absent: its terms ask for prior permission to re-stream its channels.
# Installs that already have SomaFM rows keep them (the seed never deletes).
DEFAULT_RADIO_STATIONS = [
    (
        "Radio Paradise: Main Mix",
        "https://stream.radioparadise.com/mp3-192",
        "Eclectic freeform mix spanning rock, world, electronica and more.",
        "Radio Paradise",
    ),
    (
        "Radio Paradise: Mellow Mix",
        "https://stream.radioparadise.com/mellow-192",
        "A more laid-back, acoustic-leaning blend for chilling out.",
        "Radio Paradise",
    ),
    (
        "Radio Paradise: Rock Mix",
        "https://stream.radioparadise.com/rock-192",
        "Guitar-driven rock across the decades.",
        "Radio Paradise",
    ),
    (
        "Radio Paradise: Global Mix",
        "https://stream.radioparadise.com/global-192",
        "Music from around the world, genres and borders optional.",
        "Radio Paradise",
    ),
    (
        "Radio Paradise: Beyond",
        "https://stream.radioparadise.com/beyond-192",
        "Adventurous and experimental left-field selections.",
        "Radio Paradise",
    ),
    (
        "Radio Paradise: Serenity",
        "https://stream.radioparadise.com/serenity",
        "Calm instrumental and ambient textures for focus or sleep.",
        "Radio Paradise",
    ),
    (
        "Radio Paradise: KFAT",
        "https://stream.radioparadise.com/kfat-192",
        "A revival of the legendary freeform country/roots station KFAT.",
        "Radio Paradise",
    ),
    (
        "KEXP",
        "https://kexp-mp3-128.streamguys1.com/kexp128.mp3",
        "Independent music radio from Seattle, with an eclectic mix of live sessions and new music.",
        "KEXP",
    ),
    (
        "FIP",
        "https://direct.fipradio.fr/live/fip-midfi.mp3",
        "French public radio's eclectic, commercial-free mix of rock, jazz, world, soul and more.",
        "FIP",
    ),
    (
        "Radio Swiss Jazz",
        "https://stream.srg-ssr.ch/m/rsj/aacp_96",
        "Jazz, soul and blues from Swiss public radio.",
        "Radio Swiss Jazz",
    ),
    (
        "RTÉ Radio 1",
        "https://www.rte.ie/manifests/radio1.m3u",
        "Irish national radio with news, current affairs, culture, sport and music.",
        "RTÉ",
    ),
    (
        "RTÉ 2FM",
        "https://www.rte.ie/manifests/2fm.m3u",
        "Irish national music and entertainment radio.",
        "RTÉ",
    ),
    (
        "RTÉ lyric fm",
        "https://25633.live.streamtheworld.com/RTE_LYRIC_FM.mp3?tdsdk=rte",
        "Classical music, jazz and arts programming from RTÉ.",
        "RTÉ",
    ),
    (
        "Newstalk",
        "https://edgex.audioxi.com/NT",
        "Irish national news, current affairs, sport and talk radio.",
        "Newstalk",
    ),
    (
        "Today FM",
        "https://edgex.audioxi.com/TD",
        "Irish national music, entertainment and talk radio.",
        "Today FM",
    ),
    (
        "WFMU Freeform",
        "https://stream0.wfmu.org/freeform-128k",
        "Listener-supported freeform radio from New York, with eclectic music and talk.",
        "WFMU",
    ),
    (
        "WFMU Rock'n'Soul Radio",
        "https://stream0.wfmu.org/rocknsoul",
        "Rock, soul and related sounds from WFMU's web-only stream.",
        "WFMU",
    ),
    (
        "WFMU Give the Drummer Radio",
        "https://stream0.wfmu.org/drummer",
        "A WFMU web-only stream exploring rhythm, jazz and global music.",
        "WFMU",
    ),
    (
        "WFMU Sheena's Jungle Room",
        "https://stream0.wfmu.org/sheena",
        "A WFMU web-only stream for lowbrow sounds and other oddities.",
        "WFMU",
    ),
    (
        "Radio Swiss Classic",
        "https://livestreaming-node-4.srg-ssr.ch/srgssr/rsc_de/mp3/128",
        "Classical music from Swiss public radio.",
        "Radio Swiss",
    ),
    (
        "Radio Swiss Pop",
        "https://livestreaming-node-3.srg-ssr.ch/srgssr/rsp/mp3/128",
        "Pop and soft rock from Swiss public radio.",
        "Radio Swiss",
    ),
]
