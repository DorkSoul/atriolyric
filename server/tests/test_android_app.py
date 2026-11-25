"""The Android app's APK: offered through /api/version and sent by
/api/download/app/android only when the server carries one."""

from __future__ import annotations

import json

from app import android_app
from app.config import settings


def _apk(tmp_path, version: str | None = "0.19.0", content: bytes = b"PK\x03\x04 not really an apk"):
    path = tmp_path / "atriolyric.apk"
    path.write_bytes(content)
    if version is not None:
        (tmp_path / "atriolyric.apk.json").write_text(json.dumps({"version": version}))
    return path


def test_no_path_or_no_file_means_no_app(tmp_path):
    assert android_app.android_app_info(None, "1.0.0") is None
    assert android_app.android_app_info(tmp_path / "missing.apk", "1.0.0") is None
    empty = tmp_path / "empty.apk"
    empty.write_bytes(b"")
    assert android_app.android_app_info(empty, "1.0.0") is None
    assert android_app.android_app_info(tmp_path, "1.0.0") is None  # a folder


def test_the_version_comes_from_the_file_beside_the_apk(tmp_path):
    path = _apk(tmp_path, "0.19.0", b"x" * 10)
    assert android_app.android_app_info(path, "0.20.0") == {"version": "0.19.0", "size": 10}


def test_without_that_file_the_web_version_stands_in(tmp_path):
    path = _apk(tmp_path, None)
    assert android_app.android_app_info(path, "0.20.0")["version"] == "0.20.0"
    assert android_app.android_app_info(path, None) is None
    (tmp_path / "atriolyric.apk.json").write_text("not json")
    assert android_app.android_app_info(path, "0.20.0")["version"] == "0.20.0"
    (tmp_path / "atriolyric.apk.json").write_text(json.dumps({"version": ""}))
    assert android_app.android_app_info(path, "0.20.0")["version"] == "0.20.0"


def test_version_reports_the_app_only_when_there_is_one(client, tmp_path, monkeypatch):
    assert client.get("/api/version").json()["android_app"] is None
    monkeypatch.setattr(settings, "android_apk_path", _apk(tmp_path, "0.19.0", b"y" * 7))
    assert client.get("/api/version").json()["android_app"] == {"version": "0.19.0", "size": 7}


def test_the_apk_is_sent_to_a_signed_in_user(client, raw_client, tmp_path, monkeypatch):
    assert client.get("/api/download/app/android").status_code == 404
    monkeypatch.setattr(settings, "android_apk_path", _apk(tmp_path, "0.19.0", b"apk-bytes"))
    response = client.get("/api/download/app/android")
    assert response.status_code == 200
    assert response.content == b"apk-bytes"
    assert response.headers["content-type"] == "application/vnd.android.package-archive"
    assert 'filename="atriolyric-0.19.0.apk"' in response.headers["content-disposition"]


def test_the_apk_needs_a_login(raw_client, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "android_apk_path", _apk(tmp_path))
    assert raw_client.get("/api/download/app/android").status_code == 401
