# syntax=docker/dockerfile:1

# Base images are pinned by digest so a rebuild of the same commit gives the
# same image. To update: pull the tag, take the new digest from
# `docker buildx imagetools inspect <image>:<tag>`, and change both parts.

FROM node:22-alpine@sha256:0e1754758c2df4cfa3e68d59e2e1b7374bb7fd9ea6328eaaacf15cc934af8202 AS web-build
WORKDIR /build/web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

# The Python dependencies are installed in their own stage so that uv is not
# in the image that runs.
FROM python:3.12-slim-trixie@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b AS server-build
COPY --from=ghcr.io/astral-sh/uv:0.13.1@sha256:4a1f3666816dafadd20ceb71eac9a7d56d7ef30279f7d64048519ab901ada336 /uv /usr/local/bin/uv
WORKDIR /app/server
COPY server/pyproject.toml server/uv.lock ./
# Bytecode is compiled here because the runtime never writes any (see
# PYTHONDONTWRITEBYTECODE below; production also runs read-only).
RUN uv sync --frozen --no-dev --no-install-project --compile-bytecode
COPY server/app ./app
RUN /app/server/.venv/bin/python -m compileall -q app

FROM python:3.12-slim-trixie@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b AS runtime

# SQLITE_TMPDIR: SQLite's temporary files (large sorts, temp indexes) go to
# the data volume, not /tmp, which the production overlay makes a small
# tmpfs. The app creates the directory beside the database at startup.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    ATRIOLYRIC_DATABASE_PATH=/data/atriolyric.db \
    ATRIOLYRIC_WEB_DIST_PATH=/app/web \
    ATRIOLYRIC_ANDROID_APK_PATH=/app/android/atriolyric.apk \
    SQLITE_TMPDIR=/data/tmp

# `apt-get upgrade` first: the pinned base image keeps the packages it was
# built with, and Debian's security fixes arrive between its rebuilds. The
# pin still fixes Python and the base layers; this layer follows Debian's
# security archive. A builder that keeps layers would reuse this one for
# ever, so the CI build passes the day here: the cached layer is at most a
# day old.
ARG APT_REFRESH=unset
RUN echo "apt layer of $APT_REFRESH" \
    && apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && apt-get install -y --no-install-recommends ffmpeg tini \
       intel-media-va-driver mesa-va-drivers \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app/server

# The virtualenv is used at the path it was built at.
COPY --from=server-build /app/server/.venv ./.venv
COPY --from=server-build /app/server/app ./app
COPY --from=web-build /build/web/dist /app/web
# The Android app's APK when the release has one (see android-release/README.md).
COPY android-release/ /app/android/
COPY LICENSE /app/LICENSE

# Keep a compromised application process from running as root, and from
# rewriting its own code: /app stays owned by root, only /data is writable.
# The entrypoint starts as root only to hand /data to PUID:PGID (default
# 1000:1000), then runs the server as that user. The media folders are
# governed by the host's filesystem permissions, so PUID/PGID should be a
# host user that can read them.
RUN addgroup --system --gid 1000 atriolyric \
    && adduser --system --uid 1000 --gid 1000 --home /nonexistent --no-create-home atriolyric \
    && mkdir -p /data \
    && chown atriolyric:atriolyric /data
COPY --chmod=755 docker/entrypoint.sh /usr/local/bin/atriolyric-entrypoint

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["/app/server/.venv/bin/python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3).read()"]

# tini reaps the ffmpeg child processes and passes signals on.
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/atriolyric-entrypoint"]

# --workers 1 is required, not a default: the playback WebSocket registry,
# login limiter, setup code, task stop flags, HLS sessions and library cache
# all live in this one process.
# --timeout-keep-alive is longer than a reverse proxy's idle timeout
# (usually 60s), so the proxy never reuses a connection uvicorn just closed.
CMD ["/app/server/.venv/bin/uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--timeout-keep-alive", "75", "--timeout-graceful-shutdown", "20"]
