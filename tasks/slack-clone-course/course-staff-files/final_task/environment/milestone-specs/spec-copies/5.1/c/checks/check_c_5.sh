#!/usr/bin/env bash
# Check for C.5 (user groups and group mentions). Prints PASS/FAIL per rule in /spec/c/c-5.md.
# Usage: bash /spec/c/checks/check_c_5.sh      Exit 0 only if every rule passes.
set -u
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

G=/api/workspaces
# mcount <token> <channel-id>: prints the caller's mention_count in that channel
mcount() { call GET $N1/api/channels/$2/read "" "$1" >/dev/null; pj 'd["read_state"]["mention_count"]'; }
# sorted ids of the mentions list of the last posted message
ment() { pj "sorted(d['message']['mentions'])"; }

rules_c_5() {
  local s all="5.1 5.2 5.3 5.4 5.5 5.6 5.7 5.8 5.9 5.10 5.11 5.12 5.13 5.14 5.15 5.16 5.17 5.18 5.19"
  setup_pair && mkuser m2 && mkuser m3 && mkuser out && mkchan "$owner_TOKEN" "grp$RUN" || { fail_all $all; return; }
  CH=$CID
  [ "$(join $CH "$m1_TOKEN")" = 200 ] && [ "$(join $CH "$m2_TOKEN")" = 200 ] || { fail_all $all; return; }
  # m3 is a workspace member (joined #general) but is not in the channel $CH
  [ "$(join $GID "$m3_TOKEN")" = 200 ] || { fail_all $all; return; }

  s=$(call POST $N0$G/$SLUG/groups "{\"handle\":\"crew\",\"name\":\"Crew\",\"user_ids\":[$m1_ID,$m2_ID]}" "$m1_TOKEN")
  pass_if 5.1 "a member creates a group: 201 {group} with handle, name and member_user_ids" "status $s, body $(head -c 250 $BODY)" \
    [ "$s" = 201 -a "$(pj "d['group']['handle']=='crew' and d['group']['name']=='Crew' and sorted(d['group']['member_user_ids'])==sorted([$m1_ID,$m2_ID])")" = True ]
  call GET $N1$G/$SLUG/groups "" "$m2_TOKEN" >/dev/null
  pass_if 5.2 "GET groups returns 200 {groups} including the group" "body $(head -c 250 $BODY)" [ "$(pj "[g['handle'] for g in d['groups']]")" = "['crew']" ]
  s=$(call POST $N0$G/$SLUG/groups '{"handle":"nope","name":"Nope","user_ids":[]}' "$out_TOKEN"); expect 5.3 "a non-member creating a group" 403 "$s"
  s=$(call POST $N2$G/$SLUG/groups "{\"handle\":\"crew\",\"name\":\"Again\",\"user_ids\":[$m1_ID]}" "$m2_TOKEN"); expect 5.4 "duplicate handle" 409 "$s"

  s=$(call PATCH $N0$G/$SLUG/groups/crew "{\"name\":\"Eng\",\"user_ids\":[$m1_ID]}" "$m1_TOKEN")
  pass_if 5.5 "the creator updates name and members: 200 and the updated group" "status $s, body $(head -c 250 $BODY)" \
    [ "$s" = 200 -a "$(pj "d['group']['name']+'|'+str(d['group']['member_user_ids'])")" = "Eng|[$m1_ID]" ]
  s=$(call PATCH $N1$G/$SLUG/groups/crew '{"name":"ByOwner"}' "$owner_TOKEN")
  pass_if 5.6 "the workspace owner may update another user's group" "status $s, name '$(field group.name)'" [ "$s" = 200 -a "$(field group.name)" = ByOwner ]
  s=$(call PATCH $N0$G/$SLUG/groups/crew '{"name":"Hijack"}' "$m2_TOKEN")
  call GET $N0$G/$SLUG/groups "" "$m1_TOKEN" >/dev/null
  pass_if 5.7 "another plain member cannot update (403) and the group is unchanged" "status $s, name '$(pj "d['groups'][0]['name']")'" [ "$s" = 403 -a "$(pj "d['groups'][0]['name']")" = ByOwner ]
  s=$(call PATCH $N0$G/$SLUG/groups/nosuch '{"name":"x"}' "$owner_TOKEN"); expect 5.8 "update of an unknown handle" 404 "$s"

  s=$(call DELETE $N0$G/$SLUG/groups/crew "" "$m1_TOKEN"); d1=$(field deleted)
  call GET $N0$G/$SLUG/groups "" "$m1_TOKEN" >/dev/null
  pass_if 5.9 "the creator deletes the group: 200 {\"deleted\":true} and it leaves the list" "status $s, deleted '$d1', handles $(pj "[g['handle'] for g in d['groups']]")" \
    [ "$s" = 200 -a "$d1" = True -a "$(pj "'crew' not in [g['handle'] for g in d['groups']]")" = True ]
  s2=$(call DELETE $N1$G/$SLUG/groups/crew "" "$m1_TOKEN")
  pass_if 5.10 "deleting again is idempotent: 200 {\"deleted\":true}" "status $s2, body $(head -c 100 $BODY)" [ "$s2" = 200 -a "$(field deleted)" = True ]
  call POST $N0$G/$SLUG/groups "{\"handle\":\"keep\",\"name\":\"Keep\",\"user_ids\":[$m1_ID]}" "$m1_TOKEN" >/dev/null
  s=$(call DELETE $N0$G/$SLUG/groups/keep "" "$m2_TOKEN")
  call GET $N0$G/$SLUG/groups "" "$m1_TOKEN" >/dev/null
  pass_if 5.11 "another plain member cannot delete (403) and the group stays" "status $s, handles $(pj "[g['handle'] for g in d['groups']]")" \
    [ "$s" = 403 -a "$(pj "'keep' in [g['handle'] for g in d['groups']]")" = True ]

  # mention tests: group "eng" holds owner, m1, m2 and m3; m3 is not in the channel
  call POST $N0$G/$SLUG/groups "{\"handle\":\"eng\",\"name\":\"Eng\",\"user_ids\":[$owner_ID,$m1_ID,$m2_ID,$m3_ID]}" "$m1_TOKEN" >/dev/null
  b_m1=$(mcount "$m1_TOKEN" $CH); b_m2=$(mcount "$m2_TOKEN" $CH); b_ow=$(mcount "$owner_TOKEN" $CH)
  s=$(post $CH "hello @eng please look" "$owner_TOKEN"); mm=$(ment)
  pass_if 5.12 "@handle mentions the group members who are in the channel" "status $s, mentions $mm" [ "$s" = 201 -a "$(pj "sorted([$m1_ID,$m2_ID])==sorted(d['message']['mentions'])")" = True ]
  pass_if 5.13 "a group member who is not in the channel is not mentioned" "mentions $mm, m3 is $m3_ID" [ "$(pj "$m3_ID not in d['message']['mentions']")" = True ]
  pass_if 5.14 "the author is not mentioned even though a group member" "mentions $mm, author is $owner_ID" [ "$(pj "$owner_ID not in d['message']['mentions']")" = True ]
  a_m1=$(mcount "$m1_TOKEN" $CH); a_ow=$(mcount "$owner_TOKEN" $CH)
  pass_if 5.16 "a mentioned member's mention_count rises by 1; the author's does not rise" "m1 $b_m1 -> $a_m1, author $b_ow -> $a_ow" \
    [ "$a_m1" = "$((b_m1+1))" -a "$a_ow" = "$b_ow" ]
  s=$(post $CH "again @eng and @$m1_NAME" "$owner_TOKEN")
  pass_if 5.15 "ids from @handle and @username are deduplicated" "mentions $(ment)" \
    [ "$s" = 201 -a "$(pj "sorted(d['message']['mentions'])==sorted([$m1_ID,$m2_ID])")" = True ]

  wsrun 2 "$m1_TOKEN" $N1 'act("POST","/api/channels/'"$CH"'/messages",{"body":"@here ping"},"'"$owner_TOKEN"'")'
  call GET $N0/api/channels/$CH/messages "" "$owner_TOKEN" >/dev/null
  hm=$(pj "[sorted(m['mentions']) for m in d['messages'] if m['body']=='@here ping']")
  pass_if 5.17 "@here mentions the channel member who has an open WebSocket" "mentions $hm, expected [[$m1_ID]]" [ "$hm" = "[[$m1_ID]]" ]
  pass_if 5.18 "@here leaves out the member without a WebSocket and the author" "mentions $hm (m2 $m2_ID offline, author $owner_ID)" \
    [ "$(pj "[($m2_ID in m['mentions']) or ($owner_ID in m['mentions']) for m in d['messages'] if m['body']=='@here ping']")" = "[False]" ]

  b=$(mcount "$m1_TOKEN" $CH)
  s=$(post $CH "note to self @$m1_NAME" "$m1_TOKEN"); mm=$(ment); a=$(mcount "$m1_TOKEN" $CH)
  pass_if 5.19 "a self mention is not listed and does not raise one's own mention_count" "status $s, mentions $mm, count $b -> $a" \
    [ "$s" = 201 -a "$mm" = "[]" -a "$a" = "$b" ]
}

boot; rules_c_5; finish
