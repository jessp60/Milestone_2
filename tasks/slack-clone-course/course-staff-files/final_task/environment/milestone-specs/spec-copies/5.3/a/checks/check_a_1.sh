#!/usr/bin/env bash
# Self-check for stage A.1 (auth, workspaces, channels, open join). Prints PASS/FAIL per rule in /spec/a/a-1.md.
# Usage: bash /spec/a/checks/check_a_1.sh      Exit 0 only if every rule passes.
set -u
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
N0=http://127.0.0.1:8000; N1=http://127.0.0.1:8001; N2=http://127.0.0.1:8002
BODY=/tmp/check_a_body.json
pass=0; fail=0
# AIDEV-NOTE: the agent's shell kills any command after 30s and shows no output. After BUDGET
# seconds, call() stops sending requests so the script still finishes and prints its summary.
START=$SECONDS; BUDGET=20

ok()  { [ "$FEEDBACK" = verbose ] && echo "PASS $1 $2"; pass=$((pass+1)); }
bad() { [ "$FEEDBACK" = verbose ] && echo "FAIL $1 $2"; fail=$((fail+1)); }
# expect <rule> <description> <expected-status> <actual-status>
expect() { if [ "$4" = "$3" ]; then ok "$1" "$2"; else bad "$1" "$2: expected $3, got $4"; fi; }
# setup <description> <expected-status> <actual-status>: a setup step the rules depend on
setup() { [ "$3" = "$2" ] || bad SETUP "$1: expected $2, got $3"; }
# pass_if <id> <description> <detail-if-failed> <command...>: PASS when the command succeeds
pass_if() { local id=$1 d=$2 det=$3; shift 3; if "$@"; then ok "$id" "$d"; else bad "$id" "$d: $det"; fi; }
is2xx() { case "$1" in 2??) return 0;; *) return 1;; esac; }

# call <METHOD> <URL> [json-body] [token]  -> prints the HTTP status, saves the body in $BODY
call() {
  rm -f "$BODY"   # so a request that gets no answer never shows the previous response
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
# member_role <user_id>  -> role of that user in the last members response, or nothing
member_role() {
  python3 -c 'import json,sys
try:
    for m in json.load(open(sys.argv[1]))["members"]:
        if str(m["user_id"])==sys.argv[2]: print(m["role"])
except Exception: pass' "$BODY" "$1"
}
# jid <id> -> prints the id as a JSON value (bare number, or quoted if it is not numeric)
jid() { case "$1" in ''|*[!0-9]*) printf '"%s"' "$1";; *) printf '%s' "$1";; esac; }
healthy() {
  for u in $N0 $N1 $N2; do curl -fs --max-time 2 "$u/api/health" >/dev/null 2>&1 || return 1; done
}

# AIDEV-NOTE: total wait is capped at 15s so the whole script fits the agent's 30s command limit.
if ! healthy; then
  (cd /app && nohup /app/start.sh >/tmp/start.log 2>&1 &)
  for _ in $(seq 1 15); do healthy && break; sleep 1; done
fi
healthy || { echo "FAIL 0 cluster is not answering /api/health on 8000, 8001, 8002 (see /tmp/start.log)"; echo "RESULT: 0 passed, 1 failed"; exit 1; }

RUN="$RANDOM$RANDOM"; PW="correct-horse-1"

# mkuser <name>: registers a fresh user and sets <name>_TOKEN, <name>_ID
mkuser() {
  local u="u${RUN}$1" tv="$1_TOKEN"
  call POST $N0/api/auth/register "{\"username\":\"$u\",\"password\":\"$PW\"}" >/dev/null
  printf -v "$1_TOKEN" '%s' "$(field token)"
  printf -v "$1_ID" '%s' "$(field user.id)"
  [ -n "${!tv}" ] || bad SETUP "could not register user $1 (no token returned)"
}

########## AUTH (rules 1-9) ##########
AU="u${RUN}a"

s=$(call POST $N0/api/auth/register "{\"username\":\"$AU\",\"password\":\"$PW\"}")
TOKEN=$(field token); UNAME=$(field user.username)
if [ "$s" = 201 ] && [ "${#TOKEN}" -ge 16 ] && [ "$UNAME" = "$AU" ]; then
  ok A1.1 "register returns 201 with user and token"
else
  bad A1.1 "register: expected 201 with user.username and a token of 16+ chars, got status $s, token length ${#TOKEN}"
fi

s=$(call POST $N0/api/auth/register "{\"username\":\"$AU\",\"password\":\"$PW\"}")
expect A1.2 "duplicate username" 409 "$s"

s=$(call POST $N0/api/auth/register "{\"username\":\"bad name $RANDOM\",\"password\":\"$PW\"}")
expect A1.3 "username with a space" 400 "$s"

s=$(call POST $N0/api/auth/register "{\"username\":\"${AU}b\",\"password\":\"short77\"}")
expect A1.5 "7 character password" 400 "$s"

s=$(call POST $N0/api/auth/login "{\"username\":\"$AU\",\"password\":\"$PW\"}")
LTOKEN=$(field token)
if [ "$s" = 200 ] && [ -n "$LTOKEN" ]; then ok A1.6 "login returns 200 with user and token"
else bad A1.6 "login: expected 200 with a token, got status $s"; fi

s=$(call POST $N0/api/auth/login "{\"username\":\"$AU\",\"password\":\"WRONG12345\"}")
expect A1.7 "login with wrong password" 401 "$s"

s=$(call GET $N0/api/auth/me "" "$TOKEN")
if [ "$s" = 200 ] && [ "$(field user.username)" = "$AU" ]; then ok A1.8 "me returns 200 and the same username"
else bad A1.8 "me with token: expected 200 and username $AU, got status $s"; fi

s1=$(call GET $N0/api/auth/me); s2=$(call GET $N0/api/auth/me "" "not-a-real-token-000000")
if [ "$s1" = 401 ] && [ "$s2" = 401 ]; then ok A1.9 "me without a valid token returns 401"
else bad A1.9 "me: expected 401 with no token and with a made-up token, got $s1 and $s2"; fi

a=$(call POST $N2/api/auth/login "{\"username\":\"$AU\",\"password\":\"$PW\"}")
b=$(call GET $N1/api/auth/me "" "$TOKEN"); c=$(call GET $N2/api/auth/me "" "$TOKEN")
if [ "$a" = 200 ] && [ "$b" = 200 ] && [ "$c" = 200 ]; then ok A1.10 "account and token work on all three nodes"
else bad A1.10 "cross-node: login on 8002 got $a, me on 8001 got $b, me on 8002 got $c (expected 200 each)"; fi


s=$(call POST $N0/api/auth/register "{\"username\":\"u${RUN}-x!\",\"password\":\"$PW\"}")
expect A1.4 "register with special characters in the username" 400 "$s"

for n in owner member1 out1; do mkuser $n; done

########## WORKSPACES (rules 10-21) ##########
SLUG="w$RUN"
s=$(call POST $N0/api/workspaces "{\"slug\":\"$SLUG\",\"name\":\"Check Workspace\"}" "$owner_TOKEN")
WID=$(field workspace.id); WSLUG=$(field workspace.slug); WNAME=$(field workspace.name)
WOWNER=$(field workspace.owner_id); WMODE=$(field workspace.join_mode)
GCH=$(field general_channel.id); GNAME=$(field general_channel.name)
if [ "$s" = 201 ] && [ -n "$WID" ] && [ "$WSLUG" = "$SLUG" ] && [ -n "$WNAME" ] && [ -n "$WOWNER" ] && [ -n "$WMODE" ] && [ -n "$GCH" ]; then
  ok A1.11 "create workspace returns 201 with workspace and general_channel"
else
  bad A1.11 "create workspace: expected 201 with workspace {id, slug, name, owner_id, join_mode} and general_channel, got status $s (id='$WID' slug='$WSLUG' owner_id='$WOWNER' join_mode='$WMODE' general_channel.id='$GCH')"
fi
if [ "$WMODE" = open ]; then ok A1.12 "join_mode defaults to open"; else bad A1.12 "join_mode: expected open, got '$WMODE'"; fi
if [ -n "$owner_ID" ] && [ "$WOWNER" = "$owner_ID" ]; then ok A1.13 "workspace.owner_id is the creator"; else bad A1.13 "owner_id: expected $owner_ID, got '$WOWNER'"; fi
if [ "$GNAME" = general ]; then ok A1.14 "general_channel is named general"; else bad A1.14 "general_channel.name: expected general, got '$GNAME'"; fi

s=$(call GET $N0/api/workspaces/$SLUG/members "" "$owner_TOKEN")
r=$(member_role "$owner_ID")
if [ "$s" = 200 ] && [ "$r" = owner ]; then ok A1.15 "members list shows the creator as owner"
else bad A1.15 "members: expected 200 and role owner for the creator, got status $s and role '$r'"; fi

s=$(call POST $N0/api/workspaces "{\"slug\":\"$SLUG\",\"name\":\"Again\"}" "$owner_TOKEN")
expect A1.16 "duplicate workspace slug" 409 "$s"

s=$(call POST $N0/api/workspaces '{"slug":"A","name":"Bad"}' "$owner_TOKEN")
expect A1.17 "invalid workspace slug" 400 "$s"

s=$(call POST $N0/api/workspaces "{\"slug\":\"x$RUN\",\"name\":\"NoAuth\"}")
expect A1.18 "create workspace without a token" 401 "$s"

s=$(call GET $N0/api/workspaces "" "$owner_TOKEN")
if [ "$s" = 200 ] && grep -q "\"$SLUG\"" "$BODY"; then ok A1.19 "owner lists their workspace"
else bad A1.19 "list workspaces as owner: expected 200 including $SLUG, got status $s"; fi

s=$(call GET $N0/api/workspaces "" "$out1_TOKEN")
if [ "$s" = 200 ] && ! grep -q "\"$SLUG\"" "$BODY"; then ok A1.20 "non-member does not see the workspace"
else bad A1.20 "list workspaces as non-member: expected 200 without $SLUG, got status $s"; fi

s1=$(call GET $N1/api/workspaces "" "$owner_TOKEN"); g1=0; grep -q "\"$SLUG\"" "$BODY" && g1=1
s2=$(call GET $N2/api/workspaces "" "$owner_TOKEN"); g2=0; grep -q "\"$SLUG\"" "$BODY" && g2=1
if [ "$s1" = 200 ] && [ "$s2" = 200 ] && [ $g1 = 1 ] && [ $g2 = 1 ]; then ok A1.21 "workspace is visible on all three nodes"
else bad A1.21 "cross-node: list on 8001 got $s1 (found=$g1), on 8002 got $s2 (found=$g2); expected 200 and found on both"; fi

s=$(call GET $N0/api/workspaces/nope$RUN/members "" "$owner_TOKEN")
expect A1.22 "members of an unknown workspace" 404 "$s"

########## BASIC CHANNELS (rules 22-26) ##########
s=$(call POST $N0/api/workspaces/$SLUG/channels "{\"name\":\"pub-$RUN\"}" "$owner_TOKEN")
CH_PUB=$(field channel.id)
if [ "$s" = 201 ] && [ -n "$CH_PUB" ]; then ok A1.23 "owner creates a public channel"
else bad A1.23 "create public channel as owner: expected 201 with channel.id, got status $s"; fi

s=$(call POST $N0/api/workspaces/$SLUG/channels "{\"name\":\"priv-$RUN\",\"is_private\":true}" "$owner_TOKEN")
CH_PRIV=$(field channel.id); PRIV=$(field channel.is_private)
if [ "$s" = 201 ] && [ -n "$CH_PRIV" ] && [ "$PRIV" = True ]; then ok A1.24 "owner creates a private channel"
else bad A1.24 "create private channel as owner: expected 201 with is_private true, got status $s, is_private '$PRIV'"; fi

s=$(call POST $N0/api/channels/999999999/join '{}' "$member1_TOKEN")
expect A1.25 "join a channel that does not exist" 404 "$s"

s=$(call POST $N0/api/channels/$CH_PUB/join '{}' "$member1_TOKEN")
expect A1.26 "member joins a public channel" 200 "$s"

s=$(call POST $N0/api/channels/$CH_PRIV/join '{}' "$member1_TOKEN")
expect A1.27 "join a private channel without an invite" 403 "$s"

P="$N0/api/workspaces/$SLUG/members"
s=$(call POST $N0/api/channels/$CH_PUB/join '{}' "$out1_TOKEN")
call GET $P "" "$owner_TOKEN" >/dev/null
r=$(member_role "$out1_ID")
if [ "$s" = 200 ] && [ "$r" = member ]; then ok A1.28 "open workspace: a non-member joining a public channel becomes a member"
else bad A1.28 "open workspace join: expected 200 and out1 listed as member, got status $s and role '$r'"; fi

if [ $((SECONDS-START)) -ge $BUDGET ]; then
  echo "NOTE: the check hit its ${BUDGET}s time limit and stopped sending requests, so every rule marked 'got 000' after that point failed only because of the limit. Some request is slow or hangs (more than 3s with no answer): find the first FAIL with 'got 000' and fix that route first."
fi
echo "RESULT: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
