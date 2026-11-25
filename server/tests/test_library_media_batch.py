from __future__ import annotations

from app.models import LibraryRoot, MediaItem, MediaKind
from app.scanner import scan_library_root
from tests.conftest import make_audio_fixture


def test_get_media_by_ids_preserves_order_and_filters_inaccessible(
    client, db_session, library_dir
):
    make_audio_fixture(library_dir / "a.wav", title="Alpha", artist="A", album="X", genre="Rock")
    make_audio_fixture(library_dir / "b.wav", title="Beta", artist="B", album="Y", genre="Jazz")
    root = LibraryRoot(path=str(library_dir), display_name="Lib")
    db_session.add(root)
    db_session.commit()
    db_session.refresh(root)
    scan_library_root(db_session, root)

    tracks = client.get("/api/library/tracks").json()
    alpha = next(t for t in tracks if t["title"] == "Alpha")
    beta = next(t for t in tracks if t["title"] == "Beta")

    resp = client.get(f"/api/library/media?ids={beta['id']},{alpha['id']},999999")
    assert resp.status_code == 200
    titles = [t["title"] for t in resp.json()]
    assert titles == ["Beta", "Alpha"]  # requested order, unknown id silently dropped


def test_get_media_by_ids_has_headroom_for_a_full_queue_window_but_not_a_whole_library(
    client, db_session
):
    # The playback queue is windowed (app.playback.WINDOW_TARGET), so
    # the client only ever needs to resolve a few hundred ids at once
    # regardless of library size - this endpoint's cap has comfortable
    # headroom over that window, but not for an entire large library's
    # worth of ids in one request.
    root = LibraryRoot(path="/fake", display_name="Fake")
    db_session.add(root)
    db_session.commit()
    ids = []
    for i in range(2500):
        item = MediaItem(
            library_root_id=root.id, kind=MediaKind.track, relative_path=f"t{i}.mp3", title=f"T{i}"
        )
        db_session.add(item)
        ids.append(item)
    db_session.commit()
    id_list = [i.id for i in ids]

    # Comfortably more than one queue window (200) resolves fine...
    resp = client.get(f"/api/library/media?ids={','.join(str(i) for i in id_list[:500])}")
    assert resp.status_code == 200
    assert len(resp.json()) == 500

    # ...but the whole 2500-id library-scale request gets silently truncated
    # to the cap.
    resp = client.get(f"/api/library/media?ids={','.join(str(i) for i in id_list)}")
    assert resp.status_code == 200
    assert len(resp.json()) == 2000
