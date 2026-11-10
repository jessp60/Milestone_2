#!/bin/sh
mkdir -p /app
[ -f /app/huddle.db ] || true
while :; do
 for n in 0 1 2; do
  if ! ss -ltn 2>/dev/null | grep -q ":$((8000+n)) "; then NODE_ID=$n python3 /app/server.py >/tmp/huddle-$n.log 2>&1 & echo $! >/tmp/huddle-$n.pid; fi
 done
 if ! ss -ltn 2>/dev/null | grep -q ':6667 '; then python3 /app/server.py irc >/tmp/huddle-irc.log 2>&1 & echo $! >/tmp/huddle-irc.pid; fi
 sleep 1
done
