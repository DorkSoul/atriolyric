# Playback WebSocket protocol

The play queue, position, shuffle, repeat and the list of devices live on the server, per user. Every open tab or device connects to `/ws/playback`, sends commands and receives the new state. Sources: `server/app/playback_protocol.py` (snapshot shape), `server/app/routers/playback.py` (handlers), `server/app/playback.py` (rules), `web/src/PlayerContext.tsx` (the web client).

## Connecting

- URL: `/ws/playback`, authenticated by the session cookie. Messages are JSON text frames.
- A connection with an `Origin` that is neither the page's own host nor in `ATRIOLYRIC_ALLOWED_ORIGINS` is accepted and then closed at once with 4403.
- A connection without a valid session is accepted and then closed at once with 4401. Nothing sent on a refused socket is read. A session that ends while the socket is open (logout, expiry, password change, account deleted) closes it with 4401 after the next message, or immediately when the server revokes the session.
- The first message must be `register_device`. Until a device is registered, every other message is silently ignored.

## Close codes

| Code | Meaning | What the web client does |
|------|---------|--------------------------|
| 4000 `replaced` | Another socket registered the same device (the same `client_token`, e.g. a second tab of one browser) and this one was dropped | Does not reconnect by timer. It reconnects only when its tab is shown or focused again |
| 4401 | Not authenticated, or the session ended | Stops and shows the login page. A 1006 (a handshake that failed, for example a proxy that refuses an unauthenticated socket) makes it check `/api/auth/me` and do the same on a 401 |
| 4403 | Origin not allowed | No special handling: reconnects like an ordinary drop |
| 1011 `undeliverable` | A state broadcast to this socket failed or was not taken within 5 s (a dead or stalled connection). The server closes it and marks the device disconnected; on a truly stalled connection the close frame may never arrive and the browser sees 1006 | Reconnects like an ordinary drop |
| 4408 `silent` | The socket delivered no message for 150 s (the page behind it is gone or frozen; a live client sends a heartbeat every 20 s, at worst once a minute in a throttled background tab). The device is marked offline | No special handling: reconnects like an ordinary drop |

Any other close is an ordinary drop: the client reconnects with exponential backoff from 1 s up to 30 s, abandons a handshake still pending after 10 s, and replaces an open socket that missed two heartbeat answers and heard nothing for 45 s. To avoid two tabs evicting each other in a loop, the server does not close the previous socket if it registered less than 3 seconds ago.

## Server to client

`state` is sent to every connected device of the user after each change:

```json
{
  "type": "state",
  "state": { ...snapshot... },
  "position_changed": true,
  "source_device_id": 7,
  "source_command": "seek"
}
```

`position_changed` says the change moved the playback position. `source_device_id` and `source_command` say which device's command caused it, so a device can recognise the echo of its own position report.

Other messages: `registered` (`{"type": "registered", "device_id": 7}`, the reply to `register_device`), `heartbeat_ack` (see below), `error` (`{"type": "error", "message": "..."}`, sent to the sender only, when a command is invalid or refused) and `audio_settings_changed` (the user's volume levelling and equaliser settings changed on another device). Clients must ignore message types they do not know.

### Snapshot

| Field | Type | Meaning |
|-------|------|---------|
| `media_item_id` | integer or null | The current item |
| `position_seconds` | number | Saved position in the current item |
| `is_playing` | boolean | Whether playback is on |
| `active_device_id` | integer or null | The device whose element produces the sound |
| `queue_index` | integer | Index of the current item in `queue_media_item_ids` |
| `queue_media_item_ids` | integer list | The materialised part of the queue, in play order |
| `queue_item_ids` | integer list | Queue row ids, parallel to `queue_media_item_ids` |
| `is_shuffled` | boolean | Shuffle on |
| `repeat_mode` | `off`, `all` or `one` | Repeat mode |
| `queue_context_count` | integer | Size of the context the queue was started from (an album, a playlist, the whole library). The queue itself holds only a window of it |
| `devices` | list | Each: `id`, `name`, `device_type`, `is_active`, `is_connected`, `last_seen_at` (ISO 8601, naive UTC) |
| `revision` | integer | Grows by one with every broadcast change |
| `current_queue_item_id` | integer or null | The queue row at `queue_index`; null when the queue is empty |

`revision` lets a client drop a snapshot older than one it has already applied on the same socket (a slow link can reorder them). `current_queue_item_id` is the id that `track_ended`, `position_report` and `seek` name to say which item they are about. A queue row id identifies one entry, even when the same song is queued twice.

## Client to server

Every message is an object with a string `type`. A malformed message gets an `error` reply. An unknown `type` from a registered device gets `error: Unknown message type: ...`.

| `type` | Fields | Effect |
|--------|--------|--------|
| `register_device` | `name` (string, default `Device`), `device_type` (string, default `web`), `resume_playback` (boolean, default false), `client_token` (string up to 128 characters, optional) | Registers this socket. The reply is `registered`, then a `state` broadcast. See the device rules below |
| `heartbeat` | none | Marks the device alive. Answered with `heartbeat_ack` to this socket only; nothing is broadcast. The web client sends one every 20 s. Every message counts as a sign of life, not only this one (see the device-claim rule and close code 4408) |
| `set_queue_context` | `context` (object), `start_media_item_id` (integer), `start_index` (integer, optional), `position_seconds` (number, optional), `paused` (boolean, optional) | Replaces the queue and starts playing. With `position_seconds` the start item begins there, and with `paused: true` it stays paused: the Android app sends both to hand over a queue it played while it could not reach the server (see `mobile/README.md`). A malformed value gets `Invalid queue`. `context` is `{"kind": "ids", "ids": [...]}` (up to 5000 ids) or `{"kind": "library", "filters": {...}}` with the keys `q`, `genre`, `tag`, `artist`, `album` and `artist_is_null`. The requesting device claims output (see below) |
| `add_to_queue` | `media_item_id` (integer), `mode` (`next` or `end`, default `end`) | Adds an item |
| `add_many_to_queue` | `media_item_ids` (non-empty integer list, required, at most 5000), `mode` (`next` or `end`, required) | Adds the items in the order given, as one change: one `state` broadcast and one new `revision`. `next` puts the first of them right after the current item. Each item is checked as for `add_to_queue`; one that is missing or not visible refuses the whole batch (`Media item N not found`) and nothing changes. A missing or malformed field gets `Invalid add-to-queue command`, more than 5000 items `Too many items to queue at once`. The web client uses it for "Add next in queue" and "Add last in queue" on an album, artist or playlist |
| `play` | none | Starts playback. The requesting device claims output (see below) |
| `pause` | `position_seconds` (number or null, the key required) | Pauses. A number is applied only when it comes from the active device: where the listener paused. `null` keeps the saved position |
| `seek` | `position_seconds` (number), `queue_item_id` (integer, required) | A scrub. Applied only while that item is current; otherwise ignored, with nothing sent back |
| `position_report` | `queue_item_id` (integer, required), `position_seconds` (number, required) | The active device's periodic position save. Dropped silently (nothing sent back) from a device that is not active or for an item that is not current |
| `track_ended` | `queue_item_id` (integer, required) | The active device's element reached the natural end of that queue row. Only the active device may send it (otherwise `error`). Ignored while that row is no longer current: nothing changes, nothing is broadcast, and the sender gets the current `state` to converge on. Repeat `one` restarts the item; otherwise the queue advances |
| `next`, `prev` | none | Skip |
| `jump_to_index` | `index` (integer) | Play that queue entry now |
| `set_shuffle` | `enabled` (boolean) | Shuffle on or off |
| `set_repeat` | `mode` (`off`, `all` or `one`) | Repeat mode |
| `remove_from_queue` | `queue_item_id` (integer) | Removes a queue row |
| `move_queue_item` | `queue_item_id` (integer), `direction` (`-1` or `1`) | Moves one place up or down |
| `reorder_queue_item` | `queue_item_id` (integer), `target_index` (integer) | Moves to an index |
| `transfer_to` | `device_id` (integer) | Makes a connected device the active one. Refused if the device is unknown or offline |
| `remove_device` | `device_id` (integer) | Forgets a device |
| `client_diagnostic` | `event` (`media_error` or `media_ended`) plus a few short identifier or numeric fields | Written to the server log so a phone problem can be traced. Never broadcast |

Numbers must be real JSON numbers: `NaN` is sent by `JSON.stringify` as `null`, which counts as "no position" (accepted only on `pause`). `position_seconds` must be finite and not negative. `queue_item_id` must be a positive integer.

## Device rules

- A browser keeps a `client_token` in its storage and sends it on every connection. The server reuses the device row with that token, so a phone that reconnects is the same device.
- The first device to register when nothing is active becomes the active device.
- A reconnect never moves output to the reconnecting device. A locked phone may still be playing with its socket suspended.
- `resume_playback` is `true` when the page is reconnecting after a drop or reload that should keep playing. If the server state says "playing" but that device was disconnected and `resume_playback` is false (the browser was really closed and reopened), playback is set to paused so stale state does not start by itself.
- Device-claim rule: `play` and `set_queue_context` from a connected device make it the active device when nothing is audible elsewhere, that is when playback is stopped, or the active device is offline or gone. If the active device is connected and playing, they stay remote-control commands for it. When the old active device comes back, it finds another device active and stops its own audio.
- For that rule "connected" means a live socket: the active device has a socket open on this server and has sent a message in the last 10 seconds. A playing active device sends a `position_report` every 5 seconds, so silence means its page is gone or frozen even when no close has arrived (a tab closed behind a proxy that keeps its half of the connection open, a frozen background tab whose browser still answers pings). Its row may still say `is_connected: true` in snapshots until the socket closes (at the latest after 150 s, close code 4408; sooner when a broadcast to it fails, close code 1011); the claim does not wait for that.
- `transfer_to` always moves output to the target.
- Only the active device's element knows where playback is, so `pause` positions, `position_report` and `track_ended` from any other device are ignored.
- A disconnected device row unseen for 30 days is pruned at startup.

## Recording plays

Plays are recorded by an HTTP call, not over the socket. After the client has listened to half a track or music video or four minutes, whichever comes first, it sends:

```
POST /api/stream/{media_item_id}/played
{"started_at": "2026-11-12T10:15:00Z", "queue_item_id": 123, "listened_seconds": 95.2}
```

The body is required, and so is `started_at`: a request without a body or without `started_at` gets `422` and records nothing. `queue_item_id` and `listened_seconds` are optional. A plain video (`kind` `video`) is reported once, when it loads, with only `{"started_at": ...}` (the moment it loaded).

`started_at` is when the play began (ISO 8601; no time zone means UTC). It must be no older than 14 days and no more than 5 minutes in the future, otherwise `422`. The server records at most one play per user, item and `started_at`: a repeat of the same report, for example a retry after a lost answer, returns `{"ok": true, "recorded": false}` and changes nothing. A new play returns `{"ok": true, "recorded": true}` and is then scrobbled to ListenBrainz in the background, for a user who linked a ListenBrainz account. The client keeps unconfirmed reports in its storage and retries them with backoff; it drops one the server refuses with a 4xx other than 401, 408 or 429.

## Recording how listens end

A second HTTP call says how each listen ended. It feeds the mixes (see [mixes.md](mixes.md)); it is never a play and never scrobbled.

```
POST /api/stream/{media_item_id}/listened
{"started_at": "2026-11-12T10:15:00Z", "ended_at": "2026-11-12T10:15:41Z", "listened_seconds": 38.4,
 "duration_seconds": 212.0, "position_seconds": 39.1, "end_reason": "skipped",
 "start_kind": "auto", "source_kind": "album"}
```

**Who sends it, and when.** The device producing the sound sends it once for each track or music video it started and then left, whether or not that listen counted as a play. A remote control never sends one. A plain video (`kind` `video`) is not reported. `started_at` is the same value the play report uses (the moment listening began), so a counted play and its outcome join on user, item and `started_at`; a listen that never played (an `error` before any sound) uses the moment it was set up. The web client does not send a listen with less than 2 seconds heard, except an `error`: that is scanning past items with Next, not a judgement of them. The server itself stores whatever it receives; it ignores a skip under 2 seconds only when it reads the data.

**Body.**

| Field | Type | Meaning |
|-------|------|---------|
| `started_at` | ISO 8601 string, required | When the listen began. No time zone means UTC. At most 14 days old and at most 5 minutes in the future |
| `ended_at` | ISO 8601 string or null | When it was left. Default: when the server received it. Not before `started_at`, at most 5 minutes in the future |
| `listened_seconds` | number, required | Seconds actually heard, 0 to 86400 |
| `duration_seconds` | number or null | The item's length, 0 to 604800 |
| `position_seconds` | number or null | Where in the item it was left, 0 to 604800 |
| `end_reason` | string, required | `finished` (played to its end), `skipped` (the user moved on while it played: Next, Previous, another item, removed from the queue), `stopped` (left while paused, or playback stopped or the queue was cleared), `transferred` (another device took over), `error` (it could not be played) or `unknown` (the page went away) |
| `start_kind` | `auto`, `user` or null | What started it: `auto` is the queue moving on by itself or Next; `user` is a pick (started a list at it, jumped to it, Previous). Null when the client could not tell |
| `source_kind` | `album`, `artist`, `mix`, `playlist`, `library` or null | The kind of list the user started the queue from, when known |

**Answers.**

| Status | Body | Meaning |
|--------|------|---------|
| 200 | `{"ok": true, "recorded": true}` | Stored |
| 200 | `{"ok": true, "recorded": false}` | A listen of the same user, item and `started_at` was already stored (a retry); nothing changed |
| 404 | `Media item not found` | No such item, or one in a library the user cannot access |
| 422 | `started_at is out of range` or `ended_at is out of range` | A time outside the bounds above |
| 422 | field errors | A missing or malformed field, or a value outside the allowed ones above (for example another `end_reason`) |
| 401 | | No valid session |

**Retries.** The client puts each outcome in a queue kept in the tab's session storage (`atriolyric.pending-listens`, at most 100, the newest kept) and sends it after the pending play reports, oldest first. A report the server could not take (offline, a timeout, a 5xx, 401, 408 or 429) stays queued and is retried with backoff from 5 seconds, doubling up to 5 minutes, and also after a reload of the tab or when the network or the playback socket comes back. One the server refuses with another 4xx is dropped. One whose `started_at` is more than 14 days old is dropped. Because the server stores one listen per user, item and `started_at`, a report whose answer was lost is safe to send again.

**When the page closes.** On `pagehide` the web client closes the listen it is playing as `unknown` and, besides queuing it, sends the same body as a `fetch` with `keepalive: true`, which the browser finishes after the page is gone. If that request fails or is unsupported, the queued copy is sent after the next load of the tab, and the server takes the listen once.

## Required fields

These fields are required. A message without one gets the usual `error` reply sent to the sender only (`{"type": "error", "message": ...}`, the message text in brackets below) and changes nothing: no state change, no broadcast, no new `revision`.

- `track_ended` must carry `queue_item_id` (`Invalid queue item id`). The web client names the row its element is playing (`trackEndedMessage`, `messageQueueItemIdFor` and `queueItemIdFor` in `web/src/PlayerContext.tsx`). When its own bookkeeping cannot tell the row, it names the last state's `current_queue_item_id`, provided that state's current item is the song the element was playing; with the same song queued twice that is the row the server says is playing. When the element's song is not the server's current item at all, the end is stale: the client sends no `track_ended`, does not advance along its own copy of the queue, and follows the server's last state instead (as it does when the server answers a stale `track_ended` with the current state).
- `seek` must carry `queue_item_id` (`Invalid queue item id`). The active device names the row its element is playing, a remote control the row of the item it shows. A client that cannot name a row the server is playing does not send the seek.
- `pause` must carry the key `position_seconds` (`Invalid pause position`): a number from the device producing the sound when its element has a usable position, otherwise `null` (a remote control, no metadata loaded yet, a media error, a non-finite `currentTime`). On `null`, or a number from a device that is not active, the server keeps its saved position (at most one `position_report` old).
- `position_report` must carry both `queue_item_id` and `position_seconds` (`Invalid position report`).
- `add_many_to_queue` must carry both `media_item_ids` and `mode` (`Invalid add-to-queue command`).
- `POST /api/stream/{id}/played` must have a body with `started_at` (`422`, see Recording plays).
- `POST /api/stream/{id}/listened` must have a body with `started_at`, `listened_seconds` and `end_reason` (`422`, see Recording how listens end).

`seek` is accepted from any device at any time. The web client saves its position every five seconds with `position_report`, not with `seek`.

The server and the web client ship in one image. A page loaded from an older build than the server is not supported and must be reloaded: the shapes older pages sent without these fields are refused as described above.
