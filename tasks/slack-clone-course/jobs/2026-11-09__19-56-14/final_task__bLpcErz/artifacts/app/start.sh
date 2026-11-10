#!/bin/sh
set -eu
mkdir -p /app
[ -f /app/huddle.db ] || touch /app/huddle.db
python3 -c 'import sys;sys.path.insert(0,"/app");import server;server.init()'
redis_pid=""
irc_pid=""
start_irc() { python3 /app/irc.py >/tmp/huddle-irc.log 2>&1 & irc_pid=$!; }
start_irc
start_redis() {
 if command -v redis-server >/dev/null 2>&1; then
  redis-server --bind 127.0.0.1 --port 6379 --save '' --appendonly no >/tmp/huddle-redis.log 2>&1 &
  redis_pid=$!
 fi
}
start_redis
pids=""
for p in 8000 8001 8002; do PORT=$p python3 /app/server.py >/tmp/huddle-$p.log 2>&1 & pids="$pids $!"; done
trap 'kill $pids $redis_pid $irc_pid 2>/dev/null || true' EXIT INT TERM
while :; do
 if [ -n "$redis_pid" ] && ! kill -0 "$redis_pid" 2>/dev/null; then start_redis; fi
 if [ -n "$irc_pid" ] && ! kill -0 "$irc_pid" 2>/dev/null; then start_irc; fi
 for p in 8000 8001 8002; do
  if ! kill -0 $(echo "$pids"|awk "{print \$$((p-7999))}") 2>/dev/null; then PORT=$p python3 /app/server.py >/tmp/huddle-$p.log 2>&1 & pids="$pids $!"; fi
 done
 sleep 1
done
