from fastapi import APIRouter, Query

from app.deps import CurrentUser, DbSession
from app.library_search import search_library
from app.schemas import LibrarySearchOut

# Its own module rather than part of library.py: search reuses the playlist
# summaries from playlists.py, which itself imports from library.py.
router = APIRouter(prefix="/api/library", tags=["library"])


@router.get("/search", response_model=LibrarySearchOut)
def search(
    db: DbSession,
    user: CurrentUser,
    q: str = Query(default="", max_length=200),
) -> LibrarySearchOut:
    # No browse cache headers: every keystroke is a new query, and results
    # must reflect the library as it is now.
    return search_library(db, user, q)
