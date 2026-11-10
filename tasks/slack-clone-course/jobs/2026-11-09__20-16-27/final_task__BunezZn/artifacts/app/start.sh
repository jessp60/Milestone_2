#!/bin/sh
set -u
mkdir -p /app
start_node() {
  p="$1"
  if ss -ltn "sport = :$p" 2>/dev/null | grep -q ":$p"; then return; fi
  PORT="$p" python3 /app/app.py >>"/app/node_${p}.log" 2>&1 &
  eval "pid_$p=$!"
}
start_irc() {
  python3 /app/irc.py >>/app/irc.log 2>&1 &
  irc_pid=$!
}
start_redis() {
  if command -v redis-server >/dev/null 2>&1; then
    redis-server --bind 127.0.0.1 --port 6379 --save "" --appendonly no >>/app/redis.log 2>&1 &
    redis_pid=$!
  else
    redis_pid=
  fi
}
start_node 8000
start_node 8001
start_node 8002
start_irc
start_redis
while :; do
  for p in 8000 8001 8002; do
    eval "pid=\${pid_$p:-}"
    if [ -n "$pid" ] && ! kill -0 "$pid" 2>/dev/null; then
      start_node "$p"
    fi
  done
  if [ -n "${irc_pid:-}" ] && ! kill -0 "$irc_pid" 2>/dev/null; then start_irc; fi
  if [ -n "${redis_pid:-}" ] && ! kill -0 "$redis_pid" 2>/dev/null; then start_redis; fi
  sleep 1
done
