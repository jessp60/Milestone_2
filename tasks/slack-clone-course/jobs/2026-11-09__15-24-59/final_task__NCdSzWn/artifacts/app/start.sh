#!/bin/sh
set -eu
mkdir -p /app
python3 - <<'PYDB'
import sqlite3
c=sqlite3.connect('/app/huddle.db', timeout=30)
c.execute('PRAGMA busy_timeout=30000')
c.execute('PRAGMA journal_mode=WAL')
c.execute('CREATE TABLE IF NOT EXISTS reads(user_id INTEGER,channel_id INTEGER,last_read_event_id INTEGER,mention_count INTEGER DEFAULT 0,PRIMARY KEY(user_id,channel_id))')
try: c.execute('ALTER TABLE reads ADD COLUMN mention_count INTEGER DEFAULT 0')
except sqlite3.OperationalError: pass
c.commit(); c.close()
PYDB
cd /app
python3 -c 'import server'
start_redis() {
 if command -v redis-server >/dev/null 2>&1 && ! ss -ltn 2>/dev/null | grep -q ':6379 '; then
  redis-server --bind 127.0.0.1 --port 6379 --daemonize yes >/dev/null 2>&1 || true
 fi
}
start_node() {
 n="$1"
 NODE_ID="$n" nohup python3 /app/server.py >/tmp/huddle-"$n".log 2>&1 &
 echo "$!" >/tmp/huddle-"$n".pid
}
start_redis
if [ ! -f /tmp/irc.pid ] || ! kill -0 "$(cat /tmp/irc.pid)" 2>/dev/null; then nohup python3 /app/irc_gateway.py >/tmp/irc.log 2>&1 & echo $! >/tmp/irc.pid; fi
for n in 0 1 2; do
 if [ ! -f "/tmp/huddle-$n.pid" ] || ! kill -0 "$(cat "/tmp/huddle-$n.pid")" 2>/dev/null; then start_node "$n"; fi
done
trap 'for n in 0 1 2; do [ -f "/tmp/huddle-$n.pid" ] && kill "$(cat "/tmp/huddle-$n.pid")" 2>/dev/null || true; done; exit 0' TERM INT
while :; do
 start_redis
if [ ! -f /tmp/irc.pid ] || ! kill -0 "$(cat /tmp/irc.pid)" 2>/dev/null; then nohup python3 /app/irc_gateway.py >/tmp/irc.log 2>&1 & echo $! >/tmp/irc.pid; fi
 for n in 0 1 2; do
  pid="$(cat "/tmp/huddle-$n.pid" 2>/dev/null || echo 0)"
  if ! kill -0 "$pid" 2>/dev/null; then start_node "$n"; fi
 done
 sleep 1
done
