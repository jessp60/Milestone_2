#!/bin/sh
mkdir -p /app
redis-server --daemonize yes --bind 127.0.0.1 --port 6379 >/tmp/redis.log 2>&1 || true
if ! ss -ltn 'sport = :6667' 2>/dev/null | grep -q LISTEN; then python3 /app/irc.py >/tmp/irc.log 2>&1 & fi
while :; do
 for i in 0 1 2; do
  p=$((8000+i))
# removed
  if ! ss -ltn "sport = :$p" 2>/dev/null | grep -q LISTEN; then
   NODE_ID=$i python3 /app/app.py $p >/tmp/huddle-$i.log 2>&1 &
  fi
 done
 if ! ss -ltn "sport = :6379" 2>/dev/null | grep -q LISTEN; then
  redis-server --daemonize yes --bind 127.0.0.1 --port 6379 >/tmp/redis.log 2>&1 || true
 fi
 sleep 1
done
