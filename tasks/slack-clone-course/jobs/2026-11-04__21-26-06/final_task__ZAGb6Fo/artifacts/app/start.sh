#!/bin/sh
mkdir -p /tmp
[ -f /tmp/huddle_state.json ] || echo '{"users":{},"tokens":{},"workspaces":{},"channels":{},"messages":{},"events":{},"next":{"user":1,"workspace":1,"channel":1,"message":1}}' >/tmp/huddle_state.json
redis-server --bind 127.0.0.1 --port 6379 --save '' --appendonly no >/tmp/huddle-redis.log 2>&1 &
rp=$!
for p in 8000 8001 8002; do (exec python3 /app/server.py "$p") & echo $! > "/tmp/huddle-$p.pid"; done
while :; do
 kill -0 "$rp" 2>/dev/null || { redis-server --bind 127.0.0.1 --port 6379 --save '' --appendonly no >/tmp/huddle-redis.log 2>&1 & rp=$!; }
 for p in 8000 8001 8002; do f="/tmp/huddle-$p.pid"; if ! kill -0 "$(cat "$f" 2>/dev/null)" 2>/dev/null; then (exec python3 /app/server.py "$p") & echo $! >"$f"; fi; done
 sleep 0.2
done
