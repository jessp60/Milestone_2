#!/bin/sh
set -u
mkdir -p /app
p0=0
p1=0
p2=0
rp=0
ip=0
start_node() {
  n="$1"
  port=$((8000+n))
  NODE_ID="$n" python3 /app/server.py "$port" >/tmp/huddle-node-"$n".log 2>&1 &
  eval "p$n=$!"
}
start_node 0
start_node 1
start_node 2
start_redis() {
  redis-server --bind 127.0.0.1 --port 6379 --save '' --appendonly no >/tmp/huddle-redis.log 2>&1 &
  rp=$!
}
start_redis
start_irc() { python3 /app/irc.py >/tmp/huddle-irc.log 2>&1 & ip=$!; }
start_irc
trap 'kill "$p0" "$p1" "$p2" "$rp" "$ip" 2>/dev/null || true; exit 0' INT TERM EXIT
while :; do
  if ! kill -0 "$ip" 2>/dev/null; then start_irc; fi
  if ! kill -0 "$rp" 2>/dev/null; then
    start_redis
  fi
  for n in 0 1 2; do
    eval "pid=\${p$n}"
    if ! kill -0 "$pid" 2>/dev/null; then
      start_node "$n"
    fi
  done
  sleep 1
done
