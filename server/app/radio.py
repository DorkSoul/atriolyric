import logging
import re
import socket
import ssl
import threading
import time
from collections.abc import AsyncIterator, Callable, Generator, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from ipaddress import ip_address
from urllib.parse import urljoin, urlsplit

import anyio.to_thread

from app.config import settings

logger = logging.getLogger(__name__)

# Total time to connect, across every validated address of the station.
CONNECT_TIMEOUT = 10
# Per address, so a black-holed IPv6 address cannot use up the whole budget
# before the next address is tried.
ADDRESS_CONNECT_TIMEOUT = 4
# Socket timeout for every read once connected (headers and stream).
READ_TIMEOUT = 10
CHUNK_SIZE = 4096
MAX_HEADER_BYTES = 64 * 1024
MAX_REDIRECTS = 3
# A .pls/.m3u is a few lines naming the real stream; anything bigger is not a
# playlist we want to parse.
PLAYLIST_MAX_BYTES = 64 * 1024
REDIRECT_STATUSES = {301, 302, 303, 307, 308}
PLAYLIST_TYPES = {
    "audio/x-scpls",
    "audio/scpls",
    "application/pls+xml",
    "audio/x-mpegurl",
    "audio/mpegurl",
    "application/x-mpegurl",
    "application/vnd.apple.mpegurl",
}
PLAYLIST_EXTENSIONS = (".pls", ".m3u", ".m3u8")
# Each listener keeps a worker from the server's shared thread pool (the one
# that also serves audio and every other sync request) busy relaying, plus an
# upstream connection, for as long as they listen. Two per user covers a
# phone and a desktop; eight in total leaves most of the pool for the rest.
MAX_STREAMS_PER_USER = 2
MAX_STREAMS_TOTAL = 8

# In-memory "now playing" cache, keyed by station id. Only populated while
# someone is actively listening through our proxy (that's what parses the
# ICY metadata in the first place) - a station nobody's tuned into just
# won't have a fresh title. A background keep-alive-per-station connection
# would fix that but adds real resource/cleanup cost for a household app;
# not worth it here.
_now_playing: dict[int, dict] = {}

_slots_lock = threading.Lock()
_streams_by_user: dict[int, int] = {}


class RadioUnavailable(Exception):
    """The station could not be reached or did not answer with a stream.
    Raised before any audio is sent, so the endpoint can still answer with
    `status_code` (502, or 404 when the upstream says the stream is gone)."""

    def __init__(self, message: str, status_code: int = 502) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class RadioBusy(Exception):
    """Over one of the concurrent-stream caps."""


def get_now_playing(station_id: int) -> dict | None:
    return _now_playing.get(station_id)


@contextmanager
def stream_slot(user_id: int) -> Iterator[None]:
    """Count one radio stream for `user_id` while the block runs; RadioBusy
    when the per-user or the total cap is reached. Always released."""
    with _slots_lock:
        if sum(_streams_by_user.values()) >= MAX_STREAMS_TOTAL:
            raise RadioBusy("The server is relaying as many radio streams as it can - try again later")
        if _streams_by_user.get(user_id, 0) >= MAX_STREAMS_PER_USER:
            raise RadioBusy(
                f"You are already listening to {MAX_STREAMS_PER_USER} radio streams - stop one first"
            )
        _streams_by_user[user_id] = _streams_by_user.get(user_id, 0) + 1
    try:
        yield
    finally:
        with _slots_lock:
            left = _streams_by_user.get(user_id, 1) - 1
            if left > 0:
                _streams_by_user[user_id] = left
            else:
                _streams_by_user.pop(user_id, None)


def _ordered_addresses(infos: list) -> list[str]:
    """Resolver order without duplicates, IPv4 first: the container network
    is often IPv4-only, and a dual-stack station's IPv6 address then fails."""
    addresses = list(dict.fromkeys(str(info[4][0]) for info in infos))
    return sorted(addresses, key=lambda address: ip_address(address).version != 4)


def validate_station_url(
    url: str, *, resolve_dns: bool = False
) -> tuple[str, int, str, list[str]]:
    """(host, port, path, addresses). `addresses` are every address the host
    resolves to (only resolved with `resolve_dns`; an IP literal is its own
    address), all of them checked: a host with any private or reserved
    address is refused unless `allow_private_radio_hosts`. Connections go to
    these addresses only, never to the name, so a second lookup cannot swap
    in a different one."""
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"}:
        raise ValueError("Radio URL must use http or https")
    if parts.username is not None or parts.password is not None:
        raise ValueError("Radio URL must not contain credentials")
    host = parts.hostname
    if not host:
        raise ValueError("Radio URL must include a hostname")
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError as exc:
        raise ValueError("Radio URL has an invalid port") from exc
    path = parts.path or "/"
    if parts.query:
        path += f"?{parts.query}"

    addresses: list[str] = []
    try:
        addresses.append(str(ip_address(host)))
    except ValueError:
        if resolve_dns:
            try:
                addresses = _ordered_addresses(
                    socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
                )
            except OSError as exc:
                raise ValueError("Radio hostname could not be resolved") from exc
            if not addresses:
                raise ValueError("Radio hostname could not be resolved") from None
    if addresses and not settings.allow_private_radio_hosts:
        if any(not ip_address(address).is_global for address in addresses):
            raise ValueError("Radio URL resolves to a private or reserved address")
    return host, port, path, addresses


def _connect(url: str) -> tuple[socket.socket, str, int, str]:
    """Validate `url` (see validate_station_url) and connect to the first of
    its addresses that answers. (sock, host, port, path)."""
    try:
        host, port, path, addresses = validate_station_url(url, resolve_dns=True)
    except ValueError as exc:
        raise RadioUnavailable(str(exc)) from None
    deadline = time.monotonic() + CONNECT_TIMEOUT
    sock: socket.socket | None = None
    for address in addresses:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            sock = socket.create_connection(
                (address, port), timeout=min(remaining, ADDRESS_CONNECT_TIMEOUT)
            )
            break
        except OSError as exc:
            logger.info("Radio connect failed host=%s address=%s error=%s", host, address, type(exc).__name__)
    if sock is None:
        raise RadioUnavailable("Could not connect to the radio station")
    sock.settimeout(READ_TIMEOUT)
    if urlsplit(url).scheme == "https":
        try:
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
        except OSError:  # ssl.SSLError included
            sock.close()
            raise RadioUnavailable("Secure connection to the radio station failed") from None
    return sock, host, port, path


def _request(host: str, port: int, path: str) -> bytes:
    host_header = host if port in (80, 443) else f"{host}:{port}"
    if ":" in host and not host.startswith("["):
        host_header = f"[{host}]" if port in (80, 443) else f"[{host}]:{port}"
    return (
        f"GET {path} HTTP/1.0\r\n"
        f"Host: {host_header}\r\n"
        f"User-Agent: {settings.user_agent}\r\n"
        f"Icy-MetaData: 1\r\n"
        f"Connection: close\r\n\r\n"
    ).encode()


def _read_headers(sock: socket.socket) -> tuple[int, dict[str, str], bytes]:
    """(status, headers, start of the body).

    Real Icecast/SHOUTcast servers commonly answer HTTP/1.0 (or "ICY 200 OK")
    without a Content-Length, streaming indefinitely - something httpx's
    stricter parser refuses to handle at all. Raw sockets are the standard way ICY
    clients (VLC, mpv, ...) deal with this, so that's what we do too."""
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(1024)
        if not chunk:
            break
        buf += chunk
        if len(buf) > MAX_HEADER_BYTES:
            raise RadioUnavailable("Radio server sent oversized headers")
    header_part, _, body_start = buf.partition(b"\r\n\r\n")
    if not body_start and b"\r\n\r\n" not in buf:
        raise RadioUnavailable("Radio server closed before sending complete headers")
    lines = header_part.decode("latin-1", errors="replace").split("\r\n")
    status = lines[0].split()
    if len(status) < 2 or not status[1].isdigit():
        raise RadioUnavailable("Radio server sent an invalid response")
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if ":" in line:
            k, _, v = line.partition(":")
            headers[k.strip().lower()] = v.strip()
    return int(status[1]), headers, body_start


def _media_type(headers: dict[str, str]) -> str:
    return headers.get("content-type", "").split(";")[0].strip().lower()


def _is_audio_type(media_type: str) -> bool:
    return media_type.startswith("audio/") or media_type == "application/ogg"


def _is_playlist(url: str, headers: dict[str, str]) -> bool:
    media_type = _media_type(headers)
    if media_type in PLAYLIST_TYPES:
        return True
    return urlsplit(url).path.lower().endswith(PLAYLIST_EXTENSIONS) and not _is_audio_type(media_type)


def _read_playlist(sock: socket.socket, body: bytes) -> bytes:
    data = body
    while len(data) <= PLAYLIST_MAX_BYTES:
        chunk = sock.recv(CHUNK_SIZE)
        if not chunk:
            return data
        data += chunk
    raise RadioUnavailable("The station's playlist is too large")


def _playlist_target(base_url: str, data: bytes) -> str:
    """The first stream a .pls or .m3u playlist names, absolute."""
    text = data.decode("utf-8", errors="replace")
    if "#EXT-X-" in text:
        # An HLS media playlist: segments, not a stream to relay.
        raise RadioUnavailable("This station is an HLS stream, which radio playback does not support")
    for line in text.splitlines():
        line = line.strip()
        pls = re.match(r"(?i)file\d+\s*=\s*(\S+)", line)
        if pls:
            return urljoin(base_url, pls.group(1))
        if line and not line.startswith(("#", "[")) and "=" not in line:
            return urljoin(base_url, line)
    raise RadioUnavailable("The station's playlist names no stream")


@dataclass
class Upstream:
    """An answered station: the socket positioned after the headers."""

    sock: socket.socket
    headers: dict[str, str]
    body: bytes

    @property
    def media_type(self) -> str:
        """The upstream's audio type, passed on so an AAC or Ogg station is
        not labelled MP3; anything that is not audio reads as MP3."""
        media_type = _media_type(self.headers)
        return media_type if _is_audio_type(media_type) else "audio/mpeg"

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


def open_stream(url: str) -> Upstream:
    """Connect to the station and read its response headers, following up to
    MAX_REDIRECTS redirects and resolving a .pls/.m3u playlist to the stream
    it names - every hop validated and pinned like the first. Raises
    RadioUnavailable before any audio; the socket is closed on every failure.

    Playlists are resolved here, at stream time, rather than when a station
    is saved: they are often load-balancers whose target changes, and
    stations are saved without contacting the host."""
    for _hop in range(MAX_REDIRECTS + 1):
        sock, host, port, path = _connect(url)
        keep = False
        try:
            sock.sendall(_request(host, port, path))
            status, headers, body = _read_headers(sock)
            if status in REDIRECT_STATUSES:
                location = headers.get("location")
                if not location:
                    raise RadioUnavailable("Radio server sent a redirect without a location")
                url = urljoin(url, location)
            elif status in (404, 410):
                raise RadioUnavailable("The radio station's stream was not found", 404)
            elif not 200 <= status < 300:
                raise RadioUnavailable(f"Radio server answered HTTP {status}")
            elif _is_playlist(url, headers):
                url = _playlist_target(url, _read_playlist(sock, body))
            else:
                keep = True
                return Upstream(sock, headers, body)
        except OSError:  # timeouts and resets while talking to the server
            raise RadioUnavailable("The radio station did not answer") from None
        finally:
            if not keep:
                sock.close()
    raise RadioUnavailable("Radio server redirected too many times")


def _parse_stream_title(metadata: bytes) -> str | None:
    raw = metadata.strip(b"\x00")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        # Older SHOUTcast servers send titles in Latin-1; decoding those as
        # UTF-8 would turn every accented letter into a replacement character.
        text = raw.decode("latin-1")
    marker = "StreamTitle='"
    start = text.find(marker)
    if start == -1:
        return None
    start += len(marker)
    end = text.find("';", start)
    title = text[start : end if end != -1 else None]
    return title or None


def relay_icy(station_id: int, upstream: Upstream) -> Generator[bytes, None, None]:
    """Yield clean audio bytes from an answered station (metadata blocks
    stripped out so a plain <audio> element never sees them), updating the
    now-playing cache as titles change. If the station doesn't support ICY
    metadata at all (no icy-metaint header), the raw stream passes through
    untouched. Closes the upstream when done; a connection that drops
    mid-stream just ends the stream (the headers are long gone)."""
    sock, headers, body = upstream.sock, upstream.headers, upstream.body
    try:
        try:
            metaint = int(headers["icy-metaint"]) if "icy-metaint" in headers else None
        except ValueError:
            metaint = None
        if metaint is not None and metaint <= 0:
            metaint = None

        if metaint is None:
            # No metadata support - just relay bytes untouched.
            if body:
                yield body
            while True:
                chunk = sock.recv(CHUNK_SIZE)
                if not chunk:
                    return
                yield chunk

        buf = body
        audio_remaining = metaint
        while True:
            while len(buf) < 1:
                chunk = sock.recv(CHUNK_SIZE)
                if not chunk:
                    return
                buf += chunk

            if audio_remaining > 0:
                take = min(audio_remaining, len(buf))
                yield buf[:take]
                buf = buf[take:]
                audio_remaining -= take
                continue

            # audio_remaining == 0: the outer loop above already guaranteed
            # that the next byte is available, so it is the metadata length
            # byte.
            meta_len = buf[0] * 16
            buf = buf[1:]
            while len(buf) < meta_len:
                chunk = sock.recv(CHUNK_SIZE)
                if not chunk:
                    return
                buf += chunk
            metadata, buf = buf[:meta_len], buf[meta_len:]
            if meta_len > 0:
                title = _parse_stream_title(metadata)
                if title:
                    _now_playing[station_id] = {
                        "title": title,
                        "updated_at": time.time(),
                    }
            audio_remaining = metaint
    except OSError as exc:
        logger.info("Radio stream ended station=%s error=%s", station_id, type(exc).__name__)
    finally:
        upstream.close()


def stream_audio_with_icy(station_id: int, url: str) -> Iterator[bytes]:
    """open_stream + relay_icy in one generator (connects on first use)."""
    yield from relay_icy(station_id, open_stream(url))


async def iterate_then_cleanup(
    chunks: Iterator[bytes], cleanup: Callable[[], None]
) -> AsyncIterator[bytes]:
    """Feed a blocking chunk generator to a StreamingResponse, and run
    `cleanup` however the response ends - including the client going away,
    when Starlette abandons the iterator and the generator (with its open
    upstream socket and its stream slot) would otherwise wait for garbage
    collection. Each chunk is read in a worker thread; cancellation waits
    for the read in progress, so `cleanup` never races it."""
    try:
        while True:
            chunk = await anyio.to_thread.run_sync(next, chunks, None)
            if chunk is None:
                return
            yield chunk
    finally:
        cleanup()
