#!/bin/sh
set -u
mkdir -p /app/data
redis-server --bind 127.0.0.1 --port 6379 --save '' --appendonly no >/tmp/huddle-redis.log 2>&1 &
rp=$!
python3 /app/server.py &
p0=$!
python3 /app/irc.py >/tmp/huddle-irc.log 2>&1 &
ip=$!
NODE_ID=1 PORT=8001 python3 /app/server.py &
p1=$!
NODE_ID=2 PORT=8002 python3 /app/server.py &
p2=$!
trap 'kill -9 "$p0" "$p1" "$p2" "$rp" "$ip" 2>/dev/null || true; exit 0' TERM INT
while :; do
 kill -0 "$p0" 2>/dev/null || { NODE_ID=0 PORT=8000 python3 /app/server.py & p0=$!; }
 kill -0 "$p1" 2>/dev/null || { NODE_ID=1 PORT=8001 python3 /app/server.py & p1=$!; }
 kill -0 "$p2" 2>/dev/null || { NODE_ID=2 PORT=8002 python3 /app/server.py & p2=$!; }
 kill -0 "$rp" 2>/dev/null || { redis-server --bind 127.0.0.1 --port 6379 --save '' --appendonly no >/tmp/huddle-redis.log 2>&1 & rp=$!; }
 sleep 1
done
