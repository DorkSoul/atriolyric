from fastapi import APIRouter, HTTPException
from sqlalchemy.orm import Session

from app.access import can_access_root
from app.deps import CurrentUser, DbSession
from app.models import MediaItem, User
from app.preferences import set_track_state
from app.schemas import PreferenceIn, PreferenceOut

router = APIRouter(prefix="/api/media", tags=["favorites"])


def _require_media_item(db: Session, media_item_id: int, user: User) -> None:
    item = db.get(MediaItem, media_item_id)
    if item is None or not can_access_root(db, user, item.library_root_id):
        raise HTTPException(404, "Media item not found")


@router.put("/{media_item_id}/preference", response_model=PreferenceOut)
def set_preference(
    media_item_id: int,
    body: PreferenceIn,
    db: DbSession,
    user: CurrentUser,
) -> PreferenceOut:
    """The song heart: none -> liked -> disliked -> none. Liking adds the
    song to Favourites; disliking or clearing removes it (see
    app.preferences.set_track_state)."""
    _require_media_item(db, media_item_id, user)
    set_track_state(db, user, media_item_id, body.state)
    return PreferenceOut(state=body.state)
