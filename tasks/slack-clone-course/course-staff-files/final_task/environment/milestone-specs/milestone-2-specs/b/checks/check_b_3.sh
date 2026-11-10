#!/usr/bin/env bash
# Self-check for stage B.3 (search, pins and mentions). Prints PASS/FAIL per rule in /spec/b/b-3.md.
# Usage: bash /spec/b/checks/check_b_3.sh      Exit 0 only if every rule passes.
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
