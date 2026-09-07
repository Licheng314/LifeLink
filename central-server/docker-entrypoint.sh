#!/bin/sh
set -eu

# Named volumes are created by Docker with permissive defaults.  The central
# directory contains the SQLite database and credential configuration, so the
# container owner is the only user allowed to traverse it.
mkdir -p /var/lib/lifelink/central
chmod 700 /var/lib/lifelink/central

exec python central_server.py "$@"
