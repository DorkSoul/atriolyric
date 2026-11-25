#!/bin/sh
# Starts the server as the unprivileged user PUID:PGID (default 1000:1000).
# Started as root (the default), it first hands /data to that user when that
# user cannot write to it (a data folder that Docker created as root), then
# drops root for good. Started
# as another user already (compose `user:`), it runs the server unchanged.
set -eu

if [ "$(id -u)" != 0 ]; then
    exec "$@"
fi

PUID=${PUID:-1000}
PGID=${PGID:-1000}

case "$PUID$PGID" in
    *[!0-9]*) echo "PUID and PGID must be numbers (got PUID=$PUID PGID=$PGID)" >&2; exit 1 ;;
esac

mkdir -p /data
if ! setpriv --reuid="$PUID" --regid="$PGID" --keep-groups test -w /data; then
    echo "Giving /data to $PUID:$PGID"
    chown -R "$PUID:$PGID" /data
fi

# --keep-groups keeps groups added with compose `group_add` (for example the
# host's render group for hardware video transcoding).
exec setpriv --reuid="$PUID" --regid="$PGID" --keep-groups --no-new-privs "$@"
