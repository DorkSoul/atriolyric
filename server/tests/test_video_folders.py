from __future__ import annotations

from app.models import LibraryRoot, MediaItem, MediaKind


def _make_video(db_session, root_id, relative_path, title, kind=MediaKind.video) -> MediaItem:
    item = MediaItem(
        library_root_id=root_id,
        kind=kind,
        relative_path=relative_path,
        title=title,
    )
    db_session.add(item)
    db_session.commit()
    db_session.refresh(item)
    return item


def _make_root(db_session, library_dir) -> LibraryRoot:
    root = LibraryRoot(path=str(library_dir), display_name="Video Library")
    db_session.add(root)
    db_session.commit()
    db_session.refresh(root)
    return root


def test_list_video_folders_top_level_counts_whole_subtree(client, db_session, library_dir):
    root = _make_root(db_session, library_dir)
    _make_video(db_session, root.id, "Show/S1/one.mkv", "One")
    _make_video(db_session, root.id, "Show/S2/two.mkv", "Two")
    _make_video(db_session, root.id, "Other/three.mkv", "Three")
    _make_video(db_session, root.id, "loose.mkv", "Loose")

    resp = client.get("/api/library/videos/folders")
    assert resp.status_code == 200
    by_folder = {f["path"]: f["video_count"] for f in resp.json()}
    assert by_folder == {"Show": 2, "Other": 1}


def test_list_video_folders_nested_parent(client, db_session, library_dir):
    root = _make_root(db_session, library_dir)
    _make_video(db_session, root.id, "Show/S1/one.mkv", "One")
    _make_video(db_session, root.id, "Show/S1/Extras/x.mkv", "X")
    _make_video(db_session, root.id, "Show/S2/two.mkv", "Two")

    resp = client.get("/api/library/videos/folders", params={"parent": "Show"})
    assert {(f["folder"], f["path"], f["video_count"]) for f in resp.json()} == {
        ("S1", "Show/S1", 2),
        ("S2", "Show/S2", 1),
    }
    resp = client.get("/api/library/videos/folders", params={"parent": "Show/S2"})
    assert resp.json() == []


def test_list_videos_dir_returns_only_direct_children(client, db_session, library_dir):
    root = _make_root(db_session, library_dir)
    _make_video(db_session, root.id, "Show/one.mkv", "One")
    _make_video(db_session, root.id, "Show/S1/two.mkv", "Two")
    _make_video(db_session, root.id, "Show 2/three.mkv", "Three")
    _make_video(db_session, root.id, "loose.mkv", "Loose")

    titles = lambda d: {v["title"] for v in client.get("/api/library/videos", params={"dir": d}).json()}
    assert titles("Show") == {"One"}
    assert titles("Show/S1") == {"Two"}
    assert titles("") == {"Loose"}


def test_video_folders_include_music_videos(client, db_session, library_dir):
    root = _make_root(db_session, library_dir)
    _make_video(db_session, root.id, "Band/mv.mkv", "MV", kind=MediaKind.music_video)

    resp = client.get("/api/library/videos/folders")
    assert resp.status_code == 200
    assert resp.json()[0]["path"] == "Band"


def test_videos_stay_out_of_music_recents(client, db_session, library_dir):
    root = _make_root(db_session, library_dir)
    _make_video(db_session, root.id, "Show/one.mkv", "Episode")
    _make_video(db_session, root.id, "Song.mp3", "Song", kind=MediaKind.track)

    titles = {i["title"] for i in client.get("/api/library/recently-added").json()}
    assert titles == {"Song"}
