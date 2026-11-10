#!/bin/sh
mkdir -p /app
python3 /app/server.py irc &
IP=$!
trap 'kill $IP 2>/dev/null; exit' TERM INT
while :; do
 for n in 0 1 2; do
  p=$((8000+n))
  if ! ss -ltn "sport = :$p" 2>/dev/null | grep -q LISTEN; then NODE=$n python3 /app/server.py $p >/tmp/huddle-$n.log 2>&1 & fi
 done
 sleep .4
done
