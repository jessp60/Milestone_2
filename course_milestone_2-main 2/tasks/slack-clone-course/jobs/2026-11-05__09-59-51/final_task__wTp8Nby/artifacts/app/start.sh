#!/bin/sh
set -u
mkdir -p /app /tmp/huddle_events /tmp/huddle_presence
start_node() {
  p="$1"
  (PORT="$p" exec python3 /app/server.py >>"/tmp/huddle-$p.log" 2>&1) &
  eval "PID_$p=$!"
}
start_irc() {
  (exec python3 /app/irc.py >>/tmp/huddle-irc.log 2>&1) &
  IRC_PID=$!
}
start_redis() {
  if command -v redis-server >/dev/null 2>&1; then
    (exec redis-server --bind 127.0.0.1 --port 6379 --save "" --appendonly no >>/tmp/huddle-redis.log 2>&1) &
    REDIS_PID=$!
  else
    REDIS_PID=0
  fi
}
start_node 8000
start_node 8001
start_node 8002
start_irc
start_redis
while :; do
  for p in 8000 8001 8002; do
    eval "pid=\${PID_$p}"
    if ! kill -0 "$pid" 2>/dev/null; then
      start_node "$p"
    fi
  done
  if ! kill -0 "$IRC_PID" 2>/dev/null; then
    start_irc
  fi
  if [ "$REDIS_PID" -ne 0 ] && ! kill -0 "$REDIS_PID" 2>/dev/null; then
    start_redis
  fi
  sleep 0.2
done
