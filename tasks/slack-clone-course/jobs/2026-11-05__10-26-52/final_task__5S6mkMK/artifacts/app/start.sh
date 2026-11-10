#!/bin/sh
set -eu
mkdir -p /tmp
redis_loop() {
  while :; do
    if command -v redis-server >/dev/null 2>&1; then
      redis-server --bind 127.0.0.1 --port 6379 --save '' >/tmp/huddle-redis.log 2>&1 || true
    else
      sleep 2
    fi
    sleep 1
  done
}
redis_loop &
REDIS_PID=$!
start_node() {
  port=$1
  PORT=$port python3 /app/app.py >/tmp/huddle-$port.log 2>&1 &
  echo $! >/tmp/huddle-$port.pid
}
for port in 8000 8001 8002; do
  start_node "$port"
done
python3 /app/irc.py >/tmp/huddle-irc.log 2>&1 &
IRC_PID=$!
cleanup() {
  trap - TERM INT EXIT
  kill "$REDIS_PID" 2>/dev/null || true
  kill "$IRC_PID" 2>/dev/null || true
  for port in 8000 8001 8002; do
    if [ -s /tmp/huddle-$port.pid ]; then
      kill "$(cat /tmp/huddle-$port.pid)" 2>/dev/null || true
    fi
  done
  exit 0
}
trap cleanup TERM INT EXIT
while :; do
  for port in 8000 8001 8002; do
    pid=$(cat /tmp/huddle-$port.pid 2>/dev/null || true)
    if [ -z "$pid" ] || ! kill -0 "$pid" 2>/dev/null; then
      start_node "$port"
    fi
  done
  sleep 0.25
done
