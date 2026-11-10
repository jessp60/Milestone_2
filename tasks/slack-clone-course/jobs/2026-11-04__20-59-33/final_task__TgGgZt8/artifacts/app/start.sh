#!/bin/sh
set -eu
mkdir -p /tmp
[ -f /tmp/huddle_state.json ] || echo '{"users":{},"tokens":{},"workspaces":{},"channels":{},"messages":{},"files":{},"groups":{},"dms":{},"seq":0}' >/tmp/huddle_state.json
python3 /app/app.py 8000 &
p0=$!; python3 /app/app.py 8001 & p1=$!; python3 /app/app.py 8002 & p2=$!
trap 'kill "$p0" "$p1" "$p2" 2>/dev/null || true; wait' INT TERM EXIT
wait
