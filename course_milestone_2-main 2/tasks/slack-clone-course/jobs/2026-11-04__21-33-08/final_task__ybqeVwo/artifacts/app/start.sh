#!/bin/sh
mkdir -p /app
[ -f /app/huddle.db ] || true
pids=""
trap 'kill $pids 2>/dev/null; exit 0' TERM INT
python3 /app/app.py redis & pids="$pids $!"
for port in 8000 8001 8002; do (while true; do python3 /app/app.py node "$port"; sleep 1; done) & pids="$pids $!"; done
wait
