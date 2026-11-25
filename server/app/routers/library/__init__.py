"""The /api/library routes, one module per area.

Each module's router is included here under the one prefix. The order of
inclusion is the order routes are matched in."""

from fastapi import APIRouter

from app.routers.library import (
    albums_artists,
    discovery_routes,
    folders,
    history,
    mixes,
    stats,
    tracks,
    videos,
)

router = APIRouter(prefix="/api/library", tags=["library"])
for _module in (folders, discovery_routes, tracks, videos, albums_artists, mixes, history, stats):
    router.include_router(_module.router)
