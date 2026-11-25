import unicodedata
from collections.abc import Generator

from sqlalchemy import Select, create_engine, event, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings

settings.database_path.parent.mkdir(parents=True, exist_ok=True)
# The image sets SQLITE_TMPDIR=/data/tmp so large sorts and temp indexes spill
# to the data volume, not the small /tmp tmpfs (PRAGMA temp_store_directory
# is deprecated). Created here, at import, so it exists before the engine's
# first query; were it missing SQLite would fall back to /var/tmp or /tmp.
SQLITE_TMP_DIR = settings.database_path.parent / "tmp"
SQLITE_TMP_DIR.mkdir(exist_ok=True)

engine = create_engine(
    f"sqlite:///{settings.database_path}",
    connect_args={"check_same_thread": False},
    # A grid view (Artists/Albums especially, on a large library) requests
    # art for every tile up front - hundreds to thousands of brief,
    # near-simultaneous requests each needing a connection just long enough
    # to look up a row. SQLAlchemy's default pool (5 + 10 overflow = 15) is
    # sized for a typical web app's steadier request rate, not that kind of
    # burst, and even a millisecond-scale hold times out waiting for a free
    # connection once arrivals outpace capacity by this much. SQLite
    # connections are cheap (especially in WAL mode - see below), so there's
    # no real cost to sizing this generously.
    pool_size=20,
    max_overflow=60,
    # An unhandled SQL error would otherwise log the statement's bound values:
    # password hashes, session digests, encrypted credentials.
    hide_parameters=True,
)


def fold_text(s: str | None) -> str | None:
    """Accent- and case-insensitive form of `s`, for search matching:
    "Beyoncé" -> "beyonce", "ß" -> "ss", full-width "Ｆｕｌｌ" -> "full".
    NFKD splits accented letters into base + combining mark (and maps
    compatibility forms such as full-width letters to plain ones), the
    marks are dropped, then casefold does the case part."""
    if s is None:
        return None
    if s.isascii():
        # Nothing to decompose, and casefold is lower for ASCII - most tags
        # take this path, and it is several times cheaper per row.
        return s.lower()
    decomposed = unicodedata.normalize("NFKD", s)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()


def _python_lower(s: str | None) -> str | None:
    return None if s is None else s.lower()


def register_sqlite_functions(dbapi_connection) -> None:
    """SQL functions every connection needs. atrio_fold is fold_text,
    applied at query time so search needs no stored normalized column.
    atrio_lower is Python's str.lower (SQLite's lower() only folds ASCII),
    so an ORDER BY in SQL sorts names exactly as a Python sort does."""
    dbapi_connection.create_function("atrio_fold", 1, fold_text, deterministic=True)
    dbapi_connection.create_function("atrio_lower", 1, _python_lower, deterministic=True)


def configure_sqlite_connection(dbapi_connection) -> None:
    """Per-connection setup every SQLite connection gets - the production
    engine below and the test suite's engines alike, so tests see the same
    foreign-key enforcement and locking behaviour production does.

    WAL lets readers (a WS auth check, a track play, another admin
    request) proceed while a long write - a full library scan is the
    worst case, one transaction touching every row - is still in
    progress, instead of every one of them failing outright with
    "database is locked" for the whole duration. busy_timeout is a
    backstop for the case WAL doesn't cover on its own: two writers
    actually colliding (e.g. a scan and a play-history insert) retry
    for up to 30s instead of failing immediately."""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=30000")
    # SQLite's default (FULL) fsyncs on every commit, and commits are
    # frequent here: one per scanned file, and one per playing user every
    # five seconds - each holding the single writer lock while the disk
    # catches up. NORMAL fsyncs only at WAL checkpoints. Under WAL that
    # cannot corrupt the database; a power failure or OS crash can lose
    # the last few commits (a saved position, a just-scanned file that the
    # next scan finds again), an application crash loses nothing. It is
    # per connection and not stored in the file, so it is set here.
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()
    register_sqlite_functions(dbapi_connection)


@event.listens_for(engine, "connect")
def _set_sqlite_pragmas(dbapi_connection, connection_record) -> None:
    configure_sqlite_connection(dbapi_connection)


def make_session_factory(bind) -> sessionmaker:
    """The session options production uses, for `bind`.

    Long-running operations (a library scan especially) commit in batches
    to keep the writer-lock window short rather than holding one
    transaction open for the whole operation - expire_on_commit=False
    keeps ORM objects usable across those intermediate commits without an
    implicit re-SELECT on next attribute access. Call sites that need a
    genuinely fresh row after a commit already do so explicitly via
    db.refresh()."""
    return sessionmaker(bind=bind, autoflush=False, autocommit=False, expire_on_commit=False)


SessionLocal = make_session_factory(engine)


def count_rows(db: Session, stmt: Select) -> int:
    """How many rows `stmt` returns, as SELECT count(*) FROM (stmt) - what
    the legacy Query.count() ran, so DISTINCT, GROUP BY and joins count the
    same way."""
    return db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
