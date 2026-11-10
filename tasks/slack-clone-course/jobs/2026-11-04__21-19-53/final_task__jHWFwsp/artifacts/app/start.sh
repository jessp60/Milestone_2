#!/bin/sh
mkdir -p /app
[ -f /app/state.db ] || : > /app/state.db
redis_p=""
if command -v redis-server >/dev/null 2>&1; then redis-server --bind 127.0.0.1 --port 6379 --save "" --appendonly no >/tmp/redis.log 2>&1 & redis_p=$!; fi
p0=""; p1=""; p2=""
trap 'kill $p0 $p1 $p2 $redis_p 2>/dev/null; exit 0' INT TERM EXIT
while :; do
 [ -n "$p0" ] && kill -0 "$p0" 2>/dev/null || python3 /app/server.py 0 >/tmp/node0.log 2>&1 & p0=$!
 [ -n "$p1" ] && kill -0 "$p1" 2>/dev/null || python3 /app/server.py 1 >/tmp/node1.log 2>&1 & p1=$!
 [ -n "$p2" ] && kill -0 "$p2" 2>/dev/null || python3 /app/server.py 2 >/tmp/node2.log 2>&1 & p2=$!
 if command -v redis-server >/dev/null 2>&1 && { [ -z "$redis_p" ] || ! kill -0 "$redis_p" 2>/dev/null; }; then redis-server --bind 127.0.0.1 --port 6379 --save "" --appendonly no >/tmp/redis.log 2>&1 & redis_p=$!; fi
 sleep 1
done
