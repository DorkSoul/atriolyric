from __future__ import annotations

from unittest.mock import patch

from sqlalchemy import create_engine

from app import migrations
from app.default_radio_stations import DEFAULT_RADIO_STATIONS
from app.main import seed_default_radio_stations
from app.migrations import (
    convert_legacy_radio_seed_marker,
    ensure_radio_station_columns,
)
from app.models import RadioStation, SystemSetting, User
from tests.conftest import startup_against


def test_seeds_all_default_stations_into_empty_db(session_factory):
    with patch("app.main.SessionLocal", session_factory):
        seed_default_radio_stations()
    db = session_factory()
    try:
        stations = db.query(RadioStation).all()
        assert len(stations) == len(DEFAULT_RADIO_STATIONS)
        assert all(s.is_shared for s in stations)
        assert all(s.owner_user_id is None for s in stations)
        names = {s.name for s in stations}
        assert names == {name for name, _url, _desc, _provider in DEFAULT_RADIO_STATIONS}
        assert all(s.provider != "SomaFM" for s in stations)
        assert all(s.description and s.provider for s in stations)
    finally:
        db.close()


def test_seed_backfills_preexisting_row_instead_of_duplicating(session_factory):
    """A row seeded by an older version
    of this function (before description/provider existed) already has one
    of these exact stream_urls - it must be updated in place, not duplicated."""
    name, url, description, provider = DEFAULT_RADIO_STATIONS[0]
    db = session_factory()
    db.add(RadioStation(name=name, stream_url=url, is_shared=True))
    db.commit()
    db.close()

    with patch("app.main.SessionLocal", session_factory):
        seed_default_radio_stations()

    db = session_factory()
    try:
        matching = db.query(RadioStation).filter(RadioStation.stream_url == url).all()
        assert len(matching) == 1
        assert matching[0].description == description
        assert matching[0].provider == provider
        assert db.query(RadioStation).count() == len(DEFAULT_RADIO_STATIONS)
    finally:
        db.close()


def test_seed_leaves_unrelated_user_station_untouched(session_factory):
    db = session_factory()
    db.add(RadioStation(name="My station", stream_url="http://example.com/stream"))
    db.commit()
    db.close()

    with patch("app.main.SessionLocal", session_factory):
        seed_default_radio_stations()

    db = session_factory()
    try:
        mine = db.query(RadioStation).filter(RadioStation.stream_url == "http://example.com/stream").one()
        assert mine.provider is None
        assert mine.description is None
        assert db.query(RadioStation).count() == len(DEFAULT_RADIO_STATIONS) + 1
    finally:
        db.close()


def test_seed_never_resurrects_a_deleted_default_once_already_seeded(session_factory):
    with patch("app.main.SessionLocal", session_factory):
        seed_default_radio_stations()

    deleted_url = DEFAULT_RADIO_STATIONS[0][1]
    db = session_factory()
    db.query(RadioStation).filter(RadioStation.stream_url == deleted_url).delete()
    db.commit()
    db.close()

    with patch("app.main.SessionLocal", session_factory):
        seed_default_radio_stations()

    db = session_factory()
    try:
        assert db.query(RadioStation).filter(RadioStation.stream_url == deleted_url).first() is None
        assert db.query(RadioStation).count() == len(DEFAULT_RADIO_STATIONS) - 1
    finally:
        db.close()


def test_seed_never_resurrects_defaults_after_all_are_deleted(session_factory):
    with patch("app.main.SessionLocal", session_factory):
        seed_default_radio_stations()
    db = session_factory()
    db.query(RadioStation).delete()
    db.commit()
    db.close()

    with patch("app.main.SessionLocal", session_factory):
        seed_default_radio_stations()

    db = session_factory()
    try:
        assert db.query(RadioStation).count() == 0
    finally:
        db.close()


def test_legacy_batch_marker_becomes_per_station_markers(session_factory):
    """An install from before per-station markers has only the old batch
    marker. Its conversion keeps the old meaning: the defaults added after
    that marker shipped are seeded, while missing older defaults stay
    deleted.
    """
    old_name, old_url, _old_description, old_provider = DEFAULT_RADIO_STATIONS[0]
    db = session_factory()
    db.add(
        RadioStation(
            name=old_name,
            stream_url=old_url,
            provider=old_provider,
            is_shared=True,
        )
    )
    db.add(SystemSetting(key="default_radio_stations_seeded", value="1"))
    db.commit()
    db.close()

    with startup_against(session_factory):
        convert_legacy_radio_seed_marker()
        convert_legacy_radio_seed_marker()  # idempotent
        seed_default_radio_stations()

    added_later = migrations._RADIO_DEFAULTS_ADDED_AFTER_BATCH_MARKER
    db = session_factory()
    try:
        assert db.get(SystemSetting, "default_radio_stations_seeded") is None
        assert db.query(RadioStation).count() == 1 + len(added_later)
        assert {
            station.stream_url
            for station in db.query(RadioStation).all()
            if station.stream_url != old_url
        } == added_later
    finally:
        db.close()


def test_without_the_batch_marker_the_conversion_changes_nothing(session_factory):
    with startup_against(session_factory):
        convert_legacy_radio_seed_marker()
        seed_default_radio_stations()
    db = session_factory()
    try:
        assert db.query(RadioStation).count() == len(DEFAULT_RADIO_STATIONS)
    finally:
        db.close()


def test_ensure_radio_station_columns_adds_missing_columns(tmp_path):
    db_path = tmp_path / "old_schema.db"
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE radio_stations ("
            "id INTEGER PRIMARY KEY, name TEXT, stream_url TEXT, "
            "is_shared BOOLEAN, owner_user_id INTEGER, created_at DATETIME"
            ")"
        )
        conn.exec_driver_sql(
            "INSERT INTO radio_stations (id, name, stream_url, is_shared) "
            "VALUES (1, 'Old Station', 'http://example.com/old', 1)"
        )

    with patch("app.migrations.engine", engine):
        ensure_radio_station_columns()
        ensure_radio_station_columns()  # idempotent - must not error the second time

    with engine.connect() as conn:
        columns = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(radio_stations)")}
        assert {"description", "provider"} <= columns
        row = conn.exec_driver_sql(
            "SELECT name, stream_url, description, provider FROM radio_stations WHERE id=1"
        ).fetchone()
        assert row == ("Old Station", "http://example.com/old", None, None)


OLD_SOMA = "http://ice1.somafm.com/groovesalad-128-mp3"
NEW_SOMA = "https://ice6.somafm.com/groovesalad-128-mp3"


def _old_install_with_groove_salad(session_factory, **row) -> int:
    """A database with the Groove Salad default on its old plain-HTTP URL,
    with that URL's marker."""
    db = session_factory()
    station = RadioStation(
        name="SomaFM: Groove Salad", stream_url=OLD_SOMA, provider="SomaFM", is_shared=True, **row
    )
    db.add(station)
    db.add(SystemSetting(key=f"default_radio_station:{OLD_SOMA}", value="1"))
    db.commit()
    station_id = station.id
    db.close()
    return station_id


def test_no_somafm_station_is_a_default():
    # SomaFM's terms ask for prior permission to re-stream: none is bundled.
    assert all(provider != "SomaFM" for _name, _url, _desc, provider in DEFAULT_RADIO_STATIONS)
    assert not any("somafm" in url.lower() for _name, url, _desc, _provider in DEFAULT_RADIO_STATIONS)
    # The one-time HTTPS rewrite for existing installs has its own channel list.
    assert migrations.SOMAFM_HTTPS_MOVES
    assert all(url.startswith("https://ice6.somafm.com/") for url in migrations.SOMAFM_HTTPS_MOVES.values())


def test_fresh_install_seeds_no_somafm_station(session_factory):
    with startup_against(session_factory):
        migrations.move_somafm_defaults_to_https()
        seed_default_radio_stations()
    db = session_factory()
    try:
        urls = {s.stream_url for s in db.query(RadioStation).all()}
        assert len(urls) == len(DEFAULT_RADIO_STATIONS)
        assert not any("somafm" in url for url in urls)
        assert db.get(SystemSetting, f"default_radio_station:{NEW_SOMA}") is None
    finally:
        db.close()


def test_existing_somafm_rows_survive_the_seed_and_a_legacy_batch_marker(session_factory):
    """An install that still has SomaFM rows (on the new URL and on the
    old one) and the pre-per-station batch marker keeps
    every row: nothing is deleted, duplicated or re-seeded."""
    db = session_factory()
    db.add(RadioStation(name="SomaFM: Groove Salad", stream_url=NEW_SOMA, provider="SomaFM", is_shared=True))
    db.add(RadioStation(name="SomaFM: Lush", stream_url="http://ice1.somafm.com/lush-128-mp3",
                        provider="SomaFM", is_shared=True))
    db.add(SystemSetting(key=f"default_radio_station:{NEW_SOMA}", value="1"))
    db.add(SystemSetting(key="default_radio_stations_seeded", value="1"))
    db.commit()
    db.close()
    with startup_against(session_factory):
        convert_legacy_radio_seed_marker()
        migrations.move_somafm_defaults_to_https()
        seed_default_radio_stations()
        seed_default_radio_stations()
    db = session_factory()
    try:
        soma = sorted(s.stream_url for s in db.query(RadioStation).filter(RadioStation.provider == "SomaFM"))
        assert soma == [NEW_SOMA, "https://ice6.somafm.com/lush-128-mp3"]
        assert db.get(SystemSetting, "default_radio_stations_seeded") is None
    finally:
        db.close()


def test_existing_default_row_is_moved_to_https_in_place(session_factory):
    station_id = _old_install_with_groove_salad(session_factory)
    with startup_against(session_factory):
        migrations.move_somafm_defaults_to_https()
        migrations.move_somafm_defaults_to_https()  # idempotent
        seed_default_radio_stations()
    db = session_factory()
    try:
        groove = db.query(RadioStation).filter(RadioStation.name == "SomaFM: Groove Salad").all()
        assert [(s.id, s.stream_url) for s in groove] == [(station_id, NEW_SOMA)]
        assert db.get(SystemSetting, f"default_radio_station:{NEW_SOMA}") is not None
        assert db.get(SystemSetting, f"default_radio_station:{OLD_SOMA}") is None
    finally:
        db.close()


def test_deleted_somafm_default_stays_deleted_after_the_move(session_factory):
    # Seeded once on the old URL, then deleted by the user: only the marker is left.
    db = session_factory()
    db.add(SystemSetting(key=f"default_radio_station:{OLD_SOMA}", value="1"))
    db.commit()
    db.close()
    with startup_against(session_factory):
        migrations.move_somafm_defaults_to_https()
        seed_default_radio_stations()
    db = session_factory()
    try:
        assert db.query(RadioStation).filter(RadioStation.stream_url.in_([OLD_SOMA, NEW_SOMA])).count() == 0
        assert db.get(SystemSetting, f"default_radio_station:{NEW_SOMA}") is not None
    finally:
        db.close()


def test_users_own_station_on_the_old_url_is_not_touched(session_factory, db_session):
    user_id = db_session.query(User).one().id
    db = session_factory()
    private = RadioStation(
        name="My Groove Salad", stream_url=OLD_SOMA, is_shared=False, owner_user_id=user_id
    )
    db.add(private)
    db.commit()
    private_id = private.id
    db.close()
    with startup_against(session_factory):
        migrations.move_somafm_defaults_to_https()
        seed_default_radio_stations()
    db = session_factory()
    try:
        mine = db.get(RadioStation, private_id)
        assert mine is not None
        assert (mine.name, mine.stream_url, mine.provider) == ("My Groove Salad", OLD_SOMA, None)
        # SomaFM is not a default: nothing is seeded on the new URL.
        assert db.query(RadioStation).filter(RadioStation.stream_url == NEW_SOMA).count() == 0
    finally:
        db.close()


def test_old_row_is_left_when_the_https_url_is_already_present(session_factory):
    _old_install_with_groove_salad(session_factory)
    db = session_factory()
    db.add(RadioStation(name="Added by hand", stream_url=NEW_SOMA, is_shared=True))
    db.commit()
    db.close()
    with startup_against(session_factory):
        migrations.move_somafm_defaults_to_https()
    db = session_factory()
    try:
        assert db.query(RadioStation).filter(RadioStation.stream_url == NEW_SOMA).count() == 1
        assert db.query(RadioStation).filter(RadioStation.stream_url == OLD_SOMA).count() == 1
    finally:
        db.close()


# The last plain-HTTP defaults (Radio Paradise Main Mix, RTÉ Radio 1, RTÉ 2FM)
# move to HTTPS with the same guarantees as the SomaFM move above.
HTTP_MOVES = migrations.DEFAULT_HTTPS_MOVES
_DEFAULT_BY_URL = {url: (name, provider) for name, url, _desc, provider in DEFAULT_RADIO_STATIONS}


def test_every_default_is_https_and_each_move_lands_on_a_default():
    assert all(url.startswith("https://") for _name, url, _desc, _provider in DEFAULT_RADIO_STATIONS)
    assert len(HTTP_MOVES) == 3
    for old_url, new_url, provider in HTTP_MOVES:
        assert old_url.startswith("http://") and new_url.startswith("https://")
        assert old_url not in _DEFAULT_BY_URL
        assert _DEFAULT_BY_URL[new_url][1] == provider


def _old_install_with_http_defaults(session_factory, provider_on_row: bool = True) -> dict[str, int]:
    """A database with the three defaults on their plain-HTTP URLs, each
    with its marker."""
    db = session_factory()
    stations = {}
    for old_url, new_url, provider in HTTP_MOVES:
        station = RadioStation(
            name=_DEFAULT_BY_URL[new_url][0], stream_url=old_url,
            provider=provider if provider_on_row else None, is_shared=True,
        )
        db.add(station)
        db.add(SystemSetting(key=f"default_radio_station:{old_url}", value="1"))
        stations[old_url] = station
    db.commit()
    ids = {url: station.id for url, station in stations.items()}
    db.close()
    return ids


def test_http_defaults_are_moved_to_https_in_place(session_factory):
    ids = _old_install_with_http_defaults(session_factory)
    with startup_against(session_factory):
        migrations.move_http_defaults_to_https()
        migrations.move_http_defaults_to_https()  # idempotent
        seed_default_radio_stations()
        seed_default_radio_stations()
    db = session_factory()
    try:
        assert db.query(RadioStation).count() == len(DEFAULT_RADIO_STATIONS)
        for old_url, new_url, _provider in HTTP_MOVES:
            rows = db.query(RadioStation).filter(RadioStation.name == _DEFAULT_BY_URL[new_url][0]).all()
            assert [(s.id, s.stream_url) for s in rows] == [(ids[old_url], new_url)]
            assert db.get(SystemSetting, f"default_radio_station:{new_url}") is not None
            assert db.get(SystemSetting, f"default_radio_station:{old_url}") is None
    finally:
        db.close()


def test_http_default_from_before_providers_is_moved_too(session_factory):
    _old_install_with_http_defaults(session_factory, provider_on_row=False)
    with startup_against(session_factory):
        migrations.move_http_defaults_to_https()
    db = session_factory()
    try:
        urls = {s.stream_url for s in db.query(RadioStation)}
        assert urls == {new_url for _old, new_url, _provider in HTTP_MOVES}
    finally:
        db.close()


def test_deleted_http_default_stays_deleted_after_the_move(session_factory):
    old_url, new_url, _provider = HTTP_MOVES[0]
    db = session_factory()
    db.add(SystemSetting(key=f"default_radio_station:{old_url}", value="1"))
    db.commit()
    db.close()
    with startup_against(session_factory):
        migrations.move_http_defaults_to_https()
        seed_default_radio_stations()
    db = session_factory()
    try:
        assert db.query(RadioStation).filter(RadioStation.stream_url.in_([old_url, new_url])).count() == 0
        assert db.get(SystemSetting, f"default_radio_station:{new_url}") is not None
        assert db.get(SystemSetting, f"default_radio_station:{old_url}") is None
    finally:
        db.close()


def test_private_or_edited_station_on_an_old_http_url_is_not_touched(session_factory, db_session):
    user_id = db_session.query(User).one().id
    (old_paradise, new_paradise, _p1), (old_rte1, new_rte1, _p2), _rest = HTTP_MOVES
    db = session_factory()
    private = RadioStation(name="My RP", stream_url=old_paradise, is_shared=False, owner_user_id=user_id)
    # A shared row an admin re-labelled with another provider is not a default any more.
    edited = RadioStation(name="Radio 1 (mine)", stream_url=old_rte1, provider="Custom", is_shared=True)
    db.add_all([private, edited])
    db.commit()
    private_id, edited_id = private.id, edited.id
    db.close()
    with startup_against(session_factory):
        migrations.move_http_defaults_to_https()
        seed_default_radio_stations()
    db = session_factory()
    try:
        mine = db.get(RadioStation, private_id)
        assert mine is not None and (mine.name, mine.stream_url) == ("My RP", old_paradise)
        kept = db.get(RadioStation, edited_id)
        assert kept is not None and (kept.name, kept.stream_url, kept.provider) == ("Radio 1 (mine)", old_rte1, "Custom")
        # The defaults themselves are seeded once on their HTTPS URLs.
        assert db.query(RadioStation).filter(RadioStation.stream_url == new_paradise).count() == 1
        assert db.query(RadioStation).filter(RadioStation.stream_url == new_rte1).count() == 1
    finally:
        db.close()


def test_old_http_row_is_left_when_the_https_url_is_already_present(session_factory):
    _old_install_with_http_defaults(session_factory)
    old_url, new_url, _provider = HTTP_MOVES[0]
    db = session_factory()
    db.add(RadioStation(name="Added by hand", stream_url=new_url, is_shared=True))
    db.commit()
    db.close()
    with startup_against(session_factory):
        migrations.move_http_defaults_to_https()
    db = session_factory()
    try:
        assert db.query(RadioStation).filter(RadioStation.stream_url == new_url).count() == 1
        assert db.query(RadioStation).filter(RadioStation.stream_url == old_url).count() == 1
        assert db.get(SystemSetting, f"default_radio_station:{new_url}") is not None
    finally:
        db.close()


def test_legacy_batch_marker_install_still_gets_the_rte_defaults_on_https(session_factory):
    """An install from before per-station markers never had the RTÉ stations
    (they came after the batch marker): converting the marker must not mark
    their new HTTPS URLs as deleted."""
    db = session_factory()
    db.add(SystemSetting(key="default_radio_stations_seeded", value="1"))
    db.commit()
    db.close()
    with startup_against(session_factory):
        convert_legacy_radio_seed_marker()
        migrations.move_http_defaults_to_https()
        seed_default_radio_stations()
    db = session_factory()
    try:
        urls = {s.stream_url for s in db.query(RadioStation)}
        assert {new_url for _old, new_url, provider in HTTP_MOVES if provider == "RTÉ"} <= urls
    finally:
        db.close()


def test_somafm_move_is_unchanged_by_the_shared_helper(session_factory):
    # A shared SomaFM-URL row labelled with another provider is still left alone.
    db = session_factory()
    db.add(RadioStation(name="Groove", stream_url=OLD_SOMA, provider="Radio Paradise", is_shared=True))
    db.commit()
    db.close()
    with startup_against(session_factory):
        migrations.move_somafm_defaults_to_https()
    db = session_factory()
    try:
        assert {s.stream_url for s in db.query(RadioStation)} == {OLD_SOMA}
    finally:
        db.close()
