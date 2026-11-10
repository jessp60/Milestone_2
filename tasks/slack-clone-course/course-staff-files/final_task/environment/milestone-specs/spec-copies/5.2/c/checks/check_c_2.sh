#!/usr/bin/env bash
# Check for C.2 (reactions and direct messages). Prints PASS/FAIL per rule in /spec/c/c-2.md.
# Usage: bash /spec/c/checks/check_c_2.sh      Exit 0 only if every rule passes.
set -u
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

rules_c_2() {
  local s a b
  setup_pair && mkuser out || { fail_all 2.1 2.2 2.3 2.4 2.5 2.6 2.7 2.8 2.9 2.10 2.11 2.12 2.13 2.14 2.15 2.16; return; }
  s=$(post $GID "react to me" "$owner_TOKEN"); M=$(field message.id)
  [ -n "$M" ] || { fail_all 2.1 2.2 2.3 2.4 2.5 2.6 2.7 2.8 2.9 2.10 2.11 2.12 2.13 2.14 2.15 2.16; return; }
  THUMB='{"emoji":"👍"}'; TP=$(qs "👍"); HEART_P=$(qs "❤️")

  s=$(call POST $N0/api/messages/$M/reactions "$THUMB" "$owner_TOKEN")
  pass_if 2.1 "adding a reaction returns 200 {reactions} with count 1 and the caller in user_ids" "status $s, body $(head -c 200 $BODY)" \
    [ "$s" = 200 -a "$(pj "[(r['emoji'],r['count'],r['user_ids']) for r in d['reactions']]")" = "[('👍', 1, [$owner_ID])]" ]
  s=$(call POST $N1/api/messages/$M/reactions "$THUMB" "$owner_TOKEN")
  pass_if 2.2 "the same reaction again is idempotent (200, count still 1)" "status $s, body $(head -c 200 $BODY)" \
    [ "$s" = 200 -a "$(pj "d['reactions'][0]['count']")" = 1 ]
  s=$(call POST $N2/api/messages/$M/reactions "$THUMB" "$m1_TOKEN")
  pass_if 2.3 "a second user raises count to 2 and both ids are in user_ids" "status $s, body $(head -c 200 $BODY)" \
    [ "$s" = 200 -a "$(pj "d['reactions'][0]['count']")" = 2 -a "$(pj "sorted(d['reactions'][0]['user_ids'])==sorted([$owner_ID,$m1_ID])")" = True ]
  s=$(call POST $N0/api/messages/$M/reactions '{"emoji":"❤️"}' "$m1_TOKEN")
  pass_if 2.4 "a different emoji is its own entry" "status $s, body $(head -c 200 $BODY)" [ "$s" = 200 -a "$(pj "len(d['reactions'])")" = 2 ]

  s=$(call DELETE $N0/api/messages/$M/reactions/$TP "" "$owner_TOKEN")
  pass_if 2.5 "removing the caller's reaction returns 200 and keeps the other user's (count 1)" "status $s, body $(head -c 200 $BODY)" \
    [ "$s" = 200 -a "$(pj "[(r['count'],r['user_ids']) for r in d['reactions'] if r['emoji']=='👍']")" = "[(1, [$m1_ID])]" ]
  s=$(call DELETE $N0/api/messages/$M/reactions/$TP "" "$m1_TOKEN")
  pass_if 2.6 "removing the last reaction of an emoji drops its entry" "status $s, body $(head -c 200 $BODY)" \
    [ "$s" = 200 -a "$(pj "[r['emoji'] for r in d['reactions']]")" = "['❤️']" ]
  call GET $N1/api/channels/$GID/messages "" "$owner_TOKEN" >/dev/null
  pass_if 2.7 "the message listing shows the current reactions" "body $(head -c 250 $BODY)" \
    [ "$(pj "[r['emoji'] for m in d['messages'] if m['id']==$M for r in m['reactions']]")" = "['❤️']" ]

  s=$(call POST $N0/api/messages/99999999/reactions "$THUMB" "$owner_TOKEN"); expect 2.8 "reaction on an unknown message" 404 "$s"
  s=$(call POST $N0/api/messages/$M/reactions '{}' "$owner_TOKEN"); expect 2.9 "reaction without emoji" 400 "$s"
  s=$(call POST $N0/api/messages/$M/reactions "$THUMB" ""); expect 2.10 "reaction without token" 401 "$s"

  s=$(call POST $N0/api/dms "{\"user_id\":$m1_ID}" "$owner_TOKEN"); D1=$(field channel_id)
  pass_if 2.11 "POST /api/dms returns 200 {channel_id}" "status $s, body $(head -c 150 $BODY)" [ "$s" = 200 -a -n "$D1" ]
  s=$(call POST $N1/api/dms "{\"user_id\":$owner_ID}" "$m1_TOKEN")
  pass_if 2.12 "the other user gets the same channel_id" "status $s, got '$(field channel_id)', expected '$D1'" [ "$s" = 200 -a "$(field channel_id)" = "$D1" ]
  s=$(call POST $N2/api/dms "{\"user_id\":$m1_ID}" "$owner_TOKEN")
  pass_if 2.13 "repeating the call returns the same channel_id" "status $s, got '$(field channel_id)', expected '$D1'" [ "$s" = 200 -a "$(field channel_id)" = "$D1" ]
  s=$(call POST $N0/api/dms '{"user_id":99999999}' "$owner_TOKEN"); expect 2.14 "DM with an unknown user" 404 "$s"

  a=$(post $D1 "hello dm" "$owner_TOKEN"); b=$(post $D1 "hi back" "$m1_TOKEN")
  call GET $N1/api/channels/$D1/messages "" "$owner_TOKEN" >/dev/null
  n1=$(pj "len(d['messages'])")
  call GET $N2/api/channels/$D1/messages "" "$m1_TOKEN" >/dev/null
  pass_if 2.15 "both users post (201) and see both messages in the DM" "post statuses $a/$b, owner sees $n1, m1 sees $(pj "len(d['messages'])")" \
    [ "$a" = 201 -a "$b" = 201 -a "$n1" = 2 -a "$(pj "len(d['messages'])")" = 2 ]
  s=$(call GET $N0/api/channels/$D1/messages "" "$out_TOKEN"); expect 2.16 "a third user reading the DM" 403 "$s"
}

boot; rules_c_2; finish
