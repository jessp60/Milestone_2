#!/bin/sh
set -u
mkdir -p /app
if ! kill -0 "$(cat /tmp/huddle-irc.pid 2>/dev/null || echo 0)" 2>/dev/null; then python3 /app/irc.py >/tmp/huddle-irc.log 2>&1 & echo $! >/tmp/huddle-irc.pid; fi
launch_redis() {
  if command -v redis-server >/dev/null 2>&1; then
    redis-server --bind 127.0.0.1 --port 6379 --save '' --appendonly no >/tmp/huddle-redis.log 2>&1 &
    echo "$!" >/tmp/huddle-redis.pid
  fi
}
launch_redis
launch_node() {
  n="$1"; p=$((8000+n))
  NODE_ID="$n" PORT="$p" python3 /app/server.py >>"/tmp/huddle-$p.log" 2>&1 &
  echo "$!" >"/tmp/huddle-$p.pid"
}
for n in 0 1 2; do
  p=$((8000+n))
  if ! kill -0 "$(cat "/tmp/huddle-$p.pid" 2>/dev/null || echo 0)" 2>/dev/null; then launch_node "$n"; fi
done
while :; do
  if ! kill -0 "$(cat /tmp/huddle-irc.pid 2>/dev/null || echo 0)" 2>/dev/null; then python3 /app/irc.py >/tmp/huddle-irc.log 2>&1 & echo $! >/tmp/huddle-irc.pid; fi
  if command -v redis-server >/dev/null 2>&1; then
    rpid=$(cat /tmp/huddle-redis.pid 2>/dev/null || echo 0)
    if ! kill -0 "$rpid" 2>/dev/null; then launch_redis; fi
  fi
  for n in 0 1 2; do
    p=$((8000+n)); f="/tmp/huddle-$p.pid"
    pid=$(cat "$f" 2>/dev/null || echo 0)
    if ! kill -0 "$pid" 2>/dev/null; then
      launch_node "$n"
    fi
  done
  sleep 0.25
done
