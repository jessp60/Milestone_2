#!/usr/bin/env bash
# Shared helpers for the component C checks. Sourced by check_c_*.sh, not run directly.

# Testing mode: verbose prints a PASS or FAIL line per rule, score_only prints only the RESULT line.
FEEDBACK=verbose   # or: score_only

N0=http://127.0.0.1:8000; N1=http://127.0.0.1:8001; N2=http://127.0.0.1:8002
BODY=/tmp/check_c_body.json
pass=0; fail=0
# AIDEV-NOTE: a check must print its RESULT line within the agent's 30s command limit even if the server hangs,
# so after 22s every request is skipped (status 000) and the remaining rules fail fast.
T0=$SECONDS
late() { [ $((SECONDS - T0)) -ge 22 ]; }
PW="correct-horse-1"
RUN="$RANDOM$RANDOM"

ok()  { [ "$FEEDBACK" = verbose ] && echo "PASS $1 $2"; pass=$((pass+1)); return 0; }
bad() { [ "$FEEDBACK" = verbose ] && echo "FAIL $1 $2"; fail=$((fail+1)); return 0; }
# expect <rule> <description> <expected-status> <actual-status>
expect() { if [ "$4" = "$3" ]; then ok "$1" "$2"; else bad "$1" "$2: expected $3, got $4"; fi; }
# pass_if <rule> <description> <detail-if-failed> <command...>: PASS when the command succeeds
pass_if() { local id=$1 d=$2 det=$3; shift 3; if "$@"; then ok "$id" "$d"; else bad "$id" "$d: $det"; fi; }
# fail_all <rules...>: marks every listed rule failed (used when setup did not work)
fail_all() { local r; for r in "$@"; do bad "$r" "setup failed, rule not run"; done; }

# call <METHOD> <URL> [json-body] [token]  -> prints the HTTP status, saves the body in $BODY
call() {
  rm -f "$BODY"
  if late; then echo 000; return; fi
  local args=(-s -o "$BODY" -w '%{http_code}' --max-time 4 -X "$1" "$2")
  [ -n "${3:-}" ] && args+=(-H 'Content-Type: application/json' -d "$3")
  [ -n "${4:-}" ] && args+=(-H "Authorization: Bearer $4")
  curl "${args[@]}" 2>/dev/null || true   # status 000 means no answer within 4s
}
# field <dotted.path>  -> prints that field of the last response body, or nothing
field() {
  python3 -c 'import json,sys
try:
    v=json.load(open(sys.argv[1]))
    for k in sys.argv[2].split("."): v=v[int(k)] if isinstance(v,list) else v[k]
    print(v)
except Exception: pass' "$BODY" "$1"
}
# pj <python-expression>  -> evaluates the expression with d = the last response body, prints the result
pj() {
  python3 -c 'import json,re,sys
try:
    d=json.load(open(sys.argv[1])); print(eval(sys.argv[2]))
except Exception: pass' "$BODY" "$1"
}
qs() { python3 -c 'import urllib.parse,sys;print(urllib.parse.quote(sys.argv[1],safe=""))' "$1"; }

healthy() {
  for u in $N0 $N1 $N2; do curl -fs --max-time 2 "$u/api/health" >/dev/null 2>&1 || return 1; done
}
# AIDEV-NOTE: total wait is capped at 15s so a whole check fits the agent's 30s command limit.
boot() {
  if ! healthy; then
    (cd /app && setsid nohup /app/start.sh >/tmp/start.log 2>&1 </dev/null &) 2>/dev/null
    for _ in $(seq 1 15); do healthy && break; sleep 1; done
  fi
  healthy || { [ "$FEEDBACK" = verbose ] && echo "FAIL 0 cluster is not answering /api/health on 8000, 8001, 8002 (see /tmp/start.log)"; echo "RESULT: 0 passed, 1 failed"; exit 1; }
}

# mkuser <name>: registers a fresh user, sets <name>_TOKEN, <name>_ID. Returns 1 if it did not work.
mkuser() {
  local u="u${RUN}$1"
  call POST $N0/api/auth/register "{\"username\":\"$u\",\"password\":\"$PW\"}" >/dev/null
  printf -v "$1_TOKEN" '%s' "$(field token)"
  printf -v "$1_ID" '%s' "$(field user.id)"
  printf -v "$1_NAME" '%s' "$u"
  local tv="$1_TOKEN"; [ -n "${!tv}" ]
}
# mkws <token>: creates a workspace, sets SLUG and GID (its #general channel id)
mkws() {
  SLUG="w${RUN}$RANDOM"
  call POST $N0/api/workspaces "{\"slug\":\"$SLUG\",\"name\":\"C Workspace\"}" "$1" >/dev/null
  GID=$(field general_channel.id); [ -n "$GID" ]
}
# mkchan <token> <name> [extra-json-fields]: creates a channel in $SLUG, sets CID
mkchan() {
  local s; s=$(call POST $N0/api/workspaces/$SLUG/channels "{\"name\":\"$2\"${3:-}}" "$1")
  CID=$(field channel.id); [ "$s" = 201 ] && [ -n "$CID" ]
}
join() { call POST $N0/api/channels/${1:-0}/join '{}' "${2:-}"; }
# post <channel-id> <body-text> <token> [node-url]  -> prints the status; the response is left in $BODY
# (read the new id with field message.id after the call; do not rely on variables set inside $(...))
post() {
  call POST ${4:-$N0}/api/channels/$1/messages "{\"body\":$(python3 -c 'import json,sys;print(json.dumps(sys.argv[1]))' "$2")}" "$3"
}
# setup_pair: sets up owner and m1 in one workspace, both in #general. Returns 1 if anything failed.
setup_pair() {
  mkuser owner && mkuser m1 && mkws "$owner_TOKEN" || return 1
  [ "$(join $GID "$m1_TOKEN")" = 200 ]
}

# wsrun <seconds> <token> <node-url> <actions-python>: opens a WebSocket on <node-url>, runs the
# python snippet in <actions-python> (it may call act(method, path, body, token) to send HTTP
# requests), collects every frame for <seconds>, and writes them as a JSON list to /tmp/check_c_ws.json.
wsrun() {
  if late; then echo "[]" > /tmp/check_c_ws.json; return; fi
  python3 - "$@" <<'PY'
import base64, json, os, socket, struct, sys, threading, time, urllib.request, urllib.error
secs, tok, node, actions = float(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4]
host, port = node.replace("http://", "").split(":"); port = int(port)
frames = []
def act(method, path, body=None, token=None):
    req = urllib.request.Request(node + path, method=method, data=(json.dumps(body).encode() if body is not None else None))
    req.add_header("Content-Type", "application/json")
    if token: req.add_header("Authorization", "Bearer " + token)
    try:
        r = urllib.request.urlopen(req, timeout=4); return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        try: return e.code, json.loads(e.read() or b"null")
        except Exception: return e.code, None
    except Exception: return 0, None
def recv_loop(s):
    buf = b""
    while True:
        try: d = s.recv(65536)
        except Exception: return
        if not d: return
        buf += d
        while len(buf) >= 2:
            op = buf[0] & 0x0f; ln = buf[1] & 0x7f; i = 2
            if ln == 126:
                if len(buf) < 4: break
                ln = struct.unpack(">H", buf[2:4])[0]; i = 4
            elif ln == 127:
                if len(buf) < 10: break
                ln = struct.unpack(">Q", buf[2:10])[0]; i = 10
            if len(buf) < i + ln: break
            payload, buf = buf[i:i+ln], buf[i+ln:]
            if op == 1:
                try: frames.append(json.loads(payload.decode()))
                except Exception: pass
try:
    s = socket.create_connection((host, port), timeout=3)
    key = base64.b64encode(os.urandom(16)).decode()
    s.sendall(("GET /api/ws?token=%s HTTP/1.1\r\nHost: %s:%d\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n" % (tok, host, port, key)).encode())
    head = b""
    while b"\r\n\r\n" not in head:
        d = s.recv(4096)
        if not d: break
        head += d
    rest = head.split(b"\r\n\r\n", 1)[1] if b"\r\n\r\n" in head else b""
    s.settimeout(0.5)
    threading.Thread(target=recv_loop, args=(s,), daemon=True).start()
    time.sleep(0.4)
    exec(actions)
    time.sleep(secs)
except Exception as e:
    frames.append({"type": "_error", "error": str(e)})
json.dump(frames, open("/tmp/check_c_ws.json", "w"))
PY
}
# wsj <python-expression>: evaluates the expression with f = the list of frames from the last wsrun
wsj() {
  python3 -c 'import json,sys
try:
    f=json.load(open("/tmp/check_c_ws.json")); print(eval(sys.argv[1]))
except Exception: pass' "$1"
}
# upload <token> <file-path> [filename] [content-type] [node-url]: multipart POST /api/files; prints the status and saves the body (read the new id with field file.id)
upload() {
  rm -f "$BODY"
  if late; then echo 000; return; fi
  local args=(-s -o "$BODY" -w '%{http_code}' --max-time 8 -X POST "${5:-$N0}/api/files" -F "file=@$2;filename=${3:-$(basename "$2")};type=${4:-application/octet-stream}")
  [ -n "${1:-}" ] && args+=(-H "Authorization: Bearer $1")
  curl "${args[@]}" 2>/dev/null || true
}
finish() { echo "RESULT: $pass passed, $fail failed"; [ "$fail" -eq 0 ]; }
