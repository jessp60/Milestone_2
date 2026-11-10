#!/bin/sh
set -u
mkdir -p /app/data
exec 9>/app/data/start.lock
if ! flock -n 9; then exit 0; fi
start_redis() {
  if ! ss -ltn 2>/dev/null | grep -q '127.0.0.1:6379'; then
    redis-server --bind 127.0.0.1 --port 6379 --save '' >/tmp/huddle-redis.log 2>&1 &
    echo $! >/app/data/redis.pid
  fi
}
start_node() {
  i="$1"; port=$((8000+i)); pidfile="/app/data/node$i.pid"
  if ss -ltn 2>/dev/null | grep -q "127.0.0.1:$port"; then
    return
  fi
  PORT="$port" NODE_ID="$i" python3 -u /app/server.py >/tmp/huddle-node$i.log 2>&1 &
  echo $! >"$pidfile"
}
while :; do
  start_redis
  start_node 0
  start_node 1
  start_node 2
  sleep 1
done
