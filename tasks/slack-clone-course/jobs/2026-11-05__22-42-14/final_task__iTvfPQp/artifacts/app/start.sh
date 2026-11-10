#!/usr/bin/env bash
exec 9>/tmp/huddle-start.lock
flock -n 9 || exit 0
set -u
mkdir -p /tmp
declare -A PIDS
port_busy() {
  local port="$1"
  (exec 3<>"/dev/tcp/127.0.0.1/$port") 2>/dev/null
}
start_node() {
  local n="$1" port=$((8000+n))
  if port_busy "$port"; then
    PIDS[$n]=""
    return
  fi
  PORT="$port" NODE_ID="$n" python3 /app/server.py >"/tmp/huddle-$n.log" 2>&1 &
  PIDS[$n]=$!
}
ensure_redis() {
  if ! ss -ltn "sport = :6379" 2>/dev/null | grep -q LISTEN; then
    if command -v redis-server >/dev/null 2>&1; then
      cat >/tmp/huddle-redis.conf <<'EOF'
bind 127.0.0.1
port 6379
daemonize yes
protected-mode no
save ""
appendonly no
dir /tmp
pidfile /tmp/huddle-redis.pid
logfile /tmp/huddle-redis.log
EOF
      redis-server /tmp/huddle-redis.conf >/dev/null 2>&1 || true
    fi
  fi
}
ensure_redis
for n in 0 1 2; do
  start_node "$n"
done
cleanup() {
  for n in 0 1 2; do
    if [[ -n "${PIDS[$n]:-}" ]]; then kill "${PIDS[$n]}" 2>/dev/null || true; fi
  done
}
trap cleanup TERM INT EXIT
while :; do
  ensure_redis
  for n in 0 1 2; do
    pid="${PIDS[$n]:-}"
    if [[ -z "$pid" ]] || ! kill -0 "$pid" 2>/dev/null; then
      PIDS[$n]=""
      start_node "$n"
    fi
  done
  sleep 1
done
