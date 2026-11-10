#!/bin/sh
mkdir -p /app
[ -f /app/state.json ] || echo '{"users":{},"tokens":{},"workspaces":{},"channels":{},"messages":{},"files":{},"groups":{},"events":{},"dms":{},"reads":{},"next":1}' >/app/state.json
for n in 0 1 2; do PORT=$((8000+n)) NODE_ID=$n python3 /app/server.py & echo $! >/app/node$n.pid; done
wait
