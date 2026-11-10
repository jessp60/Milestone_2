#!/usr/bin/env bash
# Regression suite for Components A and B (the working features from Milestone 1).
# One file that holds all the A and B checks (and the first three baseline boot conditions). It runs them in parallel, so the whole run fits in one
# 30 second command, then prints every failing rule and one overall line.
# Usage: bash /spec/checks/regression.sh      Exit 0 only if every A and B rule passes.
set -u
exec </dev/null
healthy() {
  for p in 8000 8001 8002; do curl -fs --max-time 2 "http://127.0.0.1:$p/api/health" >/dev/null 2>&1 || return 1; done
}
# One start of the cluster here, so the parallel checks do not each try to start it.
if ! healthy; then
  (cd /app && for fd in 3 4 5 6 7 8 9 10 11 12 13 14 15; do eval "exec $fd>&-" 2>/dev/null; done
   setsid nohup /app/start.sh >/tmp/start.log 2>&1 </dev/null &) 2>/dev/null
  for _ in $(seq 1 10); do healthy && break; sleep 1; done
fi
healthy || { echo "FAIL 0 cluster is not answering /api/health on 8000, 8001, 8002 (see /tmp/start.log)"
             echo "REGRESSION: A and B: the checks could not run"; exit 1; }

W=$(mktemp -d /tmp/regression.XXXXXX)
# Each check below is written out to a private file whose temp files have their own names, so the
# parallel runs do not overwrite each other's response files. The checks always run in verbose mode.
##### check A.1 #####
cat > "$W/a_1.sh" <<'RG_A_1'
#!/usr/bin/env bash
# Self-check for stage A.1 (auth, workspaces, channels, open join).
# Part of /spec/checks/regression.sh. Exit 0 only if every rule passes.
set -u
FEEDBACK=verbose
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
RG_A_1

##### check A.2 #####
cat > "$W/a_2.sh" <<'RG_A_2'
#!/usr/bin/env bash
# Self-check for stage A.2 (roles, role-based channel permissions, ownership transfer).
# Part of /spec/checks/regression.sh. Exit 0 only if every rule passes.
set -u
FEEDBACK=verbose
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


for n in owner admin1 admin2 member1 member2 guest1; do mkuser $n; done
SLUG="w$RUN"
s=$(call POST $N0/api/workspaces "{\"slug\":\"$SLUG\",\"name\":\"Check Workspace\"}" "$owner_TOKEN"); setup "create workspace" 201 "$s"
GCH=$(field general_channel.id)
P="$N0/api/workspaces/$SLUG/members"
# Stage A.1 behavior: in an open workspace, joining #general makes a user a workspace member.
join_ws() { local tv="$1_TOKEN" s; s=$(call POST $N0/api/channels/$GCH/join '{}' "${!tv}"); setup "$1 joins the workspace through #general" 200 "$s"; }
s=$(call POST $N0/api/workspaces/$SLUG/channels "{\"name\":\"pub-$RUN\"}" "$owner_TOKEN"); CH_PUB=$(field channel.id); setup "create a public channel" 201 "$s"
for n in admin1 admin2 member1 member2 guest1; do join_ws $n; done

########## ROLES (rules 27-36) ##########
P="$N0/api/workspaces/$SLUG/members"

s=$(call PATCH $P/$admin1_ID '{"role":"admin"}' "$owner_TOKEN")
if [ "$s" = 200 ] && [ "$(field member.role)" = admin ] && [ "$(field member.user_id)" = "$admin1_ID" ]; then
  ok A2.1 "owner promotes a member to admin"
else bad A2.1 "owner promotes admin1: expected 200 and member {user_id: $admin1_ID, role: admin}, got status $s and role '$(field member.role)'"; fi

call GET $P "" "$owner_TOKEN" >/dev/null
r=$(member_role "$admin1_ID")
if [ "$r" = admin ]; then ok A2.2 "members list shows the new role"; else bad A2.2 "members list: expected admin1 to have role admin, got '$r'"; fi

s=$(call PATCH $P/$admin2_ID '{"role":"admin"}' "$owner_TOKEN"); setup "promote admin2" 200 "$s"
s=$(call PATCH $P/$member2_ID '{"role":"admin"}' "$owner_TOKEN"); setup "promote member2" 200 "$s"

s=$(call PATCH $P/$member2_ID '{"role":"member"}' "$owner_TOKEN")
if [ "$s" = 200 ] && [ "$(field member.role)" = member ]; then ok A2.3 "owner demotes an admin to member"
else bad A2.3 "owner demotes member2: expected 200 and role member, got status $s and role '$(field member.role)'"; fi

s=$(call PATCH $P/$guest1_ID '{"role":"guest"}' "$admin1_TOKEN")
if [ "$s" = 200 ] && [ "$(field member.role)" = guest ]; then ok A2.4 "admin sets a member to guest"
else bad A2.4 "admin1 sets guest1 to guest: expected 200 and role guest, got status $s and role '$(field member.role)'"; fi

s=$(call PATCH $P/$admin2_ID '{"role":"member"}' "$admin1_TOKEN")
expect A2.5 "admin changes another admin" 403 "$s"

s=$(call PATCH $P/$owner_ID '{"role":"member"}' "$admin1_TOKEN")
expect A2.6 "admin changes the owner" 403 "$s"

s=$(call PATCH $P/$member1_ID '{"role":"owner"}' "$owner_TOKEN")
expect A2.7 "set role to owner through PATCH" 400 "$s"

s=$(call PATCH $P/$owner_ID '{"role":"admin"}' "$owner_TOKEN")
expect A2.8 "owner changes their own role" 403 "$s"

s=$(call PATCH $P/$member2_ID '{"role":"admin"}' "$member1_TOKEN")
expect A2.9 "plain member changes a role" 403 "$s"

s=$(call PATCH $P/$member1_ID '{"role":"superuser"}' "$owner_TOKEN")
expect A2.10 "unknown role value" 400 "$s"

########## ROLE-BASED CHANNEL PERMISSIONS (rules 37-41) ##########
s=$(call POST $N0/api/channels/$CH_PUB/join '{}' "$guest1_TOKEN")
expect A2.11 "guest joins a public channel" 403 "$s"

s=$(call POST $N0/api/workspaces/$SLUG/channels "{\"name\":\"g-$RUN\"}" "$guest1_TOKEN")
expect A2.12 "guest creates a channel" 403 "$s"

s=$(call POST $N0/api/workspaces/$SLUG/channels "{\"name\":\"m-pub-$RUN\"}" "$member1_TOKEN")
expect A2.13 "member creates a public channel" 201 "$s"

s=$(call POST $N0/api/workspaces/$SLUG/channels "{\"name\":\"m-priv-$RUN\",\"is_private\":true}" "$member1_TOKEN")
expect A2.14 "member creates a private channel" 403 "$s"

s=$(call POST $N0/api/workspaces/$SLUG/channels "{\"name\":\"a-priv-$RUN\",\"is_private\":true}" "$admin1_TOKEN")
expect A2.15 "admin creates a private channel" 201 "$s"

########## OWNERSHIP TRANSFER (rules 63-67; these change the owner, so they run last) ##########
T="$N0/api/workspaces/$SLUG/transfer_ownership"

s=$(call POST $T "{\"new_owner_id\":$(jid "$member1_ID")}" "$owner_TOKEN")
expect A2.16 "transfer ownership to a plain member" 400 "$s"

s=$(call POST $T "{\"new_owner_id\":$(jid "$admin2_ID")}" "$admin1_TOKEN")
expect A2.17 "non-owner transfers ownership" 403 "$s"

s=$(call POST $T "{\"new_owner_id\":$(jid "$admin2_ID")}" "$owner_TOKEN")
if is2xx "$s"; then ok A2.18 "owner transfers ownership to an admin"
else bad A2.18 "owner transfers to admin2: expected 2xx, got $s"; fi

call GET $P "" "$admin2_TOKEN" >/dev/null
r_new=$(member_role "$admin2_ID"); r_old=$(member_role "$owner_ID")
if [ "$r_new" = owner ] && [ "$r_old" = admin ]; then ok A2.19 "after the transfer: target is owner, previous owner is admin"
else bad A2.19 "roles after transfer: expected admin2=owner and previous owner=admin, got '$r_new' and '$r_old'"; fi

s=$(call POST $T "{\"new_owner_id\":$(jid "$admin1_ID")}" "$owner_TOKEN")
expect A2.20 "previous owner transfers ownership again" 403 "$s"

if [ $((SECONDS-START)) -ge $BUDGET ]; then
  echo "NOTE: the check hit its ${BUDGET}s time limit and stopped sending requests, so every rule marked 'got 000' after that point failed only because of the limit. Some request is slow or hangs (more than 3s with no answer): find the first FAIL with 'got 000' and fix that route first."
fi
echo "RESULT: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
RG_A_2

##### check A.3 #####
cat > "$W/a_3.sh" <<'RG_A_3'
#!/usr/bin/env bash
# Self-check for stage A.3 (invitations and join mode).
# Part of /spec/checks/regression.sh. Exit 0 only if every rule passes.
set -u
FEEDBACK=verbose
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


for n in owner admin1 member1 inv1 inv2 tx ty out2; do mkuser $n; done
SLUG="w$RUN"
s=$(call POST $N0/api/workspaces "{\"slug\":\"$SLUG\",\"name\":\"Check Workspace\"}" "$owner_TOKEN"); setup "create workspace" 201 "$s"
GCH=$(field general_channel.id)
P="$N0/api/workspaces/$SLUG/members"
# Stage A.1 behavior: in an open workspace, joining #general makes a user a workspace member.
join_ws() { local tv="$1_TOKEN" s; s=$(call POST $N0/api/channels/$GCH/join '{}' "${!tv}"); setup "$1 joins the workspace through #general" 200 "$s"; }
s=$(call POST $N0/api/workspaces/$SLUG/channels "{\"name\":\"pub-$RUN\"}" "$owner_TOKEN"); CH_PUB=$(field channel.id); setup "create a public channel" 201 "$s"
for n in admin1 member1; do join_ws $n; done
s=$(call PATCH $P/$admin1_ID '{"role":"admin"}' "$owner_TOKEN"); setup "promote admin1" 200 "$s"

########## INVITATIONS (rules 42-55) ##########
I="$N0/api/workspaces/$SLUG/invitations"

s=$(call POST $I '{}' "$admin1_TOKEN")
CODE1=$(field invitation.code)
if [ "$s" = 201 ] && [ -n "$CODE1" ]; then ok A3.1 "admin creates an invitation with a code"
else bad A3.1 "admin creates invitation: expected 201 with invitation.code, got status $s"; fi

s=$(call POST $I '{}' "$member1_TOKEN")
expect A3.2 "member creates an invitation" 403 "$s"

s=$(call GET $I "" "$owner_TOKEN")
if [ "$s" = 200 ] && [ -n "$CODE1" ] && grep -q "$CODE1" "$BODY"; then ok A3.3 "owner lists active invitations"
else bad A3.3 "list invitations as owner: expected 200 including code '$CODE1', got status $s"; fi

s=$(call GET $I "" "$member1_TOKEN")
expect A3.4 "member lists invitations" 403 "$s"

s=$(call POST $N0/api/invitations/$CODE1/accept '{}' "$inv1_TOKEN")
call GET $P "" "$owner_TOKEN" >/dev/null
r=$(member_role "$inv1_ID")
if is2xx "$s" && [ -n "$r" ]; then ok A3.5 "accepting an invitation adds the user to the workspace"
else bad A3.5 "accept invitation: expected 2xx and inv1 in members list, got status $s and role '$r'"; fi

s=$(call POST $N0/api/invitations/$CODE1/accept '{}' "$inv2_TOKEN")
expect A3.6 "accept an exhausted invitation (default max_uses is 1)" 404 "$s"

s=$(call POST $N0/api/invitations/nosuchcode$RUN/accept '{}' "$inv2_TOKEN")
expect A3.7 "accept an unknown invitation code" 404 "$s"

s=$(call POST $I '{"expires_in_seconds":1}' "$owner_TOKEN"); CODE_EXP=$(field invitation.code)
setup "create an expiring invitation" 201 "$s"
sleep 2
s=$(call POST $N0/api/invitations/$CODE_EXP/accept '{}' "$inv2_TOKEN")
expect A3.8 "accept an expired invitation" 404 "$s"

s=$(call POST $I '{}' "$admin1_TOKEN"); CODE_REV=$(field invitation.code)
setup "create an invitation to revoke" 201 "$s"
s=$(call DELETE $I/$CODE_REV "" "$admin1_TOKEN")
if [ "$s" = 200 ] && [ "$(field deleted)" = True ]; then ok A3.9 "admin revokes an invitation"
else bad A3.9 "revoke invitation: expected 200 with {\"deleted\":true}, got status $s and deleted '$(field deleted)'"; fi

s=$(call POST $N0/api/invitations/$CODE_REV/accept '{}' "$inv2_TOKEN")
expect A3.10 "accept a revoked invitation" 404 "$s"

s=$(call POST $I '{}' "$owner_TOKEN"); CODE_KEEP=$(field invitation.code)
setup "create an invitation for the delete check" 201 "$s"
s=$(call DELETE $I/$CODE_KEEP "" "$member1_TOKEN")
expect A3.11 "member revokes an invitation" 403 "$s"

s=$(call POST $I "{\"invited_user_id\":$(jid "$tx_ID")}" "$owner_TOKEN"); CODE_T=$(field invitation.code)
setup "create a targeted invitation" 201 "$s"
s=$(call POST $N0/api/invitations/$CODE_T/accept '{}' "$ty_TOKEN")
expect A3.12 "wrong user accepts a targeted invitation" 403 "$s"

s=$(call POST $N0/api/invitations/$CODE_T/accept '{}' "$tx_TOKEN")
if is2xx "$s"; then ok A3.13 "the targeted user accepts their invitation"
else bad A3.13 "targeted user accepts: expected 2xx, got $s"; fi

s=$(call POST $N0/api/invitations/$CODE_KEEP/accept '{}')
expect A3.14 "accept an invitation without a token" 401 "$s"

########## MORE INVITATION AND AUTH RULES (68-72) ##########
for n in x1 x2 x3 t1; do mkuser $n; done
s=$(call POST $I '{"max_uses":2}' "$owner_TOKEN"); CODE_M=$(field invitation.code)
setup "create an invitation with max_uses 2" 201 "$s"
sa=$(call POST $N0/api/invitations/$CODE_M/accept '{}' "$x1_TOKEN")
sb=$(call POST $N0/api/invitations/$CODE_M/accept '{}' "$x2_TOKEN")
sc=$(call POST $N0/api/invitations/$CODE_M/accept '{}' "$x3_TOKEN")
if is2xx "$sa" && is2xx "$sb" && [ "$sc" = 404 ]; then ok A3.15 "max_uses 2 allows two accepts and a third gets 404"
else bad A3.15 "max_uses 2: expected accept 2xx, 2xx, then 404, got $sa, $sb, $sc"; fi

call GET $I "" "$owner_TOKEN" >/dev/null
if grep -q "$CODE_M" "$BODY"; then bad A3.16 "exhausted invitation: expected it to be missing from the list, but code $CODE_M is still listed"
else ok A3.16 "an exhausted invitation is not listed"; fi
if grep -q "$CODE_REV" "$BODY"; then bad A3.17 "revoked invitation: expected it to be missing from the list, but code $CODE_REV is still listed"
else ok A3.17 "a revoked invitation is not listed"; fi

s=$(call POST $I "{\"invited_user_id\":$(jid "$t1_ID"),\"max_uses\":5}" "$owner_TOKEN"); CODE_T2=$(field invitation.code)
setup "create a targeted invitation with max_uses 5" 201 "$s"
sa=$(call POST $N0/api/invitations/$CODE_T2/accept '{}' "$t1_TOKEN")
sb=$(call POST $N0/api/invitations/$CODE_T2/accept '{}' "$t1_TOKEN")
if is2xx "$sa" && [ "$sb" = 404 ]; then ok A3.18 "a targeted invitation is single-use even with max_uses 5"
else bad A3.18 "targeted invitation single-use: expected first accept 2xx then 404, got $sa then $sb"; fi

########## API CONVENTIONS (rules 73-78) ##########
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
A_ROUTES=(
  "GET|/api/workspaces|"
  "GET|/api/workspaces/$SLUG/members|"
  "PATCH|/api/workspaces/$SLUG/members/$member1_ID|{\"role\":\"member\"}"
  "PATCH|/api/workspaces/$SLUG|{\"name\":\"Conv\"}"
  "POST|/api/workspaces/$SLUG/invitations|{}"
  "GET|/api/workspaces/$SLUG/invitations|"
  "DELETE|/api/workspaces/$SLUG/invitations/nosuchcode|"
  "POST|/api/workspaces/$SLUG/transfer_ownership|{\"new_owner_id\":1}"
  "POST|/api/workspaces/$SLUG/channels|{\"name\":\"zz-$RUN\"}"
  "POST|/api/channels/$CH_PUB/join|{}"
)
r=$(sweep 401 "" "${A_ROUTES[@]}")
pass_if A3.19 "every authenticated route returns 401 with no token" "routes that did not return 401:$r" [ -z "$r" ]
r=$(sweep 401 "not-a-real-token-000000" "${A_ROUTES[@]}")
pass_if A3.20 "every authenticated route returns 401 with an invalid token" "routes that did not return 401:$r" [ -z "$r" ]
r=$(sweep 404 "$owner_TOKEN" \
  "PATCH|/api/workspaces/nope$RUN/members/$member1_ID|{\"role\":\"member\"}" \
  "PATCH|/api/workspaces/$SLUG/members/999999999|{\"role\":\"member\"}" \
  "PATCH|/api/workspaces/nope$RUN|{\"name\":\"Conv\"}" \
  "GET|/api/workspaces/nope$RUN/invitations|" \
  "POST|/api/workspaces/nope$RUN/invitations|{}" \
  "POST|/api/workspaces/nope$RUN/transfer_ownership|{\"new_owner_id\":1}" \
  "POST|/api/workspaces/nope$RUN/channels|{\"name\":\"zz-$RUN\"}")
pass_if A3.21 "unknown workspace or user ids return 404" "routes that did not return 404:$r" [ -z "$r" ]
r=$(sweep 400 "$owner_TOKEN" \
  "POST|/api/workspaces|{\"slug\":\"cv-$RUN\"}" \
  "POST|/api/workspaces/$SLUG/channels|{}" \
  "PATCH|/api/workspaces/$SLUG/members/$member1_ID|{}")
r2=$(sweep 400 "" "POST|/api/auth/register|{\"username\":\"cv$RUN\"}")
pass_if A3.22 "missing required fields return 400" "routes that did not return 400:$r$r2" [ -z "$r$r2" ]
r=$(sweep 400 "$owner_TOKEN" "POST|/api/workspaces|{not json" "POST|/api/workspaces/$SLUG/channels|{not json")
r2=$(sweep 400 "" "POST|/api/auth/register|{not json" "POST|/api/auth/login|{not json")
pass_if A3.23 "a malformed JSON body returns 400" "routes that did not return 400:$r$r2" [ -z "$r$r2" ]
mkuser cn
s=$(call POST $N1/api/workspaces/$SLUG/invitations '{}' "$owner_TOKEN"); CODE_CN=$(field invitation.code)
setup "create an invitation on node 1" 201 "$s"
sa=$(call POST $N2/api/invitations/$CODE_CN/accept '{}' "$cn_TOKEN")
call GET $N0/api/workspaces/$SLUG/members "" "$owner_TOKEN" >/dev/null
rcn=$(member_role "$cn_ID")
pass_if A3.24 "an invitation created on node 1 is accepted on node 2 and shows on node 0" "accept on node 2 got $sa, role seen on node 0: '$rcn' (expected a member role)" [ "$(echo $sa | cut -c1)" = 2 -a -n "$rcn" ]

########## JOIN MODE (rules 56-62) ##########
s=$(call PATCH $N0/api/workspaces/$SLUG '{"join_mode":"invite_only"}' "$owner_TOKEN")
if [ "$s" = 200 ] && [ "$(field workspace.join_mode)" = invite_only ]; then ok A3.25 "owner sets join_mode to invite_only"
else bad A3.25 "owner sets join_mode: expected 200 and workspace.join_mode invite_only, got status $s and '$(field workspace.join_mode)'"; fi

s=$(call PATCH $N0/api/workspaces/$SLUG '{"name":"Renamed Workspace"}' "$admin1_TOKEN")
if [ "$s" = 200 ] && [ "$(field workspace.name)" = "Renamed Workspace" ]; then ok A3.26 "admin renames the workspace"
else bad A3.26 "admin renames: expected 200 and workspace.name 'Renamed Workspace', got status $s and '$(field workspace.name)'"; fi

s=$(call PATCH $N0/api/workspaces/$SLUG '{"name":"Nope"}' "$member1_TOKEN")
expect A3.27 "member renames the workspace" 403 "$s"

s=$(call PATCH $N0/api/workspaces/$SLUG '{"join_mode":"closed"}' "$owner_TOKEN")
expect A3.28 "unknown join_mode value" 400 "$s"

s=$(call POST $N0/api/channels/$CH_PUB/join '{}' "$out2_TOKEN")
expect A3.29 "invite_only workspace: a non-member joins a public channel" 403 "$s"

s=$(call POST $I '{}' "$owner_TOKEN"); CODE_J=$(field invitation.code)
setup "create an invitation in an invite_only workspace" 201 "$s"
sa=$(call POST $N0/api/invitations/$CODE_J/accept '{}' "$out2_TOKEN")
s=$(call POST $N0/api/channels/$CH_PUB/join '{}' "$out2_TOKEN")
if is2xx "$sa" && [ "$s" = 200 ]; then ok A3.30 "invite_only workspace: redeeming an invitation allows joining"
else bad A3.30 "invite_only join after redeeming: expected accept 2xx then join 200, got accept $sa and join $s"; fi

if [ $((SECONDS-START)) -ge $BUDGET ]; then
  echo "NOTE: the check hit its ${BUDGET}s time limit and stopped sending requests, so every rule marked 'got 000' after that point failed only because of the limit. Some request is slow or hangs (more than 3s with no answer): find the first FAIL with 'got 000' and fix that route first."
fi
echo "RESULT: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
RG_A_3

##### check B.1 #####
cat > "$W/b_1.sh" <<'RG_B_1'
#!/usr/bin/env bash
# Self-check for stage B.1 (channel management).
# Part of /spec/checks/regression.sh. Exit 0 only if every rule passes.
set -u
FEEDBACK=verbose
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

########## CHANNEL MANAGEMENT ##########
NA="c$RUN-a"
s=$(call POST $N0/api/workspaces/$SLUG/channels "{\"name\":\"$NA\"}" "$owner_TOKEN")
CH_A=$(field channel.id)
f=$(pj "all(k in d['channel'] for k in $CHF)")
v=$(pj "[d['channel']['name'],d['channel']['workspace_id'],d['channel']['is_private'],d['channel']['is_dm'],d['channel']['is_archived']]")
pass_if B1.1 "create channel returns 201 with a full ChannelObj" "got status $s, fields present '$f', values '$v' (expected ['$NA', $WID, False, False, False])" \
  [ "$s" = 201 -a "$f" = True -a "$v" = "['$NA', $WID, False, False, False]" ]
t=$(field channel.topic)
pass_if B1.2 "a fresh channel's topic is the empty string" "got topic '$t'" [ -z "$t" -a "$s" = 201 ]

s=$(call POST $N0/api/workspaces/$SLUG/channels "{\"name\":\"c$RUN-t\",\"topic\":\"hello\"}" "$owner_TOKEN")
pass_if B1.3 "create channel with a topic keeps it" "got status $s, topic '$(field channel.topic)'" [ "$s" = 201 -a "$(field channel.topic)" = hello ]

LONG=$(python3 -c 'print("x"*251)')
s=$(call POST $N0/api/workspaces/$SLUG/channels "{\"name\":\"c$RUN-lt\",\"topic\":\"$LONG\"}" "$owner_TOKEN")
pass_if B1.4 "create channel with a 251 character topic" "expected 400, got $s" [ "$s" = 400 ]

s=$(call POST $N0/api/workspaces/$SLUG/channels '{"name":"Bad Name"}' "$owner_TOKEN")
pass_if B1.5 "create channel with an invalid name" "expected 400, got $s" [ "$s" = 400 ]

s=$(call POST $N0/api/workspaces/$SLUG/channels "{\"name\":\"$NA\"}" "$owner_TOKEN")
pass_if B1.6 "create channel with a duplicate name" "expected 409, got $s" [ "$s" = 409 ]

mkchan "$owner_TOKEN" "c$RUN-p" ',"is_private":true'; CH_PRIV=$CID
s=$(call GET $N0/api/workspaces/$SLUG "" "$owner_TOKEN")
f=$(pj "d['workspace']['slug']=='$SLUG' and isinstance(d['channels'],list) and 'read_state' in d")
g=$(pj "all(all(k in c for k in $CHF) for c in d['channels']) and 'general' in [c['name'] for c in d['channels']] and $CH_A in [c['id'] for c in d['channels']]")
inown=$(pj "$CH_PRIV in [c['id'] for c in d['channels']]")
pass_if B1.7 "workspace detail returns workspace, channels and read_state" "got status $s, shape '$f', channels ok '$g'" [ "$s" = 200 -a "$f" = True -a "$g" = True ]
s=$(call GET $N0/api/workspaces/$SLUG "" "$m2_TOKEN")
inm2=$(pj "$CH_PRIV in [c['id'] for c in d['channels']]")
pass_if B1.8 "a private channel is hidden from non-members but shown to its creator" "creator sees it: '$inown', non-member sees it: '$inm2' (status $s)" [ "$s" = 200 -a "$inown" = True -a "$inm2" = False ]

s=$(join $CH_A "$m1_TOKEN"); setup "m1 joins channel a" 200 "$s"
s=$(call PATCH $N0/api/channels/$CH_A '{"topic":"by member"}' "$m1_TOKEN")
pass_if B1.9 "a channel member updates the topic" "got status $s, topic '$(field channel.topic)'" [ "$s" = 200 -a "$(field channel.topic)" = "by member" ]

mkchan "$m1_TOKEN" "c$RUN-m"; CH_M1=$CID
s=$(call PATCH $N0/api/channels/$CH_M1 '{"topic":"by owner"}' "$owner_TOKEN")
pass_if B1.10 "the workspace owner (not in the channel) updates the topic" "got status $s, topic '$(field channel.topic)'" [ "$s" = 200 -a "$(field channel.topic)" = "by owner" ]

s=$(call PATCH $N0/api/channels/$CH_A "{\"topic\":\"$LONG\"}" "$owner_TOKEN")
pass_if B1.11 "update topic to 251 characters" "expected 400, got $s" [ "$s" = 400 ]

s=$(call PATCH $N0/api/channels/$CH_A '{"topic":"nope"}' "$m2_TOKEN")
pass_if B1.12 "a non-member who is not the owner updates the topic" "expected 403, got $s" [ "$s" = 403 ]

s=$(call GET $N0/api/channels/$CH_A/members "" "$owner_TOKEN")
f=$(pj "all(all(k in u for k in $UF) for u in d['members'])")
ids=$(pj "sorted(str(u['id']) for u in d['members'])")
pass_if B1.13 "channel members are UserObjs (creator and joined user listed)" "got status $s, UserObj fields '$f', ids $ids" \
  [ "$s" = 200 -a "$f" = True -a "$(pj "'$owner_ID' in [str(u['id']) for u in d['members']] and '$m1_ID' in [str(u['id']) for u in d['members']]")" = True ]

s=$(call POST $N0/api/channels/$CH_A/leave '{}' "$m1_TOKEN")
pass_if B1.14 "leave a channel" "got status $s, channel.id '$(field channel.id)' (expected $CH_A)" [ "$s" = 200 -a "$(field channel.id)" = "$CH_A" ]
s=$(call POST $N0/api/channels/$CH_A/leave '{}' "$m1_TOKEN")
pass_if B1.15 "leaving again is idempotent" "expected 200, got $s" [ "$s" = 200 ]
call GET $N0/api/channels/$CH_A/members "" "$owner_TOKEN" >/dev/null
pass_if B1.16 "the user who left is no longer listed" "members after leaving: $(pj "[str(u['id']) for u in d['members']]")" \
  [ "$(pj "'$m1_ID' in [str(u['id']) for u in d['members']]")" = False ]

mkchan "$owner_TOKEN" "c$RUN-x"; CH_X=$CID
s=$(call POST $N0/api/channels/$CH_X/archive '{}' "$owner_TOKEN")
pass_if B1.17 "the workspace owner archives a channel" "got status $s, is_archived '$(field channel.is_archived)'" [ "$s" = 200 -a "$(field channel.is_archived)" = True ]
s=$(call POST $N0/api/channels/$CH_M1/archive '{}' "$m1_TOKEN")
pass_if B1.18 "a non-owner archives a channel" "expected 403, got $s" [ "$s" = 403 ]
call GET $N0/api/workspaces/$SLUG "" "$owner_TOKEN" >/dev/null
hid=$(pj "$CH_X in [c['id'] for c in d['channels']]")
call GET "$N0/api/workspaces/$SLUG?include_archived=1" "" "$owner_TOKEN" >/dev/null
shown=$(pj "$CH_X in [c['id'] for c in d['channels']]")
pass_if B1.19 "archived channels are hidden unless include_archived=1" "listed by default: '$hid', listed with include_archived=1: '$shown'" [ "$hid" = False -a "$shown" = True ]
s=$(call POST $N0/api/channels/$CH_X/unarchive '{}' "$owner_TOKEN")
pass_if B1.20 "the workspace owner unarchives a channel" "got status $s, is_archived '$(field channel.is_archived)'" [ "$s" = 200 -a "$(field channel.is_archived)" = False ]

LONGNAME=$(python3 -c 'print("n"*33)')
s=$(call POST $N0/api/workspaces/$SLUG/channels "{\"name\":\"$LONGNAME\"}" "$owner_TOKEN")
pass_if B1.21 "a channel name longer than 32 characters" "expected 400, got $s" [ "$s" = 400 ]
T250=$(python3 -c 'print("x"*250)')
s=$(call POST $N0/api/workspaces/$SLUG/channels "{\"name\":\"c$RUN-t250\",\"topic\":\"$T250\"}" "$owner_TOKEN")
pass_if B1.22 "a topic of exactly 250 characters is accepted" "expected 201, got $s" [ "$s" = 201 ]
if [ $((SECONDS-START)) -ge $BUDGET ]; then
  echo "NOTE: the time limit was reached; later checks may show 'got 000'. Some route is slow or hangs (no answer within 3 seconds): fix the first 'got 000' first."
fi
echo "RESULT: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
RG_B_1

##### check B.2 #####
cat > "$W/b_2.sh" <<'RG_B_2'
#!/usr/bin/env bash
# Self-check for stage B.2 (messages, replies, paging, edit, delete and reactions).
# Part of /spec/checks/regression.sh. Exit 0 only if every rule passes.
set -u
FEEDBACK=verbose
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
RULES = [("B54", "subscribe is answered with a subscribed frame carrying head_event_id"),
         ("B55", "a new message is broadcast as message.created with event_id and channel_id"),
         ("B56", "a thread reply is broadcast as message.reply"),
         ("B57", "joining a channel is broadcast as member.joined"),
         ("B58", "leaving a channel is broadcast as member.left"),
         ("B59", "changing the topic is broadcast as channel.updated"),
         ("B60", "archiving and unarchiving are broadcast as channel.archived and channel.unarchived"),
         ("B61", "event_ids are dense: first is head_event_id + 1, then exactly +1 each, no duplicates"),
         ("B62", "posts through nodes 0, 1 and 2 reach subscribers on every node with the same event_ids"),
         ("B63", "resume on another node replays the missed events in order, then sends resumed with head_event_id"),
         ("B90", "editing a message is broadcast as message.updated"),
         ("B91", "deleting a message is broadcast as message.deleted"),
         ("B92", "adding a reaction is broadcast as reaction.added"),
         ("B93", "removing a reaction is broadcast as reaction.removed")]
def skip_rest(first, why):
    for rid, d in RULES:
        if rid >= first: out(False, rid, d, why)
seen = []
ws, f, seen = sub(ch, otok)
head = f.get("head_event_id") if f else None
out(bool(f) and isint(head), "B54", RULES[0][1], "no subscribed frame within 2s (websocket connected: %s)" % ws.ok if not f else "head_event_id is %r" % head)
if not (f and isint(head)):
    skip_rest("B55", "needs the subscribed frame (B54)")
else:
    def expect(rid, kind, resp, desc):
        if resp[0] not in (200, 201):
            out(False, rid, desc, "the request itself returned status %s" % resp[0]); return None
        fr = wait(ws, lambda x: x.get("type") == kind and x.get("channel_id") == ch, seen)
        out(bool(fr) and isint(fr.get("event_id")), rid, desc, "no %s frame with an integer event_id within 1.5s" % kind)
        return fr
    st, j = http("POST", "/api/channels/%d/messages" % ch, {"body": "ev one"}, otok)
    mid = (j or {}).get("message", {}).get("id")
    expect("B55", "message.created", (st,), RULES[1][1])
    st, j = http("POST", "/api/channels/%d/messages" % ch, {"body": "ev reply", "parent_id": mid}, otok)
    expect("B56", "message.reply", (st,), RULES[2][1])
    expect("B90", "message.updated", (http("PATCH", "/api/messages/%s" % mid, {"body": "ev edited"}, otok)[0],), RULES[10][1])
    thumb = "\U0001F44D"
    expect("B92", "reaction.added", (http("POST", "/api/messages/%s/reactions" % mid, {"emoji": thumb}, otok)[0],), RULES[12][1])
    expect("B93", "reaction.removed", (http("DELETE", "/api/messages/%s/reactions/%s" % (mid, urllib.parse.quote(thumb)), None, otok)[0],), RULES[13][1])
    expect("B91", "message.deleted", (http("DELETE", "/api/messages/%s" % mid, None, otok)[0],), RULES[11][1])
    expect("B57", "member.joined", (http("POST", "/api/channels/%d/join" % ch, {}, mtok)[0],), RULES[3][1])
    expect("B58", "member.left", (http("POST", "/api/channels/%d/leave" % ch, {}, mtok)[0],), RULES[4][1])
    expect("B59", "channel.updated", (http("PATCH", "/api/channels/%d" % ch, {"topic": "ev topic"}, otok)[0],), RULES[5][1])
    a = http("POST", "/api/channels/%d/archive" % ch, {}, otok)[0]
    fa = wait(ws, lambda x: x.get("type") == "channel.archived", seen) if a == 200 else None
    u = http("POST", "/api/channels/%d/unarchive" % ch, {}, otok)[0]
    fu = wait(ws, lambda x: x.get("type") == "channel.unarchived", seen) if u == 200 else None
    out(bool(fa) and bool(fu), "B60", RULES[6][1], "archive returned %s and %s frame, unarchive returned %s and %s frame" % (a, "a" if fa else "no", u, "a" if fu else "no"))
    ids = [x["event_id"] for x in seen if isint(x.get("event_id")) and x.get("channel_id") == ch]
    dense = bool(ids) and ids[0] == head + 1 and all(ids[i + 1] == ids[i] + 1 for i in range(len(ids) - 1))
    out(dense, "B61", RULES[7][1], "head_event_id %s, event_ids received %s" % (head, ids))
ws.close()

# cluster: one subscriber per node, posts through every node
subs = [sub(cl, otok, n) for n in range(3)]
if not all(s[1] for s in subs):
    out(False, "B62", RULES[8][1], "subscribe failed on node(s) %s" % [n for n, s in enumerate(subs) if not s[1]])
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
    out(good, "B62", RULES[8][1], "head %s, event_ids seen on nodes 0/1/2: %s" % (h0, got))
for w, _, _ in subs: w.close()

# resume: events missed while disconnected are replayed on another node
w, f, lg = sub(rs, otok, 0)
if not f:
    out(False, "B63", RULES[9][1], "could not subscribe to the resume channel")
else:
    got = []
    for n in (1, 2, 0):
        http("POST", "/api/channels/%d/messages" % rs, {"body": "rs-%d" % n}, otok, n)
        fr = wait(w, lambda x: x.get("type") == "message.created" and x.get("channel_id") == rs, lg)
        got.append(fr.get("event_id") if fr else None)
    w.close()
    if None in got:
        out(False, "B63", RULES[9][1], "the first connection did not receive its 3 message.created events (got %s)" % got)
    else:
        w2 = WS(2, otok); l2 = []
        if not w2.ok:
            out(False, "B63", RULES[9][1], "could not connect to node 2")
        else:
            w2.send({"type": "resume", "channel_id": rs, "since_event_id": got[0]})
            r = wait(w2, lambda x: x.get("type") == "resumed" and x.get("channel_id") == rs, l2, 2)
            replay = [x.get("event_id") for x in l2 if isint(x.get("event_id")) and x.get("channel_id") == rs]
            good = bool(r) and replay == got[1:] and r.get("head_event_id") == got[2] and l2.index(r) == len(l2) - 1
            out(good, "B63", RULES[9][1], "since_event_id %s: replayed %s (expected %s), resumed frame %s" % (got[0], replay, got[1:], r))
        w2.close()
PY

########## MESSAGE HISTORY AND PAGING ##########
mkchan "$owner_TOKEN" "c$RUN-l"; CH_MSG=$CID
s=$(post $CH_MSG lst-1 "$owner_TOKEN")
f=$(pj "all(k in d['message'] for k in $MF)")
v=$(pj "[d['message']['body'],d['message']['parent_id'],d['message']['reply_count'],d['message']['edited_at'],d['message']['files'],d['message']['reactions'],d['message']['mentions'],str(d['message']['author_id'])]")
pass_if B2.1 "post a message returns 201 with a full MessageObj" "got status $s, fields present '$f', values '$v' (expected ['lst-1', None, 0, None, [], [], [], '$owner_ID'])" \
  [ "$s" = 201 -a "$f" = True -a "$v" = "['lst-1', None, 0, None, [], [], [], '$owner_ID']" ]
ca=$(pj 'd["message"]["created_at"]')
pass_if B2.2 "created_at is an RFC 3339 UTC timestamp" "got '$ca'" \
  [ "$(pj 'bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z", d["message"]["created_at"]))')" = True ]
s=$(post $CH_MSG "" "$owner_TOKEN")
pass_if B2.3 "post an empty message" "expected 400, got $s" [ "$s" = 400 ]

for i in 2 3 4 5; do s=$(post $CH_MSG lst-$i "$owner_TOKEN"); setup "post lst-$i" 201 "$s"; done
L="$N0/api/channels/$CH_MSG/messages"
BODIES='",".join(m["body"] for m in d["messages"])'
s=$(call GET $L "" "$owner_TOKEN"); b=$(pj "$BODIES"); nc=$(pj 'd["next_cursor"]')
pass_if B2.4 "list messages newest-first with next_cursor null on the last page" "got status $s, bodies '$b', next_cursor '$nc'" \
  [ "$s" = 200 -a "$b" = "lst-5,lst-4,lst-3,lst-2,lst-1" -a "$nc" = None ]
s=$(call GET "$L?limit=2" "" "$owner_TOKEN"); b=$(pj "$BODIES"); C1=$(pj 'd["next_cursor"]')
pass_if B2.5 "first page with limit=2" "got status $s, bodies '$b', next_cursor '$C1'" [ "$s" = 200 -a "$b" = "lst-5,lst-4" -a -n "$C1" -a "$C1" != None ]
s=$(call GET "$L?limit=2&cursor=$(qs "$C1")" "" "$owner_TOKEN"); b=$(pj "$BODIES"); C2=$(pj 'd["next_cursor"]')
pass_if B2.6 "following next_cursor returns the next older messages" "got status $s, bodies '$b' (expected lst-3,lst-2)" [ "$s" = 200 -a "$b" = "lst-3,lst-2" -a -n "$C2" -a "$C2" != None ]
s=$(call GET "$L?limit=2&cursor=$(qs "$C2")" "" "$owner_TOKEN"); b=$(pj "$BODIES"); nc=$(pj 'd["next_cursor"]')
pass_if B2.7 "the last page has next_cursor null" "got status $s, bodies '$b', next_cursor '$nc'" [ "$s" = 200 -a "$b" = "lst-1" -a "$nc" = None ]
s1=$(call GET "$N1/api/channels/$CH_MSG/messages?limit=2&cursor=$(qs "$C1")" "" "$owner_TOKEN"); b1=$(pj "$BODIES")
s2=$(call GET "$N2/api/channels/$CH_MSG/messages?limit=2&cursor=$(qs "$C1")" "" "$owner_TOKEN"); b2=$(pj "$BODIES")
pass_if B2.8 "a cursor issued by node 0 works on nodes 1 and 2" "node 1 got $s1 '$b1', node 2 got $s2 '$b2' (expected 200 'lst-3,lst-2')" \
  [ "$s1" = 200 -a "$s2" = 200 -a "$b1" = "lst-3,lst-2" -a "$b2" = "lst-3,lst-2" ]
post $CH_MSG lst-6 "$owner_TOKEN" >/dev/null; post $CH_MSG lst-7 "$owner_TOKEN" >/dev/null
s=$(call GET "$L?limit=2&cursor=$(qs "$C1")" "" "$owner_TOKEN"); b=$(pj "$BODIES")
pass_if B2.9 "a cursor is not invalidated by new messages" "got status $s, bodies '$b' (expected lst-3,lst-2)" [ "$s" = 200 -a "$b" = "lst-3,lst-2" ]
s=$(call GET "$L?cursor=$(qs 'not*a*cursor')" "" "$owner_TOKEN")
pass_if B2.10 "a malformed cursor" "expected 400, got $s" [ "$s" = 400 ]
a=$(call GET "$L?limit=0" "" "$owner_TOKEN"); b=$(call GET "$L?limit=-1" "" "$owner_TOKEN"); c=$(call GET "$L?limit=201" "" "$owner_TOKEN")
pass_if B2.11 "limit 0, -1 and 201 are out of range" "expected 400 for each, got $a, $b, $c" [ "$a" = 400 -a "$b" = 400 -a "$c" = 400 ]
mkchan "$owner_TOKEN" "c$RUN-b"; CH_BIG=$CID
# AIDEV-NOTE: posts are sequential, one at a time (a burst of parallel posts is not what this rule tests).
BP=$(timeout 12 python3 /tmp/check_b_ws.py bulk "$CH_BIG" "$owner_TOKEN" 55); setup "post 55 messages" 55 "$BP"
s=$(call GET $N0/api/channels/$CH_BIG/messages "" "$owner_TOKEN"); n=$(pj 'len(d["messages"])'); nc=$(pj 'd["next_cursor"]')
pass_if B2.12 "limit defaults to 50" "with 55 messages and no limit: status $s, $n messages, next_cursor '$nc' (expected 50 messages and a cursor)" [ "$s" = 200 -a "$n" = 50 -a -n "$nc" -a "$nc" != None ]
call GET $L "" "$owner_TOKEN" >/dev/null; first=$(pj "$BODIES")
a=$(call GET "$L?offset=2" "" "$owner_TOKEN"); ba=$(pj "$BODIES"); b=$(call GET "$L?page=2" "" "$owner_TOKEN"); bb=$(pj "$BODIES")
oa=0; [ "$a" = 400 ] || { [ "$a" = 200 ] && [ "$ba" = "$first" ] && oa=1; }; [ "$a" = 400 ] && oa=1
ob=0; [ "$b" = 400 ] || { [ "$b" = 200 ] && [ "$bb" = "$first" ] && ob=1; }; [ "$b" = 400 ] && ob=1
pass_if B2.13 "offset and page parameters are not supported" "?offset=2 got $a and ?page=2 got $b; each must be 400 or return the same first page as no parameter" [ $oa = 1 -a $ob = 1 ]

mkchan "$owner_TOKEN" "c$RUN-r"; CH_REP=$CID
post $CH_REP parent "$owner_TOKEN" >/dev/null; PID=$(field message.id)
for i in 1 2 3; do s=$(post $CH_REP r$i "$owner_TOKEN" "$PID"); setup "post reply r$i" 201 "$s"; done
RP="$N0/api/messages/$PID/replies"
s=$(call GET $RP "" "$owner_TOKEN"); b=$(pj '",".join(m["body"] for m in d["replies"])'); pp=$(pj "all(str(m['parent_id'])=='$PID' for m in d['replies'])"); nc=$(pj 'd["next_cursor"]')
pass_if B2.14 "replies are listed oldest-first" "got status $s, bodies '$b', parent_id ok '$pp', next_cursor '$nc'" [ "$s" = 200 -a "$b" = "r1,r2,r3" -a "$pp" = True -a "$nc" = None ]
call GET $N0/api/channels/$CH_REP/messages "" "$owner_TOKEN" >/dev/null
rc=$(pj "[m['reply_count'] for m in d['messages'] if str(m['id'])=='$PID']")
pass_if B2.15 "the parent message's reply_count counts its replies" "got reply_count $rc (expected [3])" [ "$rc" = "[3]" ]


mkchan "$owner_TOKEN" "c$RUN-x"; CH_X=$CID
s=$(call POST $N0/api/channels/$CH_X/archive '{}' "$owner_TOKEN"); setup "archive a channel" 200 "$s"
s=$(post $CH_X "write-while-archived" "$owner_TOKEN")
pass_if B2.16 "writing to an archived channel" "expected 423, got $s" [ "$s" = 423 ]
s=$(call POST $N0/api/channels/$CH_X/unarchive '{}' "$owner_TOKEN"); setup "unarchive a channel" 200 "$s"
s=$(post $CH_X "write-after-unarchive" "$owner_TOKEN")
pass_if B2.17 "unarchiving restores write access" "expected 201, got $s" [ "$s" = 201 ]

s=$(call GET "$RP?limit=2" "" "$owner_TOKEN"); b1=$(pj '",".join(m["body"] for m in d["replies"])'); cr=$(pj 'd["next_cursor"]')
s2=$(call GET "$RP?limit=2&cursor=$(qs "$cr")" "" "$owner_TOKEN"); b2=$(pj '",".join(m["body"] for m in d["replies"])'); nc=$(pj 'd["next_cursor"]')
pass_if B2.18 "replies accept limit and cursor (oldest-first pages)" "page 1 got $s '$b1' (expected r1,r2), page 2 got $s2 '$b2' (expected r3) with next_cursor '$nc' (expected None)" \
  [ "$s" = 200 -a "$b1" = "r1,r2" -a "$s2" = 200 -a "$b2" = r3 -a "$nc" = None ]

########## EDIT, DELETE AND REACTIONS ##########
mkchan "$owner_TOKEN" "c$RUN-ed"; CH_ED=$CID
for n in m1 m2; do tv="${n}_TOKEN"; s=$(join $CH_ED "${!tv}"); setup "$n joins the edit channel" 200 "$s"; done
post $CH_ED "orig-owner" "$owner_TOKEN" >/dev/null; MA=$(field message.id)
post $CH_ED "orig-m1" "$m1_TOKEN" >/dev/null; MB=$(field message.id)
post $CH_ED "orig-m1-b" "$m1_TOKEN" >/dev/null; MC=$(field message.id)
s=$(call PATCH $N0/api/messages/$MA '{"body":"owner edited"}' "$owner_TOKEN")
pass_if B2.19 "the author edits a message: 200, new body, edited_at set" "got status $s, body '$(field message.body)', edited_at '$(field message.edited_at)'" \
  [ "$s" = 200 -a "$(field message.body)" = "owner edited" -a -n "$(field message.edited_at)" -a "$(field message.edited_at)" != None ]
s=$(call PATCH $N0/api/messages/$MA '{"body":"hijack"}' "$m1_TOKEN")
pass_if B2.20 "a user who is not the author cannot edit a message" "expected 403, got $s" [ "$s" = 403 ]
s=$(call DELETE $N0/api/messages/$MB "" "$m1_TOKEN")
pass_if B2.21 "the author deletes a message: 200 {deleted: true}" "got status $s, deleted '$(field deleted)'" [ "$s" = 200 -a "$(field deleted)" = True ]
call GET "$N0/api/channels/$CH_ED/messages?limit=50" "" "$owner_TOKEN" >/dev/null
live=$(pj "any(m['id']==$MB and m['body']=='orig-m1' for m in d['messages'])")
pass_if B2.22 "a deleted message no longer shows its text in the channel listing" "the original message is still listed: '$live'" [ "$live" = False ]
s=$(call DELETE $N0/api/messages/$MB "" "$m1_TOKEN")
pass_if B2.23 "deleting a message again is idempotent: 200 {deleted: true}" "got status $s, deleted '$(field deleted)'" [ "$s" = 200 -a "$(field deleted)" = True ]
s=$(call PATCH $N0/api/messages/$MB '{"body":"too late"}' "$m1_TOKEN")
pass_if B2.24 "editing a deleted message returns 409" "expected 409, got $s" [ "$s" = 409 ]
s=$(call DELETE $N0/api/messages/$MC "" "$owner_TOKEN")
pass_if B2.25 "the workspace owner deletes another user's message" "got status $s, deleted '$(field deleted)'" [ "$s" = 200 -a "$(field deleted)" = True ]
s=$(call DELETE $N0/api/messages/$MA "" "$m2_TOKEN")
pass_if B2.26 "a member who is neither the author nor the workspace owner cannot delete a message" "expected 403, got $s" [ "$s" = 403 ]
THUMB_PATH='%F0%9F%91%8D'; THUMB_JSON='{"emoji":"\ud83d\udc4d"}'
s=$(call POST $N0/api/messages/$MA/reactions "$THUMB_JSON" "$m1_TOKEN"); r1=$(pj 'json.dumps(d["reactions"],sort_keys=True)')
has=$(pj "'\\U0001F44D' in json.dumps(d['reactions'], ensure_ascii=False)")
pass_if B2.27 "adding a reaction returns 200 {reactions} that include the emoji, even on the first insert" "got status $s, emoji present '$has', reactions '$r1'" [ "$s" = 200 -a "$has" = True ]
s=$(call POST $N0/api/messages/$MA/reactions "$THUMB_JSON" "$m1_TOKEN"); r2=$(pj 'json.dumps(d["reactions"],sort_keys=True)')
pass_if B2.28 "adding the same reaction twice is idempotent: 200 and the reactions do not change" "got status $s, first '$r1', second '$r2'" [ "$s" = 200 -a "$r1" = "$r2" -a -n "$r2" ]
s=$(call DELETE $N0/api/messages/$MA/reactions/$THUMB_PATH "" "$m1_TOKEN")
has=$(pj "'\\U0001F44D' in json.dumps(d['reactions'], ensure_ascii=False)")
pass_if B2.29 "removing a reaction returns 200 {reactions} without the emoji" "got status $s, emoji still present '$has'" [ "$s" = 200 -a "$has" = False ]

if [ $((SECONDS-START)) -ge $BUDGET ]; then
  echo "NOTE: the time limit was reached; later checks may show 'got 000'. Some route is slow or hangs (no answer within 3 seconds): fix the first 'got 000' first."
fi
echo "RESULT: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
RG_B_2

##### check B.3 #####
cat > "$W/b_3.sh" <<'RG_B_3'
#!/usr/bin/env bash
# Self-check for stage B.3 (search, pins and mentions).
# Part of /spec/checks/regression.sh. Exit 0 only if every rule passes.
set -u
FEEDBACK=verbose
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

mkchan "$owner_TOKEN" "c$RUN-p" ',"is_private":true'; CH_PRIV=$CID
TOK="zq$RUN"
mkchan "$owner_TOKEN" "c$RUN-s"; CH_S1=$CID
mkchan "$owner_TOKEN" "c$RUN-sp" ',"is_private":true'; CH_SP=$CID
mkchan "$owner_TOKEN" "c$RUN-w2" "" "$SLUG2"; CH_W2=$CID
post $CH_S1 "$TOK alpha" "$owner_TOKEN" >/dev/null; post $CH_S1 "$TOK beta" "$owner_TOKEN" >/dev/null; post $CH_S1 "$TOK gamma" "$owner_TOKEN" >/dev/null
post $CH_SP "$TOK secret" "$owner_TOKEN" >/dev/null; post $CH_W2 "$TOK otherws" "$owner_TOKEN" >/dev/null
SW='",".join(next((w for w in ["alpha","beta","gamma","secret","otherws"] if w in json.dumps(i)),"?") for i in d["results"])'
s=$(call GET "$N0/api/search?q=$TOK" "" "$owner_TOKEN"); w=$(pj "$SW"); hk=$(pj '"next_cursor" in d')
pass_if B3.1 "search returns matches newest-first" "got status $s, matches '$w' (expected otherws,secret,gamma,beta,alpha), has next_cursor '$hk'" [ "$s" = 200 -a "$w" = "otherws,secret,gamma,beta,alpha" -a "$hk" = True ]
s=$(call GET "$N0/api/search?q=$TOK" "" "$m2_TOKEN"); w=$(pj "$SW")
pass_if B3.2 "search only covers channels the caller can access" "got status $s, matches '$w' for a user outside the private channel and the other workspace (expected neither 'secret' nor 'otherws')" \
  [ "$s" = 200 -a "$(pj "'secret' not in '$w' and 'otherws' not in '$w'")" = True ]
s=$(call GET "$N0/api/search?q=$TOK&workspace_id=$WID" "" "$owner_TOKEN"); w1=$(pj "$SW")
s2=$(call GET "$N0/api/search?q=$TOK&workspace_id=$WID2" "" "$owner_TOKEN"); w2=$(pj "$SW")
pass_if B3.3 "workspace_id limits search to one workspace" "workspace $WID gave '$w1' (expected secret,gamma,beta,alpha), workspace $WID2 gave '$w2' (expected otherws)" \
  [ "$s" = 200 -a "$s2" = 200 -a "$w1" = "secret,gamma,beta,alpha" -a "$w2" = otherws ]

mkchan "$owner_TOKEN" "c$RUN-pn"; CH_PIN=$CID
for i in 1 2 3; do post $CH_PIN pin-$i "$owner_TOKEN" >/dev/null; eval "P$i=$(field message.id)"; done
s=$(call POST $N0/api/messages/$P1/pin '{}' "$owner_TOKEN")
f=$(pj "str(d['pin']['message_id'])=='$P1' and str(d['pin']['pinned_by'])=='$owner_ID' and bool(d['pin']['pinned_at'])")
pass_if B3.4 "pin a message" "got status $s, pin fields ok '$f'" [ "$s" = 200 -a "$f" = True ]
s=$(call POST $N0/api/messages/$P1/pin '{}' "$owner_TOKEN")
call GET $N0/api/channels/$CH_PIN/pins "" "$owner_TOKEN" >/dev/null
pass_if B3.5 "pinning twice is idempotent" "second pin got status $s, pins listed: $(pj 'len(d["pins"])') (expected 1)" [ "$s" = 200 -a "$(pj 'len(d["pins"])')" = 1 ]
call POST $N0/api/messages/$P2/pin '{}' "$owner_TOKEN" >/dev/null; call POST $N0/api/messages/$P3/pin '{}' "$owner_TOKEN" >/dev/null
s=$(call GET $N0/api/channels/$CH_PIN/pins "" "$owner_TOKEN")
ids=$(pj '",".join(str(p["message"]["id"]) for p in d["pins"])'); f=$(pj 'all("pinned_by" in p and "pinned_at" in p for p in d["pins"]) and "next_cursor" in d')
pass_if B3.6 "pins are listed newest-first" "got status $s, pinned message ids '$ids' (expected $P3,$P2,$P1), entry fields ok '$f'" [ "$s" = 200 -a "$ids" = "$P3,$P2,$P1" -a "$f" = True ]
s=$(call DELETE $N0/api/messages/$P2/pin "" "$owner_TOKEN"); u=$(pj 'd.get("unpinned")')
call GET $N0/api/channels/$CH_PIN/pins "" "$owner_TOKEN" >/dev/null
pass_if B3.7 "unpin a message" "got status $s, unpinned '$u', pins now '$(pj '",".join(str(p["message"]["id"]) for p in d["pins"])')' (expected $P3,$P1)" \
  [ "$s" = 200 -a "$u" = True -a "$(pj '",".join(str(p["message"]["id"]) for p in d["pins"])')" = "$P3,$P1" ]
s=$(call GET $N0/api/channels/$CH_PRIV/pins "" "$m2_TOKEN")
pass_if B3.8 "a non-member lists the pins of a private channel" "expected 403, got $s" [ "$s" = 403 ]

########## MENTIONS ##########
mkchan "$owner_TOKEN" "c$RUN-mn"; CH_MEN=$CID
for n in m1 m2; do tv="${n}_TOKEN"; join $CH_MEN "${!tv}" >/dev/null; done
MEN='",".join(sorted(str(x) for x in d["message"]["mentions"]))'
s=$(post $CH_MEN "hi @$m1_NAME" "$owner_TOKEN"); m=$(pj "$MEN")
pass_if B3.9 "@username resolves to that user's id" "got status $s, mentions '$m' (expected $m1_ID)" [ "$s" = 201 -a "$m" = "$m1_ID" ]
s=$(post $CH_MEN "hi @$out1_NAME" "$owner_TOKEN"); m=$(pj "$MEN")
pass_if B3.10 "a user outside the workspace is not mentioned" "got status $s, mentions '$m' (expected none)" [ "$s" = 201 -a -z "$m" ]
s1=$(post $CH_MEN "mail someone@$m1_NAME.com" "$owner_TOKEN"); m1x=$(pj "$MEN")
s2=$(post $CH_MEN "hello@$m1_NAME" "$owner_TOKEN"); m2x=$(pj "$MEN")
pass_if B3.11 "email-shaped and in-word mentions do not match" "email-shaped gave '$m1x', in-word gave '$m2x' (expected none)" [ "$s1" = 201 -a "$s2" = 201 -a -z "$m1x" -a -z "$m2x" ]
s=$(post $CH_MEN "@$owner_NAME note to self" "$owner_TOKEN"); m=$(pj "$MEN")
pass_if B3.12 "a self-mention is not included" "got status $s, mentions '$m' (expected none)" [ "$s" = 201 -a -z "$m" ]
s=$(post $CH_MEN "@$m1_NAME and again @$m1_NAME" "$owner_TOKEN"); m=$(pj "$MEN")
pass_if B3.13 "repeated mentions are deduplicated" "got status $s, mentions '$m' (expected $m1_ID once)" [ "$s" = 201 -a "$m" = "$m1_ID" ]
exp=$(printf '%s\n%s\n' "$m1_ID" "$m2_ID" | sort | paste -sd, -)
s=$(post $CH_MEN "@channel everyone" "$owner_TOKEN"); m=$(pj "$MEN")
s2=$(post $CH_MEN "@channel @$m1_NAME @$m2_NAME" "$owner_TOKEN"); m2x=$(pj "$MEN")
pass_if B3.14 "@channel mentions every channel member except the author, without duplicates" "got '$m' and '$m2x' (expected $exp for both; the author $owner_ID must not appear)" [ "$s" = 201 -a "$s2" = 201 -a "$m" = "$exp" -a "$m2x" = "$exp" ]
mkchan "$owner_TOKEN" "c$RUN-ct"; CH_CNT=$CID; join $CH_CNT "$m1_TOKEN" >/dev/null
post $CH_CNT "@$m1_NAME ping" "$owner_TOKEN" >/dev/null
s=$(call GET $N0/api/channels/$CH_CNT/read "" "$m1_TOKEN"); mc=$(pj 'd["read_state"]["mention_count"]')
pass_if B3.15 "a mention raises the mentioned user's mention_count" "got status $s, mention_count '$mc' (expected 1)" [ "$s" = 200 -a "$mc" = 1 ]
post $CH_CNT "@$owner_NAME self" "$owner_TOKEN" >/dev/null
s=$(call GET $N0/api/channels/$CH_CNT/read "" "$owner_TOKEN"); mc=$(pj 'd["read_state"]["mention_count"]')
pass_if B3.16 "mentioning yourself does not raise your own mention_count" "got status $s, mention_count '$mc' (expected 0)" [ "$s" = 200 -a "$mc" = 0 ]

s=$(call GET "$N0/api/search?q=$TOK&limit=2" "" "$owner_TOKEN"); w1=$(pj "$SW"); cr=$(pj 'd["next_cursor"]')
s2=$(call GET "$N0/api/search?q=$TOK&limit=2&cursor=$(qs "$cr")" "" "$owner_TOKEN"); w2=$(pj "$SW")
pass_if B3.17 "search accepts limit and cursor (newest-first pages)" "page 1 got $s '$w1' (expected otherws,secret), page 2 got $s2 '$w2' (expected gamma,beta)" \
  [ "$s" = 200 -a "$w1" = "otherws,secret" -a "$s2" = 200 -a "$w2" = "gamma,beta" ]
PINS='",".join(str(p["message"]["id"]) for p in d["pins"])'
s=$(call GET "$N0/api/channels/$CH_PIN/pins?limit=1" "" "$owner_TOKEN"); p1=$(pj "$PINS"); cr=$(pj 'd["next_cursor"]')
s2=$(call GET "$N0/api/channels/$CH_PIN/pins?limit=1&cursor=$(qs "$cr")" "" "$owner_TOKEN"); p2=$(pj "$PINS"); nc=$(pj 'd["next_cursor"]')
pass_if B3.18 "pins accept limit and cursor (newest-first pages)" "page 1 got $s '$p1' (expected $P3), page 2 got $s2 '$p2' (expected $P1) with next_cursor '$nc' (expected None)" \
  [ "$s" = 200 -a "$p1" = "$P3" -a "$s2" = 200 -a "$p2" = "$P1" -a "$nc" = None ]

if [ $((SECONDS-START)) -ge $BUDGET ]; then
  echo "NOTE: the time limit was reached; later checks may show 'got 000'. Some route is slow or hangs (no answer within 3 seconds): fix the first 'got 000' first."
fi
echo "RESULT: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
RG_B_3

##### check B.4 #####
cat > "$W/b_4.sh" <<'RG_B_4'
#!/usr/bin/env bash
# Self-check for stage B.4 (channel events, read state and API conventions).
# Part of /spec/checks/regression.sh. Exit 0 only if every rule passes.
set -u
FEEDBACK=verbose
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
RG_B_4

##### check Boot #####
cat > "$W/boot.sh" <<'RG_BOOT'
#!/usr/bin/env bash
# Baseline boot check (conditions 1 to 3 of the four milestone 0 boot conditions).
# Part of /spec/checks/regression.sh. Exit 0 only if every rule passes.
set -u
FEEDBACK=verbose
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
RG_BOOT

CHECKS="a_1 a_2 a_3 b_1 b_2 b_3 b_4 boot"
for n in $CHECKS; do
  sed -i "s#/tmp/check_#$W/${n}_#g" "$W/$n.sh"
  ( timeout 27 bash "$W/$n.sh" > "$W/$n.out" 2>&1 ) &
done
wait

broken=0; rules=0; unfinished=0
for n in $CHECKS; do
  label=$(echo "$n" | tr 'a-z_' 'A-Z.'); [ "$n" = boot ] && label=Boot
  res=$(grep '^RESULT:' "$W/$n.out" | tail -1)
  grep '^FAIL' "$W/$n.out" | sed "s/^/[$label] /"
  if [ -z "$res" ]; then
    echo "[$label] no RESULT line (the check was cut off or crashed)"; unfinished=$((unfinished+1))
  else
    p=$(echo "$res" | sed -E 's/.*: ([0-9]+) passed, ([0-9]+) failed.*/\1/')
    f=$(echo "$res" | sed -E 's/.*: ([0-9]+) passed, ([0-9]+) failed.*/\2/')
    echo "[$label] $res"
    rules=$((rules+p+f)); broken=$((broken+f))
  fi
done
rm -rf "$W"
if [ "$broken" -eq 0 ] && [ "$unfinished" -eq 0 ]; then
  echo "REGRESSION: A and B: 0 broken ($rules rules pass)"; exit 0
fi
echo "REGRESSION: A and B: $broken broken, $unfinished checks did not finish"; exit 1
