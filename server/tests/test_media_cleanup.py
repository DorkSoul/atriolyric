"""Deleting media: derived files go only once the delete commits, and the
cache folders are listed once per batch."""

from __future__ import annotations

import os
from unittest.mock import patch

from conftest import make_audio_fixture
from sqlalchemy import text

from app.cleanup import delete_media_cache, delete_media_records
from app.config import settings
from app.custom_art import purge_custom_art
from app.models import CustomArt, LibraryRoot, MediaItem
from app.scanner import scan_library_root


def _items(db_session, count: int) -> list[int]:
    root = LibraryRoot(path="/cleanup", display_name="Cleanup")
    db_session.add(root)
    db_session.commit()
    items = [
        MediaItem(library_root_id=root.id, relative_path=f"{n}.mp3", title=str(n))
        for n in range(count)
    ]
    db_session.add_all(items)
    db_session.commit()
    return [item.id for item in items]


def _cache_files(media_item_id: int) -> list:
    paths = [
        settings.thumbnails_dir / f"{media_item_id}.jpg",
        settings.art_thumb_dir / f"{media_item_id}.jpg",
        settings.art_dir / f"{media_item_id}.png",
        settings.transcode_dir / f"{media_item_id}_128k.mp3",
    ]
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
    return paths


def test_cache_files_go_only_after_the_delete_commits(db_session, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_path", tmp_path / "data" / "app.db")
    gone, kept = _items(db_session, 2)
    gone_files, kept_files = _cache_files(gone), _cache_files(kept)
    # Names that only look alike are not this row's files.
    lookalikes = [settings.art_dir / f"{gone}0.png", settings.transcode_dir / f"0{gone}_x.mp3"]
    for path in lookalikes:
        path.write_bytes(b"x")

    delete_media_records(db_session, [gone])
    assert all(path.exists() for path in gone_files)
    db_session.commit()

    assert not any(path.exists() for path in gone_files)
    assert all(path.exists() for path in kept_files + lookalikes)


def test_a_rolled_back_delete_keeps_the_rows_files(db_session, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_path", tmp_path / "data" / "app.db")
    (item,) = _items(db_session, 1)
    files = _cache_files(item)

    delete_media_records(db_session, [item])
    db_session.rollback()
    db_session.commit()  # a later, unrelated commit must not run the old removal

    assert db_session.get(MediaItem, item) is not None
    assert all(path.exists() for path in files)


def test_a_savepoint_inside_the_delete_does_not_remove_files_early(
    db_session, tmp_path, monkeypatch
):
    monkeypatch.setattr(settings, "database_path", tmp_path / "data" / "app.db")
    (item,) = _items(db_session, 1)
    files = _cache_files(item)

    delete_media_records(db_session, [item])
    with db_session.begin_nested():
        db_session.execute(text("SELECT 1"))
    assert all(path.exists() for path in files)
    try:
        with db_session.begin_nested():
            raise ValueError
    except ValueError:
        pass
    db_session.commit()
    assert not any(path.exists() for path in files)


def test_a_batch_delete_lists_each_cache_folder_once(db_session, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_path", tmp_path / "data" / "app.db")
    ids = _items(db_session, 30)
    files = [path for media_item_id in ids for path in _cache_files(media_item_id)]
    real_listdir = os.listdir
    with patch("app.cleanup.os.listdir", side_effect=real_listdir) as listdir:
        delete_media_records(db_session, ids)
        db_session.commit()
    assert listdir.call_count == 2  # art and transcodes
    assert not any(path.exists() for path in files)


def test_delete_media_cache_takes_one_id_and_tolerates_missing_folders(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_path", tmp_path / "data" / "app.db")
    delete_media_cache(7)  # no cache folders at all yet
    files = _cache_files(7)
    delete_media_cache(7)
    assert not any(path.exists() for path in files)


def test_purged_custom_art_files_go_only_after_the_commit(db_session, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_path", tmp_path / "data" / "app.db")
    settings.custom_art_dir.mkdir(parents=True)
    names = ("playlist-a.png", "playlist-a.thumb.jpg")
    for name in names:
        (settings.custom_art_dir / name).write_bytes(b"x")
    db_session.add(
        CustomArt(entity_type="playlist", entity_key="9", path=names[0], thumb_path=names[1])
    )
    db_session.commit()

    purge_custom_art(db_session, "playlist", [9])
    db_session.rollback()
    assert db_session.query(CustomArt).count() == 1
    assert all((settings.custom_art_dir / name).exists() for name in names)

    purge_custom_art(db_session, "playlist", [9])
    assert all((settings.custom_art_dir / name).exists() for name in names)
    db_session.commit()
    assert db_session.query(CustomArt).count() == 0
    assert not any((settings.custom_art_dir / name).exists() for name in names)


def test_a_scan_lists_the_cache_folders_once_and_removes_missing_rows_in_batches(
    db_session, library_dir, tmp_path, monkeypatch
):
    monkeypatch.setattr(settings, "database_path", tmp_path / "data" / "app.db")
    for n in range(3):
        make_audio_fixture(library_dir / f"t{n}.wav", title=f"T{n}", artist="A", album="X", genre=None)
    root = LibraryRoot(path=str(library_dir), display_name="Lib")
    db_session.add(root)
    db_session.commit()
    real_listdir = os.listdir
    with patch("app.cleanup.os.listdir", side_effect=real_listdir) as listdir:
        scan_library_root(db_session, root, mode="full")
        scan_library_root(db_session, root, mode="full")
    # One listing of the two cache folders per scan, not per file.
    assert listdir.call_count == 4

    for n in range(3):
        (library_dir / f"t{n}.wav").unlink()
    make_audio_fixture(library_dir / "new.wav", title="New", artist="B", album="Y", genre=None)
    with patch("app.scanner.SCAN_COMMIT_BATCH_SIZE", 2), patch(
        "app.scanner.delete_media_records", wraps=delete_media_records
    ) as deleter:
        result = scan_library_root(db_session, root, mode="confirm_removals")
    assert result["removed"] == 3
    assert [len(call.args[1]) for call in deleter.call_args_list] == [2, 1]
    assert [item.title for item in db_session.query(MediaItem)] == ["New"]
