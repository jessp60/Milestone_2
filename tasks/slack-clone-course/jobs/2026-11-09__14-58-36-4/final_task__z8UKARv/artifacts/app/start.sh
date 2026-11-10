#!/bin/sh
mkdir -p /app
if ! ss -ltn 2>/dev/null | grep -q ':6379 '; then (redis-server --bind 127.0.0.1 --port 6379 --daemonize yes >/dev/null 2>&1 || true); fi
if ! ss -ltn 2>/dev/null | grep -q ':6667 '; then (exec python3 /app/irc.py >/tmp/irc.log 2>&1) & echo $! > /tmp/irc.pid; fi
for n in 0 1 2; do
 p=$((8000+n)); if ! ss -ltn 2>/dev/null | grep -q ":$p "; then (exec python3 /app/server.py "$n" >/tmp/huddle-$n.log 2>&1) & echo $! > /tmp/huddle-$n.pid; fi
done
while :; do
 if ! ss -ltn 2>/dev/null | grep -q ':6667 '; then (exec python3 /app/irc.py >/tmp/irc.log 2>&1) & echo $! > /tmp/irc.pid; fi
 for n in 0 1 2; do p=$((8000+n)); if ! ss -ltn 2>/dev/null | grep -q ":$p "; then (exec python3 /app/server.py "$n" >/tmp/huddle-$n.log 2>&1) & echo $! > /tmp/huddle-$n.pid; fi; done
 if ! ss -ltn 2>/dev/null | grep -q ':6379 '; then redis-server --bind 127.0.0.1 --port 6379 --daemonize yes >/dev/null 2>&1 || true; fi
 sleep 1
done
