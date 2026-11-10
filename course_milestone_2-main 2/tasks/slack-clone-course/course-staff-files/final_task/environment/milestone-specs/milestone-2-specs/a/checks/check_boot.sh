#!/usr/bin/env bash
# Baseline boot check (the four milestone 0 conditions, written in /spec/a/a-1.md). Prints PASS/FAIL per condition.
# Usage: bash /spec/a/checks/check_boot.sh          conditions 1-3, changes nothing
#        bash /spec/a/checks/check_boot.sh --kill   also condition 4: kills the node on :8001
#                                                 (run this last; it may leave node 1 down)
# Exit 0 only if every condition that ran passed.
set -u
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
exec </dev/null
BODY=/tmp/check_boot_body.json
pass=0; fail=0
ok()  { [ "$FEEDBACK" = verbose ] && echo "PASS $1 $2"; pass=$((pass+1)); }
bad() { [ "$FEEDBACK" = verbose ] && echo "FAIL $1 $2"; fail=$((fail+1)); }

# hj <port> -> prints "<http-status> <node_id> <status-field>", with "?" for anything missing
hj() {
  local code
  rm -f "$BODY"
  code=$(curl -s -o "$BODY" -w '%{http_code}' --max-time 3 "http://127.0.0.1:$1/api/health" 2>/dev/null || true)
  echo "$code $(python3 -c 'import json,sys
try:
    d=json.load(open(sys.argv[1])); print(d.get("node_id","?"), d.get("status","?"))
except Exception: print("? ?")' "$BODY")"
}
healthy() {
  for p in 8000 8001 8002; do curl -fs --max-time 2 "http://127.0.0.1:$p/api/health" >/dev/null 2>&1 || return 1; done
}
# owner_pid <port> -> prints the pid of the process listening on that port, if any
owner_pid() {
  local pid=""
  if command -v ss >/dev/null 2>&1; then
    pid=$(ss -ltnp "sport = :$1" 2>/dev/null | grep -o 'pid=[0-9]*' | head -1 | cut -d= -f2)
  fi
  if [ -z "$pid" ] && command -v lsof >/dev/null 2>&1; then
    pid=$(lsof -ti "tcp:$1" -sTCP:LISTEN 2>/dev/null | head -1)
  fi
  echo "$pid"
}

# start_cluster: launch /app/start.sh fully detached (own session, no inherited pipes), so it
# survives this script and a command timeout in the caller's shell.
start_cluster() {
  # close every inherited descriptor above 2 so the caller's pipes are not held open by the cluster
  (cd /app && for fd in 3 4 5 6 7 8 9 10 11 12 13 14 15; do eval "exec $fd>&-" 2>/dev/null; done
   setsid nohup /app/start.sh >/tmp/start.log 2>&1 </dev/null &) 2>/dev/null
}

# AIDEV-NOTE: total wait is capped so the whole script fits the agent's 30s command limit.
if ! healthy; then
  start_cluster
  for _ in $(seq 1 15); do healthy && break; sleep 1; done
fi
healthy || { echo "FAIL 0 cluster is not answering /api/health on 8000, 8001, 8002 (see /tmp/start.log)"; echo "RESULT: 0 passed, 1 failed"; exit 1; }

# 1. Health endpoint on :8000
read -r c n st <<<"$(hj 8000)"
if [ "$c" = 200 ] && [ "$n" = 0 ] && [ "$st" = ok ]; then ok 1 "GET /api/health on 8000 returns 200 {status: ok, node_id: 0}"
else bad 1 "health on 8000: expected 200 with status ok and node_id 0, got status $c, node_id $n, status field $st"; fi

# 2. Every node reports its own id (8000 -> 0, 8001 -> 1, 8002 -> 2)
problems=""
for p in 8000 8001 8002; do
  read -r c n st <<<"$(hj $p)"
  want=$((p-8000))
  if [ "$c" != 200 ] || [ "$n" != "$want" ] || [ "$st" != ok ]; then
    problems="$problems port $p: got status $c, node_id $n, status field $st (expected 200, $want, ok);"
  fi
done
if [ -z "$problems" ]; then ok 2 "each node answers 200 with its own node_id (0, 1, 2)"
else bad 2 "node ids:$problems"; fi

# 3. A WebSocket with a bad token must be rejected
res=$(python3 - <<'PY'
import socket, time
try:
    s = socket.create_connection(("127.0.0.1", 8000), timeout=3)
except OSError:
    print("noconnect"); raise SystemExit
req = ("GET /api/ws?token=not-a-real-token-000000 HTTP/1.1\r\nHost: 127.0.0.1:8000\r\n"
       "Upgrade: websocket\r\nConnection: Upgrade\r\n"
       "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n\r\n")
s.sendall(req.encode())
s.settimeout(3)
try:
    data = s.recv(4096)
except OSError:
    print("rejected: connection closed"); raise SystemExit
if not data:
    print("rejected: connection closed"); raise SystemExit
head, _, rest = data.partition(b"\r\n\r\n")
parts = head.split(b"\r\n", 1)[0].decode("latin1").split()
code = parts[1] if len(parts) > 1 else "?"
if code != "101":
    print("rejected: HTTP " + code); raise SystemExit
# The server upgraded the connection. A rejected token must be closed right away.
if rest and (rest[0] & 0x0f) == 0x8:
    print("rejected: close frame after upgrade"); raise SystemExit
s.settimeout(0.5)
end = time.time() + 2
while time.time() < end:
    try:
        d = s.recv(4096)
    except socket.timeout:
        continue
    except OSError:
        print("rejected: connection closed after upgrade"); raise SystemExit
    if not d:
        print("rejected: connection closed after upgrade"); raise SystemExit
    if (d[0] & 0x0f) == 0x8:
        print("rejected: close frame after upgrade"); raise SystemExit
print("open")
PY
)
case "$res" in
  rejected*) ok 3 "websocket with a bad token is rejected ($res)" ;;
  *) bad 3 "websocket with a bad token: expected the connection to be rejected or closed, got '$res'" ;;
esac

# 4. One node dies: the other two keep answering (only with --kill)
if [ "${1:-}" = "--kill" ]; then
  pid=$(owner_pid 8001)
  if [ -z "$pid" ]; then
    bad 4 "could not find the process listening on :8001 to kill"
  else
    kill -9 "$pid" 2>/dev/null
    sleep 2
    read -r c0 n0 s0 <<<"$(hj 8000)"; read -r c2 n2 s2 <<<"$(hj 8002)"
    if [ "$c0" = 200 ] && [ "$s0" = ok ] && [ "$c2" = 200 ] && [ "$s2" = ok ]; then
      ok 4 "after killing node 1, nodes 0 and 2 still answer /api/health with 200 and status ok"
    else
      bad 4 "after killing the process on :8001: 8000 got status $c0 ($s0), 8002 got status $c2 ($s2); expected 200 and ok on both"
    fi
    back=no
    for _ in $(seq 1 4); do
      sleep 1
      read -r c1 n1 s1 <<<"$(hj 8001)"
      if [ "$c1" = 200 ]; then back=yes; break; fi
    done
    if [ "$back" = yes ]; then echo "INFO node 1 came back on its own"
    else
      # Node 1 stayed down. Restore the whole cluster so the next command finds all three nodes up.
      echo "INFO node 1 did not come back within 4s: restarting the cluster (stopping leftover nodes by PID first)"
      for p in 8000 8001 8002; do
        q=$(owner_pid $p); [ -n "$q" ] && kill "$q" 2>/dev/null
      done
      sleep 1
      start_cluster
      for _ in $(seq 1 10); do healthy && break; sleep 1; done
      if healthy; then echo "INFO cluster restarted: 8000, 8001 and 8002 answer /api/health again"
      else echo "INFO cluster is still down: start /app/start.sh in the background yourself and check all three ports"; fi
    fi
  fi
else
  echo "SKIP 4 crash tolerance not run: use --kill as the very last step before you submit"
fi

echo "RESULT: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
