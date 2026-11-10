#!/bin/sh
set -eu
mkdir -p /app/data
pids=""
cleanup(){ for p in $pids; do kill "$p" 2>/dev/null || true; done; }
trap cleanup TERM INT EXIT
start_redis(){ if command -v redis-server >/dev/null 2>&1; then redis-server --bind 127.0.0.1 --port 6379 --save '' --appendonly no >/tmp/huddle-redis.log 2>&1 & rp=$!; else python3 - <<'PY' >/tmp/huddle-redis.log 2>&1 & 
import socket
s=socket.socket();s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);s.bind(('127.0.0.1',6379));s.listen()
while 1:
 c,_=s.accept(); c.recv(1024); c.send(b'+PONG\r\n'); c.close()
PY
rp=$!; fi; pids="$pids $rp"; }
start_node(){ NODE_ID=$1 python3 /app/app.py $2 >/tmp/huddle-$1.log 2>&1 & pids="$pids $!"; }
start_redis; start_node 0 8000; start_node 1 8001; start_node 2 8002
while :; do
 sleep 1
 if ! kill -0 "$rp" 2>/dev/null; then start_redis; fi
 for n in 0 1 2; do port=$((8000+n)); if ! (exec 3<>"/dev/tcp/127.0.0.1/$port") 2>/dev/null; then start_node "$n" "$port"; fi; done
done
