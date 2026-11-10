#!/bin/sh
cd /app
mkdir -p /tmp/huddle
while :; do
 if ! (ss -ltn 2>/dev/null | grep -q ':6667 '); then nohup python3 /app/irc.py >/tmp/huddle/irc.log 2>&1 & echo $! >/tmp/huddle/irc.pid; fi
 if ! (ss -ltn 2>/dev/null | grep -q ':6379 '); then
  redis-server --bind 127.0.0.1 --port 6379 --daemonize yes --dir /tmp/huddle --dbfilename redis.rdb >/tmp/huddle/redis.log 2>&1
 fi
 for p in 8000 8001 8002; do
  if ! (ss -ltn 2>/dev/null | grep -q ":$p "); then
   nohup python3 /app/app.py "$p" >/tmp/huddle/$p.log 2>&1 &
   echo $! >/tmp/huddle/$p.pid
  fi
 done
 sleep 1
done
