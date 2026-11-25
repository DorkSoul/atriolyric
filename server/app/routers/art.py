"""Custom artwork: upload, reset and fetch art for songs, albums, artists
and playlists. See app.custom_art for storage and precedence rules.

Routes (entity_type is media_item | album | artist | playlist; key is the
numeric id for media_item/playlist and the `art_key` from the album/artist
list responses for album/artist):

  PUT    /api/art/{entity_type}/{key}        upload (raw image body or multipart)
  DELETE /api/art/{entity_type}/{key}        reset to the default art
  GET    /api/art/{entity_type}/{key}        full image, 204 if none
  GET    /api/art/{entity_type}/{key}/thumb  <=500px jpg, 204 if none
"""

from email import policy
from email.parser import BytesParser
from typing import cast

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, Response
from sqlalchemy.orm import Session

from app.art_candidates import (
    candidate_bytes,
    candidate_origin,
    candidates_for,
    local_candidate_bytes,
)
from app.config import settings
from app.custom_art import (
    MAX_UPLOAD_BYTES,
    art_version,
    content_type,
    delete_custom_art,
    entity_key_for_url,
    find_item_art,
    get_custom_art,
    store_custom_art,
    validate_entity,
)
from app.deps import CurrentUser, DbSession
from app.models import CustomArt, MediaItem, User
from app.presenters import ART_CACHE_HEADERS, no_art_response
from app.schemas import (
    ArtCandidateOut,
    CandidateIn,
    CustomArtResultOut,
    ImageCreditOut,
)

router = APIRouter(prefix="/api/art", tags=["art"])


def _require_art_editor(user: User, entity_type: str) -> None:
    """Album, artist and song art is shared by every user of the library, so
    only admins change it; a playlist's art is its owner's (validate_entity
    already restricts playlists to their owner)."""
    if entity_type != "playlist" and not user.is_admin:
        raise HTTPException(403, "Admin access required to change shared artwork")


async def _read_body(request: Request) -> bytes:
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_UPLOAD_BYTES + 64 * 1024:
        raise HTTPException(413, "Image is too large")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_UPLOAD_BYTES + 64 * 1024:
            raise HTTPException(413, "Image is too large")
    return bytes(body)


def _image_from_body(content_type_header: str, body: bytes) -> bytes:
    """The raw body, or the first file part of a multipart/form-data upload."""
    if not content_type_header.lower().startswith("multipart/form-data"):
        return body
    message = BytesParser(policy=policy.HTTP).parsebytes(
        b"Content-Type: " + content_type_header.encode() + b"\r\n\r\n" + body
    )
    for part in message.iter_parts():
        if part.get_filename() or part.get_param("name", header="content-disposition") == "file":
            # decode=True yields bytes for a leaf part; the stub also allows Message.
            return cast(bytes, part.get_payload(decode=True)) or b""
    raise HTTPException(400, "No file in upload")


def _save_upload(
    db: Session, user: User, entity_type: str, entity_key: str, header: str, body: bytes
) -> dict:
    validate_entity(db, user, entity_type, entity_key)
    data = _image_from_body(header, body)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "Image is too large")
    try:
        row = store_custom_art(db, entity_type, entity_key, data, source="upload")
    except ValueError as exc:
        raise HTTPException(415, str(exc)) from exc
    return {"ok": True, "custom_art": True, "art_version": art_version(row)}


@router.put("/{entity_type}/{key}", response_model=CustomArtResultOut)
async def upload_art(
    entity_type: str,
    key: str,
    request: Request,
    db: DbSession,
    user: CurrentUser,
) -> dict:
    entity_key = entity_key_for_url(entity_type, key)
    _require_art_editor(user, entity_type)
    header = request.headers.get("content-type", "")
    return await run_in_threadpool(
        _save_upload, db, user, entity_type, entity_key, header, await _read_body(request)
    )


@router.delete("/{entity_type}/{key}", response_model=CustomArtResultOut)
def reset_art(
    entity_type: str,
    key: str,
    db: DbSession,
    user: CurrentUser,
) -> dict:
    entity_key = entity_key_for_url(entity_type, key)
    _require_art_editor(user, entity_type)
    validate_entity(db, user, entity_type, entity_key)
    delete_custom_art(db, entity_type, entity_key)
    return {"ok": True, "custom_art": False, "art_version": None}


def _serve(row: CustomArt | None, thumb: bool) -> Response:
    if row is None:
        return no_art_response()
    path = settings.custom_art_dir / (row.thumb_path if thumb else row.path)
    if not path.is_file():
        return no_art_response()
    return FileResponse(path, media_type=content_type(path), headers=ART_CACHE_HEADERS)


def _fetch(entity_type: str, key: str, db: Session, user: User, thumb: bool) -> Response:
    entity_key = entity_key_for_url(entity_type, key)
    validate_entity(db, user, entity_type, entity_key)
    if entity_type == "media_item":
        # Own art, else the album's - the same answer the track's art
        # endpoints give.
        item = db.get(MediaItem, int(entity_key))
        if item is None:  # validate_entity just checked it; this narrows the type
            raise HTTPException(404, "Media item not found")
        row = find_item_art(db, item)
    else:
        row = get_custom_art(db, entity_type, entity_key)
    return _serve(row, thumb)


@router.get("/{entity_type}/{key}")
def get_art(
    entity_type: str,
    key: str,
    db: DbSession,
    user: CurrentUser,
) -> Response:
    return _fetch(entity_type, key, db, user, False)


@router.get("/{entity_type}/{key}/thumb")
def get_art_thumb(
    entity_type: str,
    key: str,
    db: DbSession,
    user: CurrentUser,
) -> Response:
    return _fetch(entity_type, key, db, user, True)


@router.get("/{entity_type}/{key}/credit", response_model=ImageCreditOut | None)
def get_art_credit(
    entity_type: str,
    key: str,
    db: DbSession,
    user: CurrentUser,
) -> dict | None:
    """The credit stored with this entity's own art (a Wikimedia Commons
    photo's author and licence), to show beside it; null when it has none."""
    entity_key = entity_key_for_url(entity_type, key)
    validate_entity(db, user, entity_type, entity_key)
    row = get_custom_art(db, entity_type, entity_key)
    return row.credit_json if row is not None and row.credit_json else None


@router.get("/local-candidate")
def get_local_candidate(
    token: str,
    db: DbSession,
    user: CurrentUser,
) -> Response:
    """An embedded picture or folder image offered by "Choose artwork…"."""
    found = local_candidate_bytes(db, user, token)
    if found is None:
        raise HTTPException(404, "Image not found")
    data, mime = found
    return Response(data, media_type=mime, headers={"Cache-Control": "private, max-age=3600"})


@router.get("/{entity_type}/{key}/candidates", response_model=list[ArtCandidateOut])
def get_art_candidates(
    entity_type: str,
    key: str,
    db: DbSession,
    user: CurrentUser,
) -> list[dict]:
    """Artwork to choose from: Cover Art Archive, the files, and Wikimedia Commons."""
    if entity_type == "playlist":
        raise HTTPException(404, "Playlists have no artwork to choose from")
    entity_key = entity_key_for_url(entity_type, key)
    validate_entity(db, user, entity_type, entity_key)
    return candidates_for(db, user, entity_type, entity_key)


@router.post("/{entity_type}/{key}/candidates", response_model=CustomArtResultOut)
def apply_art_candidate(
    entity_type: str,
    key: str,
    body: CandidateIn,
    db: DbSession,
    user: CurrentUser,
) -> dict:
    """Use a candidate as this entity's custom art (downloaded and
    thumbnailed now, like an upload)."""
    entity_key = entity_key_for_url(entity_type, key)
    _require_art_editor(user, entity_type)
    validate_entity(db, user, entity_type, entity_key)
    origin = candidate_origin(db, entity_type, entity_key, body.candidate_id)
    data = candidate_bytes(db, user, body.candidate_id) if origin is not None else None
    if origin is None or data is None:
        raise HTTPException(502, "That image couldn't be downloaded")
    try:
        row = store_custom_art(db, entity_type, entity_key, data, source=origin[0], credit=origin[1])
    except ValueError as exc:
        raise HTTPException(415, str(exc)) from exc
    return {"ok": True, "custom_art": True, "art_version": art_version(row)}
