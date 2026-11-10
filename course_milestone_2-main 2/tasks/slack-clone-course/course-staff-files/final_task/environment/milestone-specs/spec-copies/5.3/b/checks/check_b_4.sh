#!/usr/bin/env bash
# Self-check for stage B.4 (channel events, read state and API conventions). Prints PASS/FAIL per rule in /spec/b/b-4.md.
# Usage: bash /spec/b/checks/check_b_4.sh      Exit 0 only if every rule passes.
set -u
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
N0=http://127.0.0.1:8000; N1=http://127.0.0.1:8001; N2=http://127.0.0.1:8002
BODY=/tmp/check_b_body.json
pass=0; fail=0
# AIDEV-NOTE: the agent's shell kills any command after 30s and shows no output. After BUDGET seconds,
# call() stops sending requests so the script still finishes and prints its summary.
START=$SECONDS; BUDGET=18

ok()  { [ "$FEEDBACK" = verbose ] && echo "PASS $1 $2"; pass=$((pass+1)); }
bad() { [ "$FEEDBACK" = verbose ] && echo "FAIL $1 $2"; fail=$((fail+1)); }
# pass_if <id> <description> <detail-if-failed> <command...>: PASS when the command succeeds
pass_if() { local id=$1 d=$2 det=$3; shift 3; if "$@"; then ok "$id" "$d"; else bad "$id" "$d: $det"; fi; }
setup() { [ "$3" = "$2" ] || bad SETUP "$1: expected $2, got $3"; }

# call <METHOD> <URL> [json-body] [token]  -> prints the HTTP status, saves the body in $BODY
call() {
  rm -f "$BODY"
  if [ $((SECONDS-START)) -ge $BUDGET ]; then echo 000; return; fi
  local args=(-s -o "$BODY" -w '%{http_code}' --max-time 3 -X "$1" "$2")
  [ -n "${3:-}" ] && args+=(-H 'Content-Type: application/json' -d "$3")
  [ -n "${4:-}" ] && args+=(-H "Authorization: Bearer $4")
  curl "${args[@]}" 2>/dev/null || true   # status 000 means no answer within 3s
}
# field <dotted.path>  -> prints that field of the last response body, or nothing
field() {
  python3 -c 'import json,sys
try:
    v=json.load(open(sys.argv[1]))
    for k in sys.argv[2].split("."): v=v[k]
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

# AIDEV-NOTE: total wait is capped at 12s so the whole script fits the agent's 30s command limit.
if ! healthy; then
  (cd /app && nohup /app/start.sh >/tmp/start.log 2>&1 &)
  for _ in $(seq 1 8); do healthy && break; sleep 1; done
fi
healthy || { echo "FAIL 0 cluster is not answering /api/health on 8000, 8001, 8002 (see /tmp/start.log)"; echo "RESULT: 0 passed, 1 failed"; exit 1; }
START=$SECONDS

RUN="$RANDOM$RANDOM"; PW="correct-horse-1"
CHF='["id","workspace_id","name","is_private","is_dm","topic","is_archived"]'
UF='["id","username","display_name","timezone","avatar_url","status_text","status_emoji"]'
MF='["id","channel_id","author_id","author","body","parent_id","reply_count","created_at","edited_at","files","reactions","mentions"]'

# mkuser <name>: registers a fresh user and sets <name>_TOKEN, <name>_ID, <name>_NAME
mkuser() {
  local u="u${RUN}$1" tv="$1_TOKEN"
  call POST $N0/api/auth/register "{\"username\":\"$u\",\"password\":\"$PW\"}" >/dev/null
  printf -v "$1_TOKEN" '%s' "$(field token)"
  printf -v "$1_ID" '%s' "$(field user.id)"
  printf -v "$1_NAME" '%s' "$u"
  [ -n "${!tv}" ] || bad SETUP "could not register user $1 (no token returned)"
}
# mkchan <token> <name> [extra-json-fields] [slug]: creates a channel and sets CID
mkchan() {
  local s; s=$(call POST $N0/api/workspaces/${4:-$SLUG}/channels "{\"name\":\"$2\"${3:-}}" "$1")
  CID=$(field channel.id)
  [ "$s" = 201 ] && [ -n "$CID" ] || bad SETUP "create channel $2: expected 201 with channel.id, got status $s"
}
# post <channel-id> <body-text> <token> [parent-id] [node-url]  -> prints the status
post() {
  local b="{\"body\":\"${2:-}\"${4:+,\"parent_id\":$4}}"
  call POST ${5:-$N0}/api/channels/${1:-0}/messages "$b" "${3:-}"
}
join() { call POST $N0/api/channels/${1:-0}/join '{}' "${2:-}"; }

########## SETUP: users and workspaces (uses Component A routes) ##########
for n in owner m1 m2 out1; do mkuser $n; done
SLUG="bw$RUN"; SLUG2="bx$RUN"
s=$(call POST $N0/api/workspaces "{\"slug\":\"$SLUG\",\"name\":\"B Workspace\"}" "$owner_TOKEN"); setup "create workspace" 201 "$s"
WID=$(field workspace.id); GID=$(field general_channel.id)
s=$(call POST $N0/api/workspaces "{\"slug\":\"$SLUG2\",\"name\":\"B Second\"}" "$owner_TOKEN"); setup "create second workspace" 201 "$s"
WID2=$(field workspace.id)
for n in m1 m2; do tv="${n}_TOKEN"; s=$(join $GID "${!tv}"); setup "$n joins the general channel" 200 "$s"; done

########## WEBSOCKET EVENT CHECKS: started now, run in the background while the HTTP rules run ##########
# The WebSocket checks need a small Python client; it is written to /tmp and run with a time limit.
cat > /tmp/check_b_ws.py <<'PY'
import base64, json, os, socket, struct, sys, time, urllib.error, urllib.parse, urllib.request
H = "127.0.0.1"
def http(method, path, body=None, tok=None, node=0):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request("http://%s:%d%s" % (H, 8000 + node, path), data=data, method=method)
    if data is not None: req.add_header("Content-Type", "application/json")
    if tok: req.add_header("Authorization", "Bearer " + tok)
    try:
        with urllib.request.urlopen(req, timeout=3) as r: return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e: return e.code, None
    except Exception: return 0, None
class WS:
    def __init__(self, node, tok):
        self.ok = False; self.buf = b""; self.closed = False
        try:
            s = socket.create_connection((H, 8000 + node), timeout=3)
            key = base64.b64encode(os.urandom(16)).decode()
            s.sendall(("GET /api/ws?token=%s HTTP/1.1\r\nHost: %s:%d\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                       "Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n" % (urllib.parse.quote(tok), H, 8000 + node, key)).encode())
            data = b""
            while b"\r\n\r\n" not in data:
                c = s.recv(4096)
                if not c: break
                data += c
            head, _, self.buf = data.partition(b"\r\n\r\n")
            parts = head.split(b"\r\n", 1)[0].decode("latin1").split()
            self.ok = len(parts) > 1 and parts[1] == "101"
            self.s = s
        except Exception:
            self.s = None
    def send(self, obj):
        d = json.dumps(obj).encode(); m = os.urandom(4); n = len(d)
        h = bytearray([0x81])
        if n < 126: h.append(0x80 | n)
        else: h.append(0x80 | 126); h += struct.pack(">H", n)
        self.s.sendall(bytes(h) + m + bytes(c ^ m[i % 4] for i, c in enumerate(d)))
    def _fill(self, n, end):
        while len(self.buf) < n:
            rem = end - time.time()
            if rem <= 0: return False
            self.s.settimeout(rem)
            try: c = self.s.recv(65536)
            except socket.timeout: return False
            except OSError: self.closed = True; return False
            if not c: self.closed = True; return False
            self.buf += c
        return True
    def recv(self, timeout):
        end = time.time() + timeout
        while True:
            if not self._fill(2, end): return None
            b0, b1 = self.buf[0], self.buf[1]; ln = b1 & 0x7f; off = 2
            if ln == 126:
                if not self._fill(4, end): return None
                ln = struct.unpack(">H", self.buf[2:4])[0]; off = 4
            elif ln == 127:
                if not self._fill(10, end): return None
                ln = struct.unpack(">Q", self.buf[2:10])[0]; off = 10
            if not self._fill(off + ln, end): return None
            p = self.buf[off:off + ln]; self.buf = self.buf[off + ln:]
            op = b0 & 0x0f
            if op == 8: self.closed = True; return None
            if op == 1:
                try: return json.loads(p)
                except Exception: continue
    def close(self):
        try: self.s.close()
        except Exception: pass
def wait(ws, pred, log, t=1.5):
    end = time.time() + t
    while time.time() < end and not ws.closed:
        f = ws.recv(end - time.time())
        if f is None: continue
        log.append(f)
        if pred(f): return f
    return None
def sub(ch, tok, node=0):
    ws = WS(node, tok); log = []
    if not ws.ok: return ws, None, log
    ws.send({"type": "subscribe", "channel_id": ch})
    f = wait(ws, lambda f: f.get("type") == "subscribed" and f.get("channel_id") == ch, log, 2)
    return ws, f, log
def out(ok, rid, desc, detail=""):
    print(("PASS %s %s" % (rid, desc)) if ok else ("FAIL %s %s: %s" % (rid, desc, detail)), flush=True)
isint = lambda x: isinstance(x, int) and not isinstance(x, bool)

mode = sys.argv[1]
if mode == "bulk":
    n = 0
    for i in range(int(sys.argv[4])):
        if http("POST", "/api/channels/%s/messages" % sys.argv[2], {"body": "big-%d" % i}, sys.argv[3])[0] == 201: n += 1
    print(n); sys.exit(0)
if mode == "head":
    ws, f, _ = sub(int(sys.argv[2]), sys.argv[3]); ws.close()
    print(f.get("head_event_id") if f and isint(f.get("head_event_id")) else "")
    sys.exit(0)

otok, mtok, ch, cl, rs = sys.argv[2], sys.argv[3], int(sys.argv[4]), int(sys.argv[5]), int(sys.argv[6])
RULES = [("B4.1", "subscribe is answered with a subscribed frame carrying head_event_id"),
         ("B4.2", "a new message is broadcast as message.created with event_id and channel_id"),
         ("B4.3", "a thread reply is broadcast as message.reply"),
         ("B4.4", "joining a channel is broadcast as member.joined"),
         ("B4.5", "leaving a channel is broadcast as member.left"),
         ("B4.6", "changing the topic is broadcast as channel.updated"),
         ("B4.7", "archiving and unarchiving are broadcast as channel.archived and channel.unarchived"),
         ("B4.8", "event_ids are dense: first is head_event_id + 1, then exactly +1 each, no duplicates"),
         ("B4.9", "posts through nodes 0, 1 and 2 reach subscribers on every node with the same event_ids"),
         ("B4.10", "resume on another node replays the missed events in order, then sends resumed with head_event_id"),
         ("B4.21", "editing a message is broadcast as message.updated"),
         ("B4.22", "deleting a message is broadcast as message.deleted"),
         ("B4.23", "adding a reaction is broadcast as reaction.added"),
         ("B4.24", "removing a reaction is broadcast as reaction.removed")]
def skip_rest(first, why):
    ids = [r for r, _ in RULES]
    for rid, d in RULES:
        if ids.index(rid) >= ids.index(first): out(False, rid, d, why)
seen = []
ws, f, seen = sub(ch, otok)
head = f.get("head_event_id") if f else None
out(bool(f) and isint(head), "B4.1", RULES[0][1], "no subscribed frame within 2s (websocket connected: %s)" % ws.ok if not f else "head_event_id is %r" % head)
if not (f and isint(head)):
    skip_rest("B4.2", "needs the subscribed frame (B4.1)")
else:
    def expect(rid, kind, resp, desc):
        if resp[0] not in (200, 201):
            out(False, rid, desc, "the request itself returned status %s" % resp[0]); return None
        fr = wait(ws, lambda x: x.get("type") == kind and x.get("channel_id") == ch, seen)
        out(bool(fr) and isint(fr.get("event_id")), rid, desc, "no %s frame with an integer event_id within 1.5s" % kind)
        return fr
    st, j = http("POST", "/api/channels/%d/messages" % ch, {"body": "ev one"}, otok)
    mid = (j or {}).get("message", {}).get("id")
    expect("B4.2", "message.created", (st,), RULES[1][1])
    st, j = http("POST", "/api/channels/%d/messages" % ch, {"body": "ev reply", "parent_id": mid}, otok)
    expect("B4.3", "message.reply", (st,), RULES[2][1])
    expect("B4.21", "message.updated", (http("PATCH", "/api/messages/%s" % mid, {"body": "ev edited"}, otok)[0],), RULES[10][1])
    thumb = "\U0001F44D"
    expect("B4.23", "reaction.added", (http("POST", "/api/messages/%s/reactions" % mid, {"emoji": thumb}, otok)[0],), RULES[12][1])
    expect("B4.24", "reaction.removed", (http("DELETE", "/api/messages/%s/reactions/%s" % (mid, urllib.parse.quote(thumb)), None, otok)[0],), RULES[13][1])
    expect("B4.22", "message.deleted", (http("DELETE", "/api/messages/%s" % mid, None, otok)[0],), RULES[11][1])
    expect("B4.4", "member.joined", (http("POST", "/api/channels/%d/join" % ch, {}, mtok)[0],), RULES[3][1])
    expect("B4.5", "member.left", (http("POST", "/api/channels/%d/leave" % ch, {}, mtok)[0],), RULES[4][1])
    expect("B4.6", "channel.updated", (http("PATCH", "/api/channels/%d" % ch, {"topic": "ev topic"}, otok)[0],), RULES[5][1])
    a = http("POST", "/api/channels/%d/archive" % ch, {}, otok)[0]
    fa = wait(ws, lambda x: x.get("type") == "channel.archived", seen) if a == 200 else None
    u = http("POST", "/api/channels/%d/unarchive" % ch, {}, otok)[0]
    fu = wait(ws, lambda x: x.get("type") == "channel.unarchived", seen) if u == 200 else None
    out(bool(fa) and bool(fu), "B4.7", RULES[6][1], "archive returned %s and %s frame, unarchive returned %s and %s frame" % (a, "a" if fa else "no", u, "a" if fu else "no"))
    ids = [x["event_id"] for x in seen if isint(x.get("event_id")) and x.get("channel_id") == ch]
    dense = bool(ids) and ids[0] == head + 1 and all(ids[i + 1] == ids[i] + 1 for i in range(len(ids) - 1))
    out(dense, "B4.8", RULES[7][1], "head_event_id %s, event_ids received %s" % (head, ids))
ws.close()

# cluster: one subscriber per node, posts through every node
subs = [sub(cl, otok, n) for n in range(3)]
if not all(s[1] for s in subs):
    out(False, "B4.9", RULES[8][1], "subscribe failed on node(s) %s" % [n for n, s in enumerate(subs) if not s[1]])
else:
    h0 = subs[0][1].get("head_event_id")
    for n in range(3): http("POST", "/api/channels/%d/messages" % cl, {"body": "cl-%d" % n}, otok, n)
    got = []
    for w, _, lg in subs:
        ev = []
        for _ in range(3):
            fr = wait(w, lambda x: x.get("type") == "message.created" and x.get("channel_id") == cl, lg)
            if fr: ev.append(fr.get("event_id"))
        got.append(ev)
    good = len(got[0]) == 3 and got[0] == [h0 + 1, h0 + 2, h0 + 3] and got[1] == got[0] and got[2] == got[0]
    out(good, "B4.9", RULES[8][1], "head %s, event_ids seen on nodes 0/1/2: %s" % (h0, got))
for w, _, _ in subs: w.close()

# resume: events missed while disconnected are replayed on another node
w, f, lg = sub(rs, otok, 0)
if not f:
    out(False, "B4.10", RULES[9][1], "could not subscribe to the resume channel")
else:
    got = []
    for n in (1, 2, 0):
        http("POST", "/api/channels/%d/messages" % rs, {"body": "rs-%d" % n}, otok, n)
        fr = wait(w, lambda x: x.get("type") == "message.created" and x.get("channel_id") == rs, lg)
        got.append(fr.get("event_id") if fr else None)
    w.close()
    if None in got:
        out(False, "B4.10", RULES[9][1], "the first connection did not receive its 3 message.created events (got %s)" % got)
    else:
        w2 = WS(2, otok); l2 = []
        if not w2.ok:
            out(False, "B4.10", RULES[9][1], "could not connect to node 2")
        else:
            w2.send({"type": "resume", "channel_id": rs, "since_event_id": got[0]})
            r = wait(w2, lambda x: x.get("type") == "resumed" and x.get("channel_id") == rs, l2, 2)
            replay = [x.get("event_id") for x in l2 if isint(x.get("event_id")) and x.get("channel_id") == rs]
            good = bool(r) and replay == got[1:] and r.get("head_event_id") == got[2] and l2.index(r) == len(l2) - 1
            out(good, "B4.10", RULES[9][1], "since_event_id %s: replayed %s (expected %s), resumed frame %s" % (got[0], replay, got[1:], r))
        w2.close()
PY
# Channels for the event checks (events, cluster, resume). owner and m2 are workspace members.
mkchan "$owner_TOKEN" "c$RUN-ev"; CH_EV=$CID
mkchan "$owner_TOKEN" "c$RUN-cl"; CH_CL=$CID
mkchan "$owner_TOKEN" "c$RUN-rs"; CH_RS=$CID
WSOUT=/tmp/check_b_ws.out; rm -f "$WSOUT"
( timeout 14 python3 /tmp/check_b_ws.py events "$owner_TOKEN" "$m2_TOKEN" "$CH_EV" "$CH_CL" "$CH_RS" >"$WSOUT" 2>&1 ) &
WSPID=$!

########## API CONVENTIONS ##########
# AIDEV-NOTE: each route is "METHOD|PATH|BODY". sweep <expected-status> <token> <routes...> prints the routes that did not answer the expected status.
sweep() {
  local exp=$1 tok=$2 line m p b s out=""; shift 2
  for line in "$@"; do
    IFS='|' read -r m p b <<<"$line"
    s=$(call "$m" "$N0$p" "$b" "$tok")
    [ "$s" = "$exp" ] || out="$out $m $p -> $s;"
  done
  echo "$out"
}
mkchan "$owner_TOKEN" "c$RUN-cv"; CH_CV=$CID
s=$(post $CH_CV conv-1 "$owner_TOKEN"); MID_CV=$(field message.id)
setup "post a message for the convention rules" 201 "$s"
B_ROUTES=(
  "GET|/api/workspaces/$SLUG|"
  "POST|/api/workspaces/$SLUG/channels|{\"name\":\"zz-$RUN\"}"
  "PATCH|/api/channels/$CH_CV|{\"topic\":\"x\"}"
  "GET|/api/channels/$CH_CV/members|"
  "POST|/api/channels/$CH_CV/leave|{}"
  "POST|/api/channels/$CH_CV/archive|{}"
  "POST|/api/channels/$CH_CV/unarchive|{}"
  "POST|/api/channels/$CH_CV/messages|{\"body\":\"x\"}"
  "GET|/api/channels/$CH_CV/messages|"
  "GET|/api/messages/$MID_CV/replies|"
  "GET|/api/search?q=conv|"
  "GET|/api/channels/$CH_CV/pins|"
  "POST|/api/messages/$MID_CV/pin|{}"
  "DELETE|/api/messages/$MID_CV/pin|"
  "GET|/api/channels/$CH_CV/read|"
  "POST|/api/channels/$CH_CV/read|{\"last_read_event_id\":1}"
)
r=$(sweep 401 "" "${B_ROUTES[@]}")
pass_if B4.15 "every authenticated route returns 401 with no token" "routes that did not return 401:$r" [ -z "$r" ]
r=$(sweep 401 "not-a-real-token-000000" "${B_ROUTES[@]}")
pass_if B4.16 "every authenticated route returns 401 with an invalid token" "routes that did not return 401:$r" [ -z "$r" ]
r=$(sweep 404 "$owner_TOKEN" \
  "GET|/api/workspaces/nope$RUN|" \
  "POST|/api/workspaces/nope$RUN/channels|{\"name\":\"zz-$RUN\"}" \
  "PATCH|/api/channels/999999999|{\"topic\":\"x\"}" \
  "GET|/api/channels/999999999/members|" \
  "POST|/api/channels/999999999/leave|{}" \
  "POST|/api/channels/999999999/archive|{}" \
  "POST|/api/channels/999999999/unarchive|{}" \
  "GET|/api/channels/999999999/messages|" \
  "POST|/api/channels/999999999/messages|{\"body\":\"x\"}" \
  "GET|/api/messages/999999999/replies|" \
  "GET|/api/channels/999999999/pins|" \
  "POST|/api/messages/999999999/pin|{}" \
  "DELETE|/api/messages/999999999/pin|" \
  "GET|/api/channels/999999999/read|" \
  "POST|/api/channels/999999999/read|{\"last_read_event_id\":1}")
pass_if B4.17 "unknown ids return 404" "routes that did not return 404:$r" [ -z "$r" ]
r=$(sweep 400 "$owner_TOKEN" "POST|/api/channels/$CH_CV/messages|{}" "POST|/api/workspaces/$SLUG/channels|{}")
pass_if B4.18 "missing required fields return 400" "routes that did not return 400:$r" [ -z "$r" ]
r=$(sweep 400 "$owner_TOKEN" "POST|/api/channels/$CH_CV/messages|{not json" "POST|/api/workspaces/$SLUG/channels|{not json")
pass_if B4.19 "a malformed JSON body returns 400" "routes that did not return 400:$r" [ -z "$r" ]
mkchan "$owner_TOKEN" "c$RUN-xn"; s1=$(call POST $N1/api/workspaces/$SLUG/channels "{\"name\":\"c$RUN-xn2\"}" "$owner_TOKEN"); CH_XN=$(field channel.id)
s2=$(post "$CH_XN" cross-1 "$owner_TOKEN" "" "$N2"); MID_XN=$(field message.id)
a=$(call GET $N0/api/channels/$CH_XN/messages "" "$owner_TOKEN"); ma=$(pj '",".join(str(m["id"]) for m in d["messages"])')
b=$(call GET $N1/api/channels/$CH_XN/messages "" "$owner_TOKEN"); mb=$(pj '",".join(str(m["id"]) for m in d["messages"])')
pass_if B4.20 "a channel made on node 1 and a message posted on node 2 are visible on nodes 0 and 1 with the same ids" "channel create got $s1, post on node 2 got $s2, node 0 list got $a '$ma', node 1 list got $b '$mb' (expected 200 and '$MID_XN' on both)" \
  [ "$s1" = 201 -a "$s2" = 201 -a "$a" = 200 -a "$b" = 200 -a "$ma" = "$MID_XN" -a "$mb" = "$MID_XN" ]

########## WEBSOCKET RESULTS ##########
wait $WSPID 2>/dev/null
EVOUT=$(cat "$WSOUT" 2>/dev/null)
echo "$EVOUT" | grep -E '^(PASS|FAIL) '
pass=$((pass + $(echo "$EVOUT" | grep -c '^PASS ')))
fail=$((fail + $(echo "$EVOUT" | grep -c '^FAIL ')))
# AIDEV-NOTE: a rule that printed nothing (script crashed or hit its time limit) must count as a failure, never be skipped.
for r in B4.1 B4.2 B4.3 B4.4 B4.5 B4.6 B4.7 B4.8 B4.9 B4.10 B4.21 B4.22 B4.23 B4.24; do
  echo "$EVOUT" | grep -qE "^(PASS|FAIL) $r " || bad $r "no result: the websocket check stopped before this rule (last output: $(echo "$EVOUT" | tail -1 | cut -c1-150))"
done
BUDGET=$((SECONDS-START+8))   # keep the read-state requests below from being cut off by the time limit

########## READ STATE ##########
R="$N0/api/channels/$CH_EV/read"
HEAD=$(timeout 5 python3 /tmp/check_b_ws.py head "$CH_EV" "$owner_TOKEN")
s=$(call GET $R "" "$owner_TOKEN")
f=$(pj "all(k in d['read_state'] for k in ['channel_id','last_read_event_id','unread_count','mention_count'])")
pass_if B4.11 "GET read returns read_state with channel_id, last_read_event_id, unread_count, mention_count" "got status $s, fields present '$f'" [ "$s" = 200 -a "$f" = True ]
if [[ "$HEAD" =~ ^[0-9]+$ ]] && [ "$HEAD" -gt 1 ]; then
  s=$(call POST $R "{\"last_read_event_id\":$HEAD}" "$owner_TOKEN")
  pass_if B4.12 "POST read moves the cursor forward" "set $HEAD, got status $s, last_read_event_id '$(field read_state.last_read_event_id)'" [ "$s" = 200 -a "$(field read_state.last_read_event_id)" = "$HEAD" ]
  s=$(call POST $R "{\"last_read_event_id\":$((HEAD-1))}" "$owner_TOKEN")
  pass_if B4.13 "POST read never moves the cursor backwards" "set $((HEAD-1)) after $HEAD, got status $s, last_read_event_id '$(field read_state.last_read_event_id)' (expected $HEAD)" [ "$s" = 200 -a "$(field read_state.last_read_event_id)" = "$HEAD" ]
else
  bad B4.12 "POST read moves the cursor forward: no head_event_id available (subscribe to the channel failed, see B4.1)"
  bad B4.13 "POST read never moves the cursor backwards: no head_event_id available (see B4.1)"
fi
call GET $N0/api/workspaces/$SLUG "" "$owner_TOKEN" >/dev/null
pass_if B4.14 "workspace detail read_state includes this channel's state" "read_state in workspace detail: $(pj 'json.dumps(d.get("read_state"))' | cut -c1-200)" \
  [ "$(pj "'$CH_EV' in json.dumps(d['read_state'])")" = True ]

if [ $((SECONDS-START)) -ge $BUDGET ]; then
  echo "NOTE: the time limit was reached; later checks may show 'got 000'. Some route is slow or hangs (no answer within 3 seconds): fix the first 'got 000' first."
fi
echo "RESULT: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
