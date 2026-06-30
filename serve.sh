#!/bin/sh
# Start the TubeNews web server with gunicorn.
#
# Usage:
#   ./serve.sh              — listens on 0.0.0.0 at the port in config.json (default 8000)
#   TUBENEWS_HTTPS=true ./serve.sh   — also marks session cookies Secure (use behind HTTPS)
#
# To keep it running after logout, start it under your preferred process manager,
# e.g.:  nohup ./serve.sh &     or via a FreeBSD rc.d/daemon service entry.

cd "$(dirname "$0")"

# Gunicorn writes its control socket to ~/.gunicorn/. On FreeBSD the www user
# has HOME=/nonexistent which exists but is not writable, so redirect to /tmp.
if [ ! -w "${HOME:-}" ]; then
  export HOME=/tmp
fi

PORT=$(python3 -c "
import json, sys
try:
    print(json.load(open('config.json')).get('port', 8000))
except Exception:
    print(8000)
" 2>/dev/null)
PORT=${PORT:-8000}

exec gunicorn -w 1 --timeout 30 -b "0.0.0.0:${PORT}" 'web.app:app'
