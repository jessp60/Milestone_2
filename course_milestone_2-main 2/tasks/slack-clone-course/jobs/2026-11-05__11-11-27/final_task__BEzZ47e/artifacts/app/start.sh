#!/bin/sh
mkdir -p /app
[ -f /app/huddle.db ] || true
command -v redis-server >/dev/null 2>&1 && (redis-server --bind 127.0.0.1 --port 6379 --daemonize yes >/dev/null 2>&1 || true)
python3 -u /app/server.py --x >/tmp/huddle0.log 2>&1 & echo $! >/tmp/huddle0.pid
PORT=8001 python3 -u /app/server.py >/tmp/huddle1.log 2>&1 & echo $! >/tmp/huddle1.pid
PORT=8002 python3 -u /app/server.py >/tmp/huddle2.log 2>&1 & echo $! >/tmp/huddle2.pid
IRC=1 PORT=7999 python3 -u /app/server.py >/tmp/huddleirc.log 2>&1 & echo $! >/tmp/huddleirc.pid
trap 'kill $(cat /tmp/huddle0.pid /tmp/huddle1.pid /tmp/huddle2.pid /tmp/huddleirc.pid 2>/dev/null) 2>/dev/null; exit' TERM INT
while :; do
 for x in 0 1 2; do p=/tmp/huddle$x.pid; port=$((8000+x)); if ! kill -0 $(cat $p 2>/dev/null) 2>/dev/null; then PORT=$port python3 -u /app/server.py >/tmp/huddle$x.log 2>&1 & echo $! >$p; fi; done
 if ! ss -ltn 2>/dev/null | grep -q ':6379 '; then
  command -v redis-server >/dev/null 2>&1 && redis-server --bind 127.0.0.1 --port 6379 --daemonize yes >/dev/null 2>&1 || true
 fi
 sleep 1
done
