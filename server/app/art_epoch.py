"""The art epoch: one number that changes whenever a scan or a cover art run
may have changed the picture an existing track serves.

A track's art address (/api/stream/<id>/art) is the same before and after
its file is retagged, and is cached for a day by browsers and by the Android
app's own copy. The web app appends this number to those addresses
(/api/version hands it out), so changed art shows on the next look instead
of a day later. New tracks need no bump: nothing holds a copy of them yet.
"""

import time

from sqlalchemy.orm import Session

from app.models import SystemSetting

ART_EPOCH_SETTING = "art_epoch"


def art_epoch(db: Session) -> int:
    """0 until the first bump."""
    row = db.get(SystemSetting, ART_EPOCH_SETTING)
    try:
        return int(row.value) if row is not None else 0
    except ValueError:
        return 0


def bump_art_epoch(db: Session) -> None:
    """Commits. Epoch milliseconds, and always more than the last value."""
    value = max(int(time.time() * 1000), art_epoch(db) + 1)
    row = db.get(SystemSetting, ART_EPOCH_SETTING)
    if row is None:
        db.add(SystemSetting(key=ART_EPOCH_SETTING, value=str(value)))
    else:
        row.value = str(value)
    db.commit()
