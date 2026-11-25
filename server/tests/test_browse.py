from __future__ import annotations

import wave

import pytest
from sqlalchemy.exc import IntegrityError

from app.models import IgnoredLibraryFolder, LibraryRoot, User
from app.routers.library.folders import ignore_library_folder
from app.scanner import scan_library_root
from app.schemas import IgnoredFolderIn
from app.security import hash_password
from tests.conftest import login_as, make_audio_fixture


def _make_untagged_wav(path):
    """A WAV with no ID3 tags at all - unlike `make_audio_fixture`, which
    always writes a title. Simulates a real "no metadata" file, so scanning
    falls back to the filename as the title."""
    with wave.open(str(path), "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\x00\x00" * 800)


def _scan_nested_library(db_session, library_dir):
    (library_dir / "Rock").mkdir()
    (library_dir / "Rock" / "Album One").mkdir()
    make_audio_fixture(
        library_dir / "Rock" / "Album One" / "01.wav",
        title="Track One",
        artist="Band",
        album="Album One",
        genre="Rock",
    )
    _make_untagged_wav(library_dir / "untagged.wav")
    (library_dir / "random.txt").write_text("not audio")
    root = LibraryRoot(path=str(library_dir), display_name="Test Library")
    db_session.add(root)
    db_session.commit()
    db_session.refresh(root)
    scan_library_root(db_session, root)
    return root


def test_list_roots_returns_accessible_roots(client, db_session, library_dir):
    root = _scan_nested_library(db_session, library_dir)
    resp = client.get("/api/library/roots")
    assert resp.status_code == 200
    assert [r["id"] for r in resp.json()] == [root.id]
    assert "path" not in resp.json()[0]


def test_browse_root_lists_top_level_dirs_and_files(client, db_session, library_dir):
    root = _scan_nested_library(db_session, library_dir)
    resp = client.get("/api/library/browse", params={"root_id": root.id})
    assert resp.status_code == 200
    body = resp.json()
    assert body["path"] == ""
    assert body["parent_path"] is None
    assert [d["name"] for d in body["directories"]] == ["Rock"]
    titles = {i["title"] for i in body["items"]}
    assert "untagged" in titles  # falls back to the filename stem when untagged
    assert not any(i["title"] == "random.txt" for i in body["items"])


def test_browse_subdirectory_shows_nested_track(client, db_session, library_dir):
    root = _scan_nested_library(db_session, library_dir)
    resp = client.get(
        "/api/library/browse", params={"root_id": root.id, "path": "Rock/Album One"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["directories"] == []
    assert [i["title"] for i in body["items"]] == ["Track One"]
    assert body["parent_path"] == "Rock"


def test_file_search_finds_nested_folders_and_filenames(client, db_session, library_dir):
    root = _scan_nested_library(db_session, library_dir)

    folder_response = client.get(
        "/api/library/files/search", params={"root_id": root.id, "q": "album one"}
    )
    assert folder_response.status_code == 200
    folder_body = folder_response.json()
    assert [(folder["name"], folder["path"]) for folder in folder_body["folders"]] == [
        ("Album One", "Rock/Album One")
    ]
    assert folder_body["items"] == []

    file_response = client.get(
        "/api/library/files/search", params={"root_id": root.id, "q": "01.wav"}
    )
    assert file_response.status_code == 200
    file_body = file_response.json()
    assert [item["relative_path"] for item in file_body["items"]] == [
        "Rock/Album One/01.wav"
    ]
    assert file_body["items"][0]["title"] == "Track One"


def test_file_search_escapes_like_wildcards_and_checks_root_access(
    session_factory, client, db_session, library_dir
):
    root = _scan_nested_library(db_session, library_dir)
    assert client.get(
        "/api/library/files/search", params={"root_id": root.id, "q": "%"}
    ).json()["items"] == []

    other = User(username="search-other", password_hash=hash_password("pw"))
    db_session.add(other)
    db_session.commit()
    other_client = login_as(session_factory, client, "search-other", "pw")
    db_session.query(LibraryRoot).filter(LibraryRoot.id == root.id).update(
        {"is_shared": False}
    )
    db_session.commit()
    denied = other_client.get(
        "/api/library/files/search", params={"root_id": root.id, "q": "Track"}
    )
    assert denied.status_code == 404


def test_file_search_handles_blank_query_and_unavailable_root(client, db_session, library_dir):
    root = _scan_nested_library(db_session, library_dir)
    blank = client.get(
        "/api/library/files/search", params={"root_id": root.id, "q": "   "}
    )
    assert blank.status_code == 200
    assert blank.json() == {"root_id": root.id, "folders": [], "items": [], "truncated": False}

    unavailable = LibraryRoot(
        path=str(library_dir / "missing-root"), display_name="Unavailable"
    )
    db_session.add(unavailable)
    db_session.commit()
    missing = client.get(
        "/api/library/files/search",
        params={"root_id": unavailable.id, "q": "track"},
    )
    assert missing.status_code == 404


def test_file_search_skips_missing_directories_and_files_outside_the_root(
    client, db_session, library_dir
):
    root = _scan_nested_library(db_session, library_dir)
    escaped_file = library_dir / "escape.wav"
    make_audio_fixture(
        escaped_file,
        title="Escaped",
        artist="Band",
        album="Album",
        genre="Rock",
    )
    scan_library_root(db_session, root)

    # Simulate stale scan rows pointing at a directory and a symlink that now
    # resolves outside the root.
    nested_file = library_dir / "Rock" / "Album One" / "01.wav"
    nested_file.unlink()
    nested_file.mkdir()
    (library_dir / "untagged.wav").unlink()
    outside_file = library_dir.parent / "outside.wav"
    make_audio_fixture(
        outside_file,
        title="Outside",
        artist="Band",
        album="Outside",
        genre="Rock",
    )
    escaped_file.unlink()
    escaped_file.symlink_to(outside_file)

    directory_result = client.get(
        "/api/library/files/search", params={"root_id": root.id, "q": "01.wav"}
    )
    symlink_result = client.get(
        "/api/library/files/search", params={"root_id": root.id, "q": "escape"}
    )
    missing_result = client.get(
        "/api/library/files/search", params={"root_id": root.id, "q": "untagged"}
    )
    assert directory_result.status_code == 200
    assert directory_result.json()["items"] == []
    assert symlink_result.status_code == 200
    assert symlink_result.json()["items"] == []
    assert missing_result.status_code == 200
    assert missing_result.json()["items"] == []


def test_file_search_caps_broad_results(client, db_session, library_dir, monkeypatch):
    root = _scan_nested_library(db_session, library_dir)
    monkeypatch.setattr("app.routers.library.folders.FILE_SEARCH_RESULT_LIMIT", 1)

    response = client.get(
        "/api/library/files/search", params={"root_id": root.id, "q": ".wav"}
    )
    assert response.status_code == 200
    assert response.json()["truncated"] is True
    assert len(response.json()["items"]) == 1


def test_ignored_folder_is_recursive_but_remains_browsable_and_restorable(
    client, db_session, library_dir
):
    root = _scan_nested_library(db_session, library_dir)

    response = client.post(
        f"/api/library/roots/{root.id}/ignored-folders", json={"path": "Rock"}
    )
    assert response.status_code == 200
    assert response.json() == {"path": "Rock"}
    duplicate = client.post(
        f"/api/library/roots/{root.id}/ignored-folders", json={"path": "Rock"}
    )
    assert duplicate.status_code == 200
    assert duplicate.json() == {"path": "Rock"}

    root_listing = client.get("/api/library/browse", params={"root_id": root.id}).json()
    assert root_listing["directories"] == [
        {
            "name": "Rock",
            "path": "Rock",
            "is_ignored": True,
            "ignored_directly": True,
            "ignored_by": "Rock",
        }
    ]
    rock_listing = client.get(
        "/api/library/browse", params={"root_id": root.id, "path": "Rock"}
    ).json()
    assert rock_listing["directories"][0]["is_ignored"] is True
    assert rock_listing["directories"][0]["ignored_directly"] is False
    assert rock_listing["directories"][0]["ignored_by"] == "Rock"
    nested = client.get(
        "/api/library/browse", params={"root_id": root.id, "path": "Rock/Album One"}
    ).json()
    assert nested["items"][0]["title"] == "Track One"

    restored = client.request(
        "DELETE",
        f"/api/library/roots/{root.id}/ignored-folders",
        json={"path": "Rock"},
    )
    assert restored.status_code == 200
    restored_listing = client.get(
        "/api/library/browse", params={"root_id": root.id}
    ).json()
    assert restored_listing["directories"][0]["is_ignored"] is False
    assert db_session.query(IgnoredLibraryFolder).filter_by(library_root_id=root.id).count() == 0


def test_ignoring_folder_rejects_root_traversal_and_non_admins(
    session_factory, client, db_session, library_dir
):
    root = _scan_nested_library(db_session, library_dir)
    traversal = client.post(
        f"/api/library/roots/{root.id}/ignored-folders", json={"path": "../outside"}
    )
    assert traversal.status_code == 422

    other = User(username="ignore-other", password_hash=hash_password("pw"))
    db_session.add(other)
    db_session.commit()
    other_client = login_as(session_factory, client, "ignore-other", "pw")
    denied = other_client.post(
        f"/api/library/roots/{root.id}/ignored-folders", json={"path": "Rock"}
    )
    assert denied.status_code == 403


def test_ignored_folder_routes_reject_invalid_targets(client, db_session, library_dir):
    root = _scan_nested_library(db_session, library_dir)

    blank = client.post(
        f"/api/library/roots/{root.id}/ignored-folders", json={"path": "."}
    )
    missing_root = client.post(
        "/api/library/roots/99999/ignored-folders", json={"path": "Rock"}
    )
    missing_folder = client.post(
        f"/api/library/roots/{root.id}/ignored-folders", json={"path": "Not there"}
    )
    file_target = client.post(
        f"/api/library/roots/{root.id}/ignored-folders",
        json={"path": "Rock/Album One/01.wav"},
    )
    missing_delete_root = client.request(
        "DELETE", "/api/library/roots/99999/ignored-folders", json={"path": "Rock"}
    )

    assert blank.status_code == 422
    assert missing_root.status_code == 404
    assert missing_folder.status_code == 404
    assert file_target.status_code == 404
    assert missing_delete_root.status_code == 404


def test_ignore_folder_recovers_when_duplicate_is_inserted_concurrently(
    db_session, library_dir, monkeypatch
):
    root = _scan_nested_library(db_session, library_dir)
    user = db_session.query(User).filter_by(username="dork_soul").one()
    original_commit = db_session.commit

    def commit_then_raise_duplicate():
        # Persist the winner as if another request inserted it between our
        # existence check and commit, then simulate the uniqueness violation.
        original_commit()
        raise IntegrityError("insert ignored folder", {}, RuntimeError("duplicate"))

    monkeypatch.setattr(db_session, "commit", commit_then_raise_duplicate)
    result = ignore_library_folder(
        root.id, IgnoredFolderIn(path="Rock"), db_session, user
    )
    assert result.path == "Rock"
    assert db_session.query(IgnoredLibraryFolder).filter_by(relative_path="Rock").one()


def test_ignore_folder_reraises_unrecovered_commit_error(db_session, library_dir, monkeypatch):
    root = _scan_nested_library(db_session, library_dir)
    user = db_session.query(User).filter_by(username="dork_soul").one()

    def fail_commit():
        raise IntegrityError("insert ignored folder", {}, RuntimeError("database error"))

    monkeypatch.setattr(db_session, "commit", fail_commit)
    with pytest.raises(IntegrityError):
        ignore_library_folder(root.id, IgnoredFolderIn(path="Rock"), db_session, user)
    assert db_session.query(IgnoredLibraryFolder).filter_by(relative_path="Rock").one_or_none() is None


def test_browse_rejects_path_traversal(client, db_session, library_dir):
    root = _scan_nested_library(db_session, library_dir)
    resp = client.get(
        "/api/library/browse", params={"root_id": root.id, "path": "../../../etc"}
    )
    assert resp.status_code == 404


def test_browse_404_for_unscanned_root(client):
    resp = client.get("/api/library/browse", params={"root_id": 9999})
    assert resp.status_code == 404


def test_browse_404_for_inaccessible_root(session_factory, client, db_session, library_dir):
    root = _scan_nested_library(db_session, library_dir)
    other = User(username="browse-other", password_hash=hash_password("pw"))
    db_session.add(other)
    db_session.commit()
    other_client = login_as(session_factory, client, "browse-other", "pw")

    from app.models import LibraryRoot as LR

    db_session.query(LR).filter(LR.id == root.id).update({"is_shared": False})
    db_session.commit()

    resp = other_client.get("/api/library/browse", params={"root_id": root.id})
    assert resp.status_code == 404
