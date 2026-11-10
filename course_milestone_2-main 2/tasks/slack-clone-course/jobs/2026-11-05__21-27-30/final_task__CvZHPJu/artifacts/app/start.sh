#!/bin/sh
set -u
mkdir -p /app
spawn_node() {
  n="$1"; p=$((8000+n))
  NODE_ID="$n" python3 /app/app.py "$p" >/tmp/huddle-node-"$n".log 2>&1 &
  echo $! > /app/node"$n".pid
}
spawn_redis() {
  if command -v redis-server >/dev/null 2>&1; then
    if ! ss -ltnp 'sport = :6379' 2>/dev/null | grep -q LISTEN; then
      redis-server --bind 127.0.0.1 --port 6379 --save "" --appendonly no >/tmp/huddle-redis.log 2>&1 &
      echo $! > /app/redis.pid
    fi
  fi
}
for n in 0 1 2; do spawn_node "$n"; done
spawn_redis
if ! ss -ltnp 'sport = :6667' 2>/dev/null | grep -q LISTEN; then python3 /app/irc.py >/tmp/huddle-irc.log 2>&1 & echo $! > /app/irc.pid; fi
while :; do
  for n in 0 1 2; do
    p=$((8000+n))
    if ! curl -sf --max-time 1 "http://127.0.0.1:$p/api/health" >/dev/null 2>&1; then
      spawn_node "$n"
    fi
  done
  spawn_redis
  if ! ss -ltnp 'sport = :6667' 2>/dev/null | grep -q LISTEN; then python3 /app/irc.py >/tmp/huddle-irc.log 2>&1 & echo $! > /app/irc.pid; fi
  sleep 0.5
done
