"""Reading image dimensions from headers, and refusing oversized images
before any decoder sees them."""

from __future__ import annotations

import struct
import subprocess
from unittest.mock import patch

import pytest

from app import art, custom_art
from app.image_sniff import (
    MAX_IMAGE_DIMENSION,
    decodable_size,
    image_dimensions,
    too_large,
)


def png(width: int, height: int) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", 13) + b"IHDR" + struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
        + b"\x00" * 4
    )


def gif(width: int, height: int) -> bytes:
    return b"GIF89a" + struct.pack("<HH", width, height) + b"\x00\x00\x00"


def jpeg(width: int, height: int, *, sof: int = 0xC0, before: bytes = b"") -> bytes:
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00" + b"\x00" * 9
    frame = b"\xff" + bytes([sof]) + struct.pack(">HBHHB", 11, 8, height, width, 1) + b"\x01\x11\x00"
    return b"\xff\xd8" + app0 + before + frame + b"\xff\xda\x00\x02"


def webp_lossy(width: int, height: int) -> bytes:
    body = b"\x00\x00\x00" + b"\x9d\x01\x2a" + struct.pack("<HH", width, height)
    return b"RIFF" + struct.pack("<I", 4 + 8 + len(body)) + b"WEBP" + b"VP8 " + struct.pack("<I", len(body)) + body


def webp_lossless(width: int, height: int) -> bytes:
    bits = (width - 1) | ((height - 1) << 14)
    body = b"\x2f" + struct.pack("<I", bits)
    return b"RIFF" + struct.pack("<I", 4 + 8 + len(body)) + b"WEBP" + b"VP8L" + struct.pack("<I", len(body)) + body


def webp_extended(width: int, height: int) -> bytes:
    body = b"\x00\x00\x00\x00" + (width - 1).to_bytes(3, "little") + (height - 1).to_bytes(3, "little")
    return b"RIFF" + struct.pack("<I", 4 + 8 + len(body)) + b"WEBP" + b"VP8X" + struct.pack("<I", len(body)) + body


@pytest.mark.parametrize(
    "data",
    [
        png(640, 480),
        gif(640, 480),
        jpeg(640, 480),
        jpeg(640, 480, sof=0xC2),  # progressive
        jpeg(640, 480, before=b"\xff\xff\xff\xfe" + struct.pack(">H", 6) + b"hi!!"),  # fill bytes + comment
        webp_lossy(640, 480),
        webp_lossless(640, 480),
        webp_extended(640, 480),
    ],
    ids=["png", "gif", "jpeg", "jpeg-progressive", "jpeg-comment", "webp-vp8", "webp-vp8l", "webp-vp8x"],
)
def test_dimensions_are_read_from_each_format(data):
    assert image_dimensions(data) == (640, 480)
    assert decodable_size(data) is True
    assert too_large(data) is False


@pytest.mark.parametrize(
    "data",
    [png(8001, 10), png(10, 65535), gif(9000, 1), jpeg(8001, 10), jpeg(10, 9000),
     webp_lossy(9000, 10), webp_lossless(10, 9000), webp_extended(20000, 20000)],
)
def test_oversized_images_are_refused(data):
    assert too_large(data) is True
    assert decodable_size(data) is False


def test_the_limit_itself_is_allowed():
    assert MAX_IMAGE_DIMENSION == 8000
    assert decodable_size(png(8000, 8000)) is True


@pytest.mark.parametrize(
    "data",
    [
        b"", b"\x89PNG", png(10, 10)[:20], gif(10, 10)[:8], jpeg(10, 10)[:10],
        b"\xff\xd8\xff\xe0\x00", b"RIFF\x00\x00\x00\x00WEBP", webp_lossy(10, 10)[:28],
        b"\xff\xd8" + b"\x00" * 50, b"\xff\xd8\xff\xe0\x00\x00" + b"\x00" * 20,
        b"garbage" * 100,
    ],
)
def test_truncated_and_garbage_input_has_no_dimensions(data):
    assert image_dimensions(data) is None
    assert decodable_size(data) is False
    assert too_large(data) is False  # unknown is not "too large"; decodable_size gates decoding


def test_zero_sized_images_are_not_decodable():
    assert decodable_size(png(0, 10)) is False


def test_jpeg_without_a_frame_header_before_the_scan_has_no_size():
    data = b"\xff\xd8" + b"\xff\xda\x00\x02" + b"\x00" * 10
    assert image_dimensions(data) is None


@pytest.mark.parametrize("extension", ["png", "jpg", "gif", "webp"])
def test_dimensions_match_what_a_real_encoder_wrote(tmp_path, extension):
    path = tmp_path / f"x.{extension}"
    result = subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=123x77:rate=1",
         "-frames:v", "1", str(path)],
        capture_output=True,
    )
    if result.returncode != 0:
        pytest.skip(f"this ffmpeg cannot write {extension}")
    assert image_dimensions(path.read_bytes()) == (123, 77)


def test_store_custom_art_rejects_oversized_images_before_decoding(db_session, tmp_path, monkeypatch):
    monkeypatch.setattr(custom_art.settings, "database_path", tmp_path / "data" / "app.db")
    with patch.object(custom_art, "_resize_art") as resize:
        with pytest.raises(ValueError, match="too large"):
            custom_art.store_custom_art(db_session, "media_item", "1", png(60000, 60000))
    assert resize.call_count == 0


def test_resize_refuses_oversized_or_unreadable_images_without_running_ffmpeg(tmp_path):
    big = tmp_path / "big.png"
    big.write_bytes(png(60000, 60000))
    junk = tmp_path / "junk.png"
    junk.write_bytes(b"\x89PNG\r\n\x1a\nnot really")
    with patch("app.art.subprocess.run") as run:
        assert art._resize_art(big, tmp_path / "o.jpg") is False
        assert art._resize_art(junk, tmp_path / "o.jpg") is False
        assert art._resize_art(art.ArtBytes(png(60000, 1), "image/png"), tmp_path / "o.jpg") is False
    assert run.call_count == 0


def test_remote_and_embedded_art_over_the_limit_is_dropped():
    assert art._checked(png(10, 10) + b"\x00" * 10) is not None
    assert art._checked(png(60000, 60000)) is None
