#!/usr/bin/env bash
# Check for C.1 (profiles). Prints PASS/FAIL per rule in /spec/c/c-1.md.
# Usage: bash /spec/c/checks/check_c_1.sh      Exit 0 only if every rule passes.
set -u
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

rules_c_1() {
  local s
  setup_pair || { fail_all 1.1 1.2 1.3 1.4 1.5 1.6 1.7 1.8 1.9 1.10; return; }
  UF='["id","username","display_name","timezone","avatar_url","status_text","status_emoji"]'

  s=$(call GET $N0/api/users/$owner_ID "" "$m1_TOKEN")
  pass_if 1.1 "GET /api/users/{id} returns 200 {user} with all UserObj fields" "status $s, body $(head -c 150 $BODY)" \
    [ "$s" = 200 -a "$(pj "all(k in d['user'] for k in $UF) and d['user']['id']==$owner_ID")" = True ]
  s=$(call GET $N0/api/users/99999999 "" "$m1_TOKEN"); expect 1.2 "unknown user id" 404 "$s"
  s=$(call GET $N0/api/users/$owner_ID ""); expect 1.3 "read without token" 401 "$s"

  s=$(call PATCH $N0/api/users/me '{"display_name":"New Name","status_text":"away"}' "$owner_TOKEN")
  a=$(pj "d['user']['display_name']+'|'+d['user']['status_text']")
  s2=$(call GET $N2/api/users/$owner_ID "" "$m1_TOKEN"); b=$(pj "d['user']['display_name']+'|'+d['user']['status_text']")
  pass_if 1.4 "PATCH users/me returns 200 {user} and the change is visible on another node" "status $s, got '$a', other node '$b'" \
    [ "$s" = 200 -a "$a" = "New Name|away" -a "$b" = "New Name|away" ]

  s=$(call PATCH $N0/api/users/me '{"status_emoji":":tada:"}' "$owner_TOKEN")
  pass_if 1.5 "patching one field leaves the others unchanged" "status $s, body $(head -c 200 $BODY)" \
    [ "$s" = 200 -a "$(pj "d['user']['display_name']+'|'+d['user']['status_text']+'|'+d['user']['status_emoji']")" = "New Name|away|:tada:" ]

  s=$(call PATCH $N0/api/users/me '{"timezone":"America/New_York"}' "$owner_TOKEN")
  pass_if 1.6 "valid timezone is accepted and returned" "status $s, timezone '$(field user.timezone)'" [ "$s" = 200 -a "$(field user.timezone)" = America/New_York ]
  s=$(call PATCH $N0/api/users/me '{"timezone":"Not/AZone"}' "$owner_TOKEN")
  call GET $N0/api/users/$owner_ID "" "$m1_TOKEN" >/dev/null
  pass_if 1.7 "invalid timezone returns 400 and is not stored" "status $s, stored timezone '$(field user.timezone)'" [ "$s" = 400 -a "$(field user.timezone)" = America/New_York ]

  long=$(python3 -c 'print("x"*1000)')
  s=$(call PATCH $N0/api/users/me "{\"display_name\":\"$long\"}" "$owner_TOKEN")
  call GET $N0/api/users/$owner_ID "" "$m1_TOKEN" >/dev/null
  pass_if 1.8 "display_name of 1000 characters returns 400 and is not stored" "status $s, stored '$(field user.display_name | head -c 40)'" [ "$s" = 400 -a "$(field user.display_name)" = "New Name" ]
  s=$(call PATCH $N0/api/users/me '{"display_name":"x"}' ""); expect 1.9 "update without token" 401 "$s"

  wsrun 2 "$m1_TOKEN" $N1 'act("PATCH","/api/users/me",{"status_text":"in a meeting"},"'"$owner_TOKEN"'")'
  pass_if 1.10 "other node's WebSocket gets user.updated with the new user and no channel_id or event_id" \
    "frames: $(wsj "[x.get('type') for x in f]")" \
    [ "$(wsj "any(x.get('type')=='user.updated' and x['user']['id']==$owner_ID and x['user']['status_text']=='in a meeting' and 'channel_id' not in x and 'event_id' not in x for x in f)")" = True ]
}

boot; rules_c_1; finish
