#!/bin/sh
set -eu
mkdir -p /app/data
redis-server --bind 127.0.0.1 --port 6379 --save "" --appendonly no >/tmp/huddle-redis.log 2>&1 &
rp=$!
p0=; p1=; p2=
trap 'kill $rp $p0 $p1 $p2 2>/dev/null || true' EXIT
while :; do
 kill -0 "$rp" 2>/dev/null || { redis-server --bind 127.0.0.1 --port 6379 --save "" --appendonly no >/tmp/huddle-redis.log 2>&1 & rp=$!; }
 for n in 0 1 2; do eval p=\${p$n-}; if ! kill -0 "$p" 2>/dev/null; then NODE_ID=$n python3 /app/server.py >/tmp/huddle-$n.log 2>&1 & eval p$n=$!; fi; done
 sleep .2
done
