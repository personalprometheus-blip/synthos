#!/bin/bash
# dev_portal.sh — start the retail portal locally for offline design iteration.
#
# Uses the uv-managed venv at /Users/patrickmcguire/synthos/.venv-portal-dev
# (Python 3.13.13 with Flask + python-dotenv + cryptography).
#
# Not for production — pi5 runs gunicorn, this is Flask's built-in dev server
# for template/design work only.
#
# Usage:
#   ./scripts/dev_portal.sh            # start on port 5555
#   PORTAL_PORT=5556 ./scripts/dev_portal.sh   # use different port
#
# Ctrl-C to stop.

set -e

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$REPO_ROOT/.venv-portal-dev"
PORT="${PORTAL_PORT:-5555}"

# Check venv exists
if [ ! -x "$VENV/bin/python" ]; then
    echo "ERROR: venv not found at $VENV"
    echo "Rebuild with: uv venv --python 3.13 $VENV && uv pip install --python $VENV/bin/python flask python-dotenv cryptography"
    exit 1
fi

# Check port is free
if lsof -iTCP:$PORT -sTCP:LISTEN >/dev/null 2>&1; then
    echo "ERROR: port $PORT already in use. Run: lsof -iTCP:$PORT -sTCP:LISTEN"
    echo "Or start on a different port: PORTAL_PORT=$((PORT+1)) $0"
    exit 1
fi

echo "─────────────────────────────────────────────────"
echo "  Synthos retail portal — LOCAL DEV"
echo "  Python:  $($VENV/bin/python --version)"
echo "  Flask:   $($VENV/bin/python -c 'import flask; print(flask.__version__)')"
echo "  Port:    $PORT"
echo "  Browse:  http://localhost:$PORT/"
echo "─────────────────────────────────────────────────"

cd "$REPO_ROOT/synthos_build/src"
exec env PORTAL_PORT=$PORT "$VENV/bin/python" retail_portal.py
