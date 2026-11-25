"""Per-user discovery state and settings."""


from sqlalchemy.orm import Session

from app.models import (
    DiscoveryState,
    User,
)


def _get_or_create_state(db: Session, user: User) -> DiscoveryState:
    state = db.get(DiscoveryState, user.id)
    if state is None:
        state = DiscoveryState(user_id=user.id)
        db.add(state)
        db.commit()
        db.refresh(state)
    return state


def get_discovery_state(db: Session, user: User) -> dict:
    from app.discovery.explore import explore_previews  # explore imports this module

    state = _get_or_create_state(db, user)
    previews, pending = explore_previews(state.explore_results_json, user_id=user.id)
    return {
        "discovery_seed_artists": state.discovery_seed_artists_json,
        "discovery_similar_artists": state.discovery_similar_artists_json,
        "discovery_tracks_added": state.discovery_tracks_added,
        "discovery_updated_at": state.discovery_updated_at,
        "explore_seed_artists": state.explore_seed_artists_json,
        "explore_results": previews,
        "explore_pending": pending,
        "explore_updated_at": state.explore_updated_at,
    }


def get_discovery_settings(db: Session, user: User) -> dict:
    state = _get_or_create_state(db, user)
    return {
        "auto_refresh_enabled": state.auto_refresh_enabled,
        "auto_refresh_hour": state.auto_refresh_hour,
    }


def update_discovery_settings(
    db: Session, user: User, enabled: bool, hour: int
) -> dict:
    state = _get_or_create_state(db, user)
    state.auto_refresh_enabled = enabled
    state.auto_refresh_hour = hour
    db.commit()
    return {
        "auto_refresh_enabled": state.auto_refresh_enabled,
        "auto_refresh_hour": state.auto_refresh_hour,
    }
