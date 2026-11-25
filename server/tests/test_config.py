"""Settings defaults and startup validation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import Settings


def test_safe_defaults(monkeypatch):
    # The suite's conftest turns Secure cookies off; the real default is auto.
    monkeypatch.delenv("ATRIOLYRIC_COOKIE_SECURE")
    fresh = Settings(_env_file=None)
    assert fresh.cookie_secure == "auto"
    assert fresh.allowed_origins == ()


def test_allowed_origins_is_a_json_list(monkeypatch):
    monkeypatch.setenv("ATRIOLYRIC_ALLOWED_ORIGINS", '["https://music.example.com"]')
    assert Settings(_env_file=None).allowed_origins == ("https://music.example.com",)


@pytest.mark.parametrize("raw", ["debug", " Warning ", "ERROR", "info"])
def test_log_level_is_case_insensitive(monkeypatch, raw):
    monkeypatch.setenv("ATRIOLYRIC_LOG_LEVEL", raw)
    assert Settings(_env_file=None).log_level == raw.strip().upper()


def test_a_log_level_typo_fails_at_startup(monkeypatch):
    monkeypatch.setenv("ATRIOLYRIC_LOG_LEVEL", "VERBOSE")
    with pytest.raises(ValidationError, match="log_level"):
        Settings(_env_file=None)


def test_hwaccel_values(monkeypatch):
    monkeypatch.setenv("ATRIOLYRIC_HWACCEL", "VAAPI")
    assert Settings(_env_file=None).hwaccel == "vaapi"


def test_a_hwaccel_typo_fails_at_startup(monkeypatch):
    monkeypatch.setenv("ATRIOLYRIC_HWACCEL", "vaapii")
    with pytest.raises(ValidationError, match="hwaccel"):
        Settings(_env_file=None)


def test_rendition_cache_setting_and_folder(monkeypatch):
    assert Settings(_env_file=None).rendition_cache_max_bytes == 20 * 1024**3
    monkeypatch.setenv("ATRIOLYRIC_RENDITION_CACHE_MAX_BYTES", "12345")
    configured = Settings(_env_file=None)
    assert configured.rendition_cache_max_bytes == 12345
    assert configured.rendition_dir == configured.database_path.parent / "renditions"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("auto", "auto"), (" AUTO ", "auto"), ("true", "true"), ("1", "true"),
     ("yes", "true"), ("false", "false"), ("0", "false"), ("off", "false")],
)
def test_cookie_secure_words(monkeypatch, raw, expected):
    monkeypatch.setenv("ATRIOLYRIC_COOKIE_SECURE", raw)
    assert Settings(_env_file=None).cookie_secure == expected


def test_a_cookie_secure_typo_fails_at_startup(monkeypatch):
    monkeypatch.setenv("ATRIOLYRIC_COOKIE_SECURE", "sometimes")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


@pytest.mark.parametrize(
    ("mode", "scheme", "proto", "expected"),
    [
        ("auto", "http", None, False),
        ("auto", "https", None, True),
        ("auto", "http", "https", True),
        ("auto", "http", "HTTPS, http", True),
        ("auto", "http", "http", False),
        ("true", "http", None, True),
        ("false", "https", "https", False),
    ],
)
def test_is_https(monkeypatch, mode, scheme, proto, expected):
    from app.config import is_https, settings

    monkeypatch.setattr(settings, "cookie_secure", mode)
    assert is_https(scheme, proto) is expected
