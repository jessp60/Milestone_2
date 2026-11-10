#!/usr/bin/env bash
# Shared helpers for the component E checks. Sourced by check_e_*.sh, not run directly.

# Testing mode: verbose prints a PASS or FAIL line per rule, score_only prints only the RESULT line.
FEEDBACK=verbose   # or: score_only

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
N0=http://127.0.0.1:8000; N1=http://127.0.0.1:8001; N2=http://127.0.0.1:8002
pass=0; fail=0

ok()  { [ "$FEEDBACK" = verbose ] && echo "PASS $1 $2"; pass=$((pass+1)); return 0; }
bad() { [ "$FEEDBACK" = verbose ] && echo "FAIL $1 $2"; fail=$((fail+1)); return 0; }

healthy() {
  for u in $N0 $N1 $N2; do curl -fs --max-time 2 "$u/api/health" >/dev/null 2>&1 || return 1; done
}
# AIDEV-NOTE: total wait is capped at 12s so a whole check fits the agent's 30s command limit.
boot() {
  if ! healthy; then
    (cd /app && setsid nohup /app/start.sh >/tmp/start.log 2>&1 </dev/null &) 2>/dev/null
    for _ in $(seq 1 12); do healthy && break; sleep 1; done
  fi
  healthy || { [ "$FEEDBACK" = verbose ] && echo "FAIL 0 cluster is not answering /api/health on 8000, 8001, 8002 (see /tmp/start.log)"; echo "RESULT: 0 passed, 1 failed"; exit 1; }
}
# feed: reads "ok|bad <rule> <detail>" lines from a python scenario and counts them
feed() {
  local st id rest
  while read -r st id rest; do
    case "$st" in ok) ok "$id" "$rest" ;; bad) bad "$id" "$rest" ;; esac
  done
}
# scenario <file.py> [rules...]: runs a python scenario; rules it never reported count as failed
scenario() {
  local f=$1; shift
  local out; out=$(timeout 24 python3 -I "$HERE/$f" 2>&1)
  local seen=""
  feed < <(printf '%s\n' "$out" | grep -E '^(ok|bad) ')
  for r in "$@"; do
    printf '%s\n' "$out" | grep -qE "^(ok|bad) $r( |$)" || bad "$r" "no result (the check stopped before this rule): $(printf '%s\n' "$out" | tail -n 2 | tr '\n' ' ' | head -c 200)"
  done
}
finish() { echo "RESULT: $pass passed, $fail failed"; [ "$fail" -eq 0 ]; }
