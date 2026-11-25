from __future__ import annotations

import itertools

import pytest

from app.radio import _parse_stream_title, stream_audio_with_icy


def test_parse_stream_title_extracts_artist_and_title():
    raw = b"StreamTitle='Kalabi - Inside The Sun';StreamUrl='https://example.com/art.jpg';"
    assert _parse_stream_title(raw) == "Kalabi - Inside The Sun"


def test_parse_stream_title_handles_padding_nulls():
    raw = b"StreamTitle='Artist - Track';" + b"\x00" * 20
    assert _parse_stream_title(raw) == "Artist - Track"


def test_parse_stream_title_falls_back_to_latin1():
    raw = "StreamTitle='Björk - Jóga';".encode("latin-1")
    assert _parse_stream_title(raw) == "Björk - Jóga"
    assert _parse_stream_title("StreamTitle='Sigur Rós';".encode()) == "Sigur Rós"


def test_stream_slots_cap_per_user_and_in_total_and_always_release():
    from contextlib import ExitStack

    from app import radio

    with radio.stream_slot(1), radio.stream_slot(1):
        with pytest.raises(radio.RadioBusy), radio.stream_slot(1):
            pass
        with radio.stream_slot(2):
            assert radio._streams_by_user == {1: 2, 2: 1}
    with pytest.raises(RuntimeError), radio.stream_slot(3):
        raise RuntimeError("stream broke")
    assert radio._streams_by_user == {}
    with ExitStack() as stack:
        for user in range(radio.MAX_STREAMS_TOTAL):
            stack.enter_context(radio.stream_slot(user))
        with pytest.raises(radio.RadioBusy), radio.stream_slot(99):
            pass
    assert radio._streams_by_user == {}


def test_parse_stream_title_none_when_no_marker():
    assert _parse_stream_title(b"\x00" * 16) is None


def test_parse_stream_title_none_for_empty_title():
    assert _parse_stream_title(b"StreamTitle='';StreamUrl='';") is None


@pytest.mark.network
def test_stream_audio_with_icy_against_real_station_strips_metadata_and_finds_title():
    # A real, public Icecast stream (SomaFM) that answers HTTP/1.0 without
    # a Content-Length, which is exactly the case httpx's stricter parser
    # refuses to stream at all (raw sockets are used here instead
    # specifically because of that).
    gen = stream_audio_with_icy(123456, "http://ice1.somafm.com/groovesalad-128-mp3")
    total_bytes = 0
    for chunk in itertools.islice(gen, 40):  # ~160KB, comfortably past one metaint boundary
        assert isinstance(chunk, bytes)
        total_bytes += len(chunk)
    gen.close()
    assert total_bytes > 0

    from app.radio import get_now_playing

    now_playing = get_now_playing(123456)
    assert now_playing is not None
    assert now_playing["title"]  # some non-empty "Artist - Track" string


def test_request_host_header_brackets_ipv6_and_names_odd_ports():
    from app import radio

    assert b"Host: [2606::1]\r\n" in radio._request("2606::1", 80, "/")
    assert b"Host: [2606::1]:8000\r\n" in radio._request("2606::1", 8000, "/")
    assert b"Host: radio.example:8000\r\n" in radio._request("radio.example", 8000, "/")


def test_tls_failure_and_empty_dns_answers_are_radio_unavailable(monkeypatch):
    import ssl

    from app import radio

    monkeypatch.setattr("app.radio.socket.getaddrinfo", lambda *a, **k: [])
    with pytest.raises(radio.RadioUnavailable, match="resolved"):
        radio._connect("http://radio.example/")

    class Sock:
        closed = False

        def settimeout(self, _t):
            pass

        def close(self):
            self.closed = True

    sock = Sock()

    class Context:
        def wrap_socket(self, _sock, server_hostname):
            raise ssl.SSLError("bad certificate")

    monkeypatch.setattr(
        "app.radio.socket.getaddrinfo",
        lambda host, port, type=None: [(None, None, None, None, ("93.184.216.34", port))],
    )
    monkeypatch.setattr("app.radio.socket.create_connection", lambda *a, **k: sock)
    monkeypatch.setattr("app.radio.ssl.create_default_context", Context)
    with pytest.raises(radio.RadioUnavailable, match="Secure connection"):
        radio._connect("https://radio.example/")
    assert sock.closed


def test_connect_gives_up_when_the_time_budget_is_spent(monkeypatch):
    from app import radio

    monkeypatch.setattr(
        "app.radio.socket.getaddrinfo",
        lambda host, port, type=None: [(None, None, None, None, ("93.184.216.34", port))],
    )
    monkeypatch.setattr(radio, "CONNECT_TIMEOUT", 0)
    with pytest.raises(radio.RadioUnavailable, match="Could not connect"):
        radio._connect("http://radio.example/")
