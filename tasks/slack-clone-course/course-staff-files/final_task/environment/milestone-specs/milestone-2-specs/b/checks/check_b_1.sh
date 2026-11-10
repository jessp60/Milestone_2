#!/usr/bin/env bash
# Self-check for stage B.1 (channel management). Prints PASS/FAIL per rule in /spec/b/b-1.md.
# Usage: bash /spec/b/checks/check_b_1.sh      Exit 0 only if every rule passes.
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
