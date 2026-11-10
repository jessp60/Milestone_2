#!/bin/sh
set -eu
mkdir -p /app/data
redis_pid=""
start_redis() {
  if command -v redis-server >/dev/null 2>&1; then
    redis-server --bind 127.0.0.1 --port 6379 --save '' --appendonly no >/tmp/redis.log 2>&1 &
    redis_pid=$!
  fi
}
start_redis
pids=""
start_node() {
  p="$1"
  PORT="$p" nohup python3 /app/server.py >/tmp/huddle-$p.log 2>&1 &
  eval "pid_$p=$!"
  pids="$pids $p"
}
start_node 8000
start_node 8001
start_node 8002
trap 'exit 0' INT TERM
while :; do
  if command -v redis-server >/dev/null 2>&1 && { [ -z "$redis_pid" ] || ! kill -0 "$redis_pid" 2>/dev/null; }; then start_redis; fi
  for p in $pids; do
    eval "pid=\${pid_$p:-}"
    if [ -z "$pid" ] || ! kill -0 "$pid" 2>/dev/null; then
      start_node "$p"
    fi
  done
  sleep 0.5
done
