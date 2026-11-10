#!/usr/bin/env bash
# Self-check for stage A.2 (roles, role-based channel permissions, ownership transfer). Prints PASS/FAIL per rule in /spec/a/a-2.md.
# Usage: bash /spec/a/checks/check_a_2.sh      Exit 0 only if every rule passes.
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
