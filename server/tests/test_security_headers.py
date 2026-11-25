from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient

from app import main, security_headers
from app.config import settings
from app.security_headers import API_CSP, APP_CSP

BASE_HEADERS = {
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "same-origin",
    "permissions-policy": "camera=(), microphone=(), geolocation=()",
}


def _assert_api_headers(resp):
    for name, value in BASE_HEADERS.items():
        assert resp.headers[name] == value
    assert resp.headers["content-security-policy"] == API_CSP


def test_headers_on_a_200(raw_client):
    resp = raw_client.get("/api/health")
    assert resp.status_code == 200
    _assert_api_headers(resp)


def test_headers_on_a_404(raw_client):
    resp = raw_client.get("/api/does-not-exist")
    assert resp.status_code == 404
    _assert_api_headers(resp)


def test_headers_on_the_origin_check_403(raw_client):
    resp = raw_client.post(
        "/api/auth/login",
        json={"username": "x", "password": "y"},
        headers={"origin": "https://evil.example"},
    )
    assert resp.status_code == 403
    _assert_api_headers(resp)


def test_headers_on_the_body_limit_413(raw_client):
    resp = raw_client.post(
        "/api/auth/login",
        content=b"{}",
        headers={"content-length": str(5 * 1024 * 1024), "content-type": "application/json"},
    )
    assert resp.status_code == 413
    _assert_api_headers(resp)


def test_headers_on_an_unhandled_500(raw_client):
    router = APIRouter()

    @router.get("/api/_boom")
    def boom() -> None:
        raise RuntimeError("boom")

    main.app.include_router(router)
    added = main.app.router.routes[-1]
    try:
        resp = TestClient(main.app, raise_server_exceptions=False).get("/api/_boom")
    finally:
        main.app.router.routes.remove(added)
    assert resp.status_code == 500
    _assert_api_headers(resp)


def test_an_unhandled_error_still_reaches_the_server(raw_client):
    """The middleware answers the 500 itself but re-raises, so uvicorn logs
    the traceback (and the test client surfaces it by default)."""
    router = APIRouter()

    @router.get("/api/_boom2")
    def boom() -> None:
        raise RuntimeError("boom")

    main.app.include_router(router)
    added = main.app.router.routes[-1]
    try:
        with pytest.raises(RuntimeError, match="boom"):
            TestClient(main.app, raise_server_exceptions=True).get("/api/_boom2")
    finally:
        main.app.router.routes.remove(added)


def test_the_app_shell_gets_the_app_policy(raw_client):
    resp = raw_client.get("/some-page")
    assert resp.status_code == 404
    assert resp.headers["content-security-policy"] == APP_CSP
    for name, value in BASE_HEADERS.items():
        assert resp.headers[name] == value


def test_the_app_policy_forbids_inline_scripts_and_framing():
    directives = {part.split()[0]: part.split()[1:] for part in APP_CSP.split("; ")}
    assert directives["script-src"] == ["'self'"]
    assert "'unsafe-inline'" not in directives["default-src"]
    assert directives["object-src"] == ["'none'"]
    assert directives["frame-ancestors"] == ["'none'"]
    assert directives["base-uri"] == ["'self'"]


def test_hsts_only_when_cookies_are_secure(raw_client, monkeypatch):
    assert "strict-transport-security" not in raw_client.get("/api/health").headers
    monkeypatch.setattr(settings, "cookie_secure", "true")
    assert (
        raw_client.get("/api/health").headers["strict-transport-security"]
        == "max-age=31536000"
    )


def test_hsts_in_auto_mode_follows_the_forwarded_scheme(raw_client, monkeypatch):
    monkeypatch.setattr(settings, "cookie_secure", "auto")
    assert "strict-transport-security" not in raw_client.get("/api/health").headers
    proxied = raw_client.get("/api/health", headers={"X-Forwarded-Proto": "https"})
    assert proxied.headers["strict-transport-security"] == "max-age=31536000"


def test_a_route_that_sets_its_own_header_keeps_it(raw_client):
    # setdefault semantics: only absent headers are added.
    seen: list[tuple[bytes, bytes]] = []

    async def inner(scope, receive, send):
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"x-frame-options", b"SAMEORIGIN")],
            }
        )
        await send({"type": "http.response.body", "body": b""})

    async def capture(message):
        if message["type"] == "http.response.start":
            seen.extend(message["headers"])

    import asyncio

    asyncio.run(
        security_headers.SecurityHeadersMiddleware(inner)(
            {"type": "http", "path": "/x"}, None, capture
        )
    )
    assert [v for k, v in seen if k == b"x-frame-options"] == [b"SAMEORIGIN"]


def test_websocket_scope_passes_through_untouched():
    import asyncio

    calls = []

    async def inner(scope, receive, send):
        calls.append(scope["type"])

    asyncio.run(security_headers.SecurityHeadersMiddleware(inner)({"type": "websocket"}, None, None))
    asyncio.run(
        security_headers.OriginCheckMiddleware(inner, lambda *a: None)({"type": "lifespan"}, None, None)
    )
    assert calls == ["websocket", "lifespan"]


def _run_app_script(script: str, tmp_path: Path, **env: str) -> str:
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        env={
            **{k: v for k, v in os.environ.items() if not k.startswith("ATRIOLYRIC_")},
            "ATRIOLYRIC_DATABASE_PATH": str(tmp_path / "x.db"),
            **env,
        },
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


_DOCS_SCRIPT = (
    "from fastapi.testclient import TestClient\n"
    "from app.main import app\n"
    "c = TestClient(app)\n"
    "print([c.get(p).status_code for p in ('/docs', '/redoc', '/openapi.json')])\n"
)


def test_api_docs_are_off_when_the_built_app_is_served(tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html></html>")
    out = _run_app_script(_DOCS_SCRIPT, tmp_path, ATRIOLYRIC_WEB_DIST_PATH=str(dist))
    assert out == "[404, 404, 404]"


def test_api_docs_stay_on_in_development(tmp_path):
    assert _run_app_script(_DOCS_SCRIPT, tmp_path) == "[200, 200, 200]"


def test_dev_api_docs_are_not_given_the_app_policy(raw_client):
    # Swagger UI needs a CDN script and inline code; development only.
    assert "content-security-policy" not in raw_client.get("/docs").headers


def test_origin_allowed_is_the_one_rule_for_writes_and_the_websocket(monkeypatch):
    monkeypatch.setattr(settings, "allowed_origins", ("https://music.example.com",))
    allowed = security_headers.origin_allowed
    assert allowed(None, "app.example") and allowed("", "app.example")
    assert allowed("https://app.example", "app.example")
    assert allowed("http://app.example", "app.example")
    assert allowed("https://music.example.com", "app.example")
    assert not allowed("https://evil.example", "app.example")
    assert not allowed("capacitor://app.example", "app.example")
    assert not allowed("null", "app.example")
