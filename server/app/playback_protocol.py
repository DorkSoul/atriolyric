"""Shapes of what the playback WebSocket sends, so the wire format is
documented in one place (web/src/api-types.ts is generated from it).

Kept out of app.schemas, which holds the REST request/response models.
"""

from typing import Literal

from pydantic import BaseModel


class PlaybackDeviceSnapshot(BaseModel):
    id: int
    name: str
    device_type: str
    is_active: bool
    is_connected: bool
    # Already ISO-formatted (naive UTC, `datetime.isoformat()`); pydantic
    # would otherwise reformat it.
    last_seen_at: str


class QueueOrigin(BaseModel):
    """What the queue was started from: a play source (see
    app.schemas.PlaySourceIn) - an album, artist, playlist, mix or Songs-tab
    filter - so a device can link back to it."""

    kind: str
    label: str
    payload: dict


class PlaybackStateSnapshot(BaseModel):
    media_item_id: int | None
    position_seconds: float
    is_playing: bool
    active_device_id: int | None
    queue_index: int
    queue_media_item_ids: list[int]
    queue_item_ids: list[int]
    is_shuffled: bool
    repeat_mode: Literal["off", "all", "one"]
    queue_context_count: int
    devices: list[PlaybackDeviceSnapshot]
    # `revision` grows by one with every broadcast change;
    # `current_queue_item_id` is the queue row at `queue_index` (null when
    # the queue is empty) - the id `track_ended`, `position_report` and
    # `seek` name to say which item they are about.
    revision: int
    current_queue_item_id: int | None
    # What the queue was started from, null when unknown
    # or once autoplay has moved on past it.
    queue_origin: QueueOrigin | None = None
