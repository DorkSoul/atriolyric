"""Decide what an image really is from its bytes.

Art reaches the app from places that can claim any type - a tag's MIME
string, a file suffix, a remote server's Content-Type - and a served
`text/html` or `image/svg+xml` would run script on the app's origin. So the
type is always taken from the magic bytes, and only these formats exist.
"""

IMAGE_TYPES = {".jpg": "image/jpeg", ".png": "image/png", ".webp": "image/webp", ".gif": "image/gif"}


def sniff_image(data: bytes) -> str | None:
    """File extension for the image format `data` really is, by magic bytes."""
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    return None


def sniff_mime(data: bytes) -> str | None:
    """MIME type for the image format `data` really is, or None."""
    ext = sniff_image(data)
    return IMAGE_TYPES[ext] if ext else None


# Larger than any real cover, smaller than a decompression bomb: ffmpeg would
# allocate width x height x 4 bytes (about 256 MB at the limit) just to make
# a thumbnail, and a few KB of header can claim 65535 x 65535.
MAX_IMAGE_DIMENSION = 8000

# Marker bytes of a JPEG start-of-frame segment (SOF0..SOF15 except DHT 0xC4,
# JPG 0xC8 and DAC 0xCC, which share the range but are not frames).
_JPEG_SOF = frozenset({0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF})


def _jpeg_dimensions(data: bytes) -> tuple[int, int] | None:
    pos = 2
    while pos + 4 <= len(data):
        if data[pos] != 0xFF:
            return None
        marker = data[pos + 1]
        if marker == 0xFF:  # fill byte before a marker
            pos += 1
            continue
        if marker in (0x01, 0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:  # no length field
            pos += 2
            continue
        length = int.from_bytes(data[pos + 2 : pos + 4], "big")
        if marker in _JPEG_SOF:
            frame = data[pos + 4 : pos + 9]  # precision, height, width
            if len(frame) < 5:
                return None
            return int.from_bytes(frame[3:5], "big"), int.from_bytes(frame[1:3], "big")
        if marker == 0xDA or length < 2:  # scan data reached without a frame header
            return None
        pos += 2 + length
    return None


def _webp_dimensions(data: bytes) -> tuple[int, int] | None:
    kind = data[12:16]
    if kind == b"VP8 " and data[23:26] == b"\x9d\x01\x2a" and len(data) >= 30:
        return (
            int.from_bytes(data[26:28], "little") & 0x3FFF,
            int.from_bytes(data[28:30], "little") & 0x3FFF,
        )
    if kind == b"VP8L" and data[20:21] == b"\x2f" and len(data) >= 25:
        bits = int.from_bytes(data[21:25], "little")
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    if kind == b"VP8X" and len(data) >= 30:
        return (
            int.from_bytes(data[24:27], "little") + 1,
            int.from_bytes(data[27:30], "little") + 1,
        )
    return None


def image_dimensions(data: bytes) -> tuple[int, int] | None:
    """(width, height) read from the image header without decoding anything,
    or None when the format is unknown or the header is truncated or
    malformed. Only the first few KB (more for a JPEG with big metadata
    blocks) are looked at."""
    ext = sniff_image(data)
    if ext == ".png":
        if len(data) >= 24 and data[12:16] == b"IHDR":
            return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
        return None
    if ext == ".gif":
        if len(data) >= 10:
            return int.from_bytes(data[6:8], "little"), int.from_bytes(data[8:10], "little")
        return None
    if ext == ".webp":
        return _webp_dimensions(data)
    if ext == ".jpg":
        return _jpeg_dimensions(data)
    return None


def too_large(data: bytes) -> bool:
    """Whether the header says the image is over MAX_IMAGE_DIMENSION on a side."""
    dims = image_dimensions(data)
    return dims is not None and max(dims) > MAX_IMAGE_DIMENSION


def decodable_size(data: bytes) -> bool:
    """Whether the header can be read and is within MAX_IMAGE_DIMENSION - the
    check to pass before handing bytes to a decoder."""
    dims = image_dimensions(data)
    return dims is not None and 0 < min(dims) and max(dims) <= MAX_IMAGE_DIMENSION
