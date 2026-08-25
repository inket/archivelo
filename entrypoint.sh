#!/bin/sh
# Optionally drop from root to a specific PUID/PGID before starting the app,
# so files written to the /data volume are owned by a real NAS user instead
# of root. Defaults to staying as root (PUID=0/PGID=0) when unset, matching
# the container's original behavior.
set -e

PUID="${PUID:-0}"
PGID="${PGID:-0}"

if [ "$PUID" = "0" ] && [ "$PGID" = "0" ]; then
    exec "$@"
fi

if ! getent group "$PGID" >/dev/null 2>&1; then
    groupadd -o -g "$PGID" appgroup
fi

if ! getent passwd "$PUID" >/dev/null 2>&1; then
    useradd -o -u "$PUID" -g "$PGID" -M -s /usr/sbin/nologin appuser
fi

chown -R "$PUID":"$PGID" /data

exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups "$@"
