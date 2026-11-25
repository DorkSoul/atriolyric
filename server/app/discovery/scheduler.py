"""The scheduled daily refresh of each user's discovery playlists."""

import logging
from datetime import date, datetime

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.discovery.discovery_playlist import refresh_discovery_playlist
from app.discovery.explore import explore_new_artists
from app.discovery.mixes import refresh_all_genre_mixes
from app.models import (
    DiscoveryState,
    User,
)

# The package's logger: every discovery module logs under one name.
logger = logging.getLogger("app.discovery")


def run_due_auto_refreshes(db: Session, current_hour: int | None = None) -> None:
    """Called periodically by the background scheduler loop (see main.py).
    Runs both discovery jobs, once per user per calendar day, at whatever
    local hour that user configured - never more than once a day even if
    the loop's interval fires several times within that hour.
    `current_hour` is injectable for tests; defaults to the real local hour.

    Hours and "today" are the server's local time. A user is due once their
    hour has passed today and today's run hasn't happened, so a tick that
    overruns the hour, a restart during it or a DST change that removes it
    does not skip the day."""
    today = date.today().isoformat()
    if current_hour is None:
        current_hour = datetime.now().hour
    due_states = db.scalars(
        select(DiscoveryState)
        .where(
            DiscoveryState.auto_refresh_enabled.is_(True),
            DiscoveryState.auto_refresh_hour <= current_hour,
            or_(
                DiscoveryState.last_auto_refresh_date.is_(None),
                DiscoveryState.last_auto_refresh_date != today,
            ),
        )
        .order_by(DiscoveryState.user_id)
    ).all()
    for state in due_states:
        user = db.get(User, state.user_id)
        if user is None:
            continue
        try:
            refresh_discovery_playlist(db, user)
            explore_new_artists(db, user)
            refresh_all_genre_mixes(db, user)
            state.last_auto_refresh_date = today
            db.commit()
        except Exception:  # one user's provider failure must not abort others
            db.rollback()
            logger.exception("Scheduled discovery refresh failed for user %s", user.id)
