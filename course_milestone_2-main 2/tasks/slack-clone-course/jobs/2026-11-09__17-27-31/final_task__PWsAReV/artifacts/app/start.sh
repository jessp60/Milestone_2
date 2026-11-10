#!/bin/sh
set -eu
mkdir -p /app
if ! ss -ltn 2>/dev/null | grep -q ':6379 '; then
  (command -v redis-server >/dev/null 2>&1 && redis-server --daemonize yes --bind 127.0.0.1 --port 6379 >/dev/null 2>&1 || true)
fi
pids=""
for p in 8000 8001 8002; do
  PORT=$p python3 /app/server.py &
  pids="$pids $!"
done
trap 'kill $pids 2>/dev/null || true' TERM INT EXIT
while :; do
 for p in 8000 8001 8002; do
  if ! kill -0 $(echo "$pids" | awk "{print \$$((p-7999))}") 2>/dev/null; then PORT=$p python3 /app/server.py & pids="$pids $!"; fi
 done
 sleep 1
done
