"""One version source: the FastAPI app version and the
outbound User-Agent come from the built frontend's version.json, with a
fallback when there is none (development, the tests)."""

from __future__ import annotations

import json

from app import main, version
from app.config import Settings


def _dist(tmp_path, content: str | None):
    if content is not None:
        (tmp_path / "version.json").write_text(content)
    return tmp_path


def test_the_built_frontend_names_the_version(tmp_path):
    dist = _dist(tmp_path, json.dumps({"version": "0.6.1"}))
    assert version.app_version(dist) == "0.6.1"
    assert version.default_user_agent(dist) == (
        "atriolyric/0.6.1 (+https://github.com/DorkSoul/atriolyric)"
    )
    settings = Settings(_env_file=None, web_dist_path=dist)
    assert settings.user_agent.startswith("atriolyric/0.6.1 ")


def test_without_a_build_the_fallback_is_used(tmp_path, monkeypatch):
    monkeypatch.delenv("ATRIOLYRIC_USER_AGENT", raising=False)
    for dist in (None, _dist(tmp_path, None), tmp_path / "missing"):
        assert version.app_version(dist) == version.FALLBACK_VERSION
    for broken in ("not json", "[]", '{"version": ""}', '{"version": 5}'):
        assert version.app_version(_dist(tmp_path, broken)) == version.FALLBACK_VERSION
    assert Settings(_env_file=None).user_agent == (
        f"atriolyric/{version.FALLBACK_VERSION} (+https://github.com/DorkSoul/atriolyric)"
    )
    # The tests serve no built frontend, so the app reports the fallback.
    assert main.app.version == version.FALLBACK_VERSION
    assert main.app.openapi()["info"]["version"] == version.FALLBACK_VERSION


def test_an_explicit_user_agent_still_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("ATRIOLYRIC_USER_AGENT", "my-mirror/1.0")
    dist = _dist(tmp_path, json.dumps({"version": "0.6.1"}))
    assert Settings(_env_file=None, web_dist_path=dist).user_agent == "my-mirror/1.0"


def test_the_openapi_document_exports_without_a_server_or_database(tmp_path):
    """`python -m app.openapi_export` (the web build's type generation)."""
    import os
    import subprocess
    import sys
    from pathlib import Path

    server = Path(__file__).resolve().parents[1]
    env = {k: v for k, v in os.environ.items() if not k.startswith("ATRIOLYRIC_")}
    env["ATRIOLYRIC_DATABASE_PATH"] = str(tmp_path / "real" / "atriolyric.db")
    env["PYTHONPATH"] = str(server)
    result = subprocess.run(
        [sys.executable, "-m", "app.openapi_export"],
        cwd=tmp_path, env=env, capture_output=True, text=True, check=True, timeout=120,
    )
    document = json.loads(result.stdout)
    assert document["info"]["version"] == version.FALLBACK_VERSION
    info = document["paths"]["/api/library/artist-info"]["get"]["responses"]["200"]
    assert info["content"]["application/json"]["schema"] == {"$ref": "#/components/schemas/ArtistInfoOut"}
    assert "WantEntryOut" in document["components"]["schemas"]
    # Neither the configured database's directory nor ./data was created.
    assert not (tmp_path / "real").exists() and not (tmp_path / "data").exists()
