#!/usr/bin/env bash
# Self-check for stage A.3 (invitations and join mode). Prints PASS/FAIL per rule in /spec/a/a-3.md.
# Usage: bash /spec/a/checks/check_a_3.sh      Exit 0 only if every rule passes.
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
