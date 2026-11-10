#!/usr/bin/env bash
# Check for C.4 (slash commands). Prints PASS/FAIL per rule in /spec/c/c-4.md.
# Usage: bash /spec/c/checks/check_c_4.sh      Exit 0 only if every rule passes.
set -u
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

# chan_field <token> <channel-id> <python-expression on c>: reads a channel from the workspace channel list
chan_field() {
  call GET $N1/api/workspaces/$SLUG/channels "" "$1" >/dev/null
  pj "[$3 for c in d['channels'] if c['id']==$2]" | head -c 200
}

rules_c_4() {
  local s SHRUG='¯\_(ツ)_/¯'
  setup_pair && mkchan "$owner_TOKEN" "slash$RUN" || { fail_all 4.1 4.2 4.3 4.4 4.5 4.6 4.7 4.8 4.9 4.10 4.11 4.12 4.13; return; }
  CH=$CID; join $CH "$m1_TOKEN" >/dev/null

  s=$(post $CH "/me waves" "$owner_TOKEN")
  pass_if 4.1 "/me waves is posted with body kept as typed" "status $s, body '$(field message.body)'" [ "$s" = 201 -a "$(field message.body)" = "/me waves" ]
  s=$(post $CH "/me" "$owner_TOKEN"); s2=$(post $CH "/me   " "$owner_TOKEN")
  pass_if 4.2 "/me without text returns 400 (also with only spaces)" "got $s and $s2" [ "$s" = 400 -a "$s2" = 400 ]
  s=$(post $CH "/shrug oh well" "$owner_TOKEN")
  pass_if 4.3 "/shrug text appends the emoticon after one space" "status $s, body '$(field message.body)'" [ "$s" = 201 -a "$(field message.body)" = "oh well $SHRUG" ]
  s=$(post $CH "/shrug" "$owner_TOKEN")
  pass_if 4.4 "/shrug alone is just the emoticon" "status $s, body '$(field message.body)'" [ "$s" = 201 -a "$(field message.body)" = "$SHRUG" ]
  s=$(post $CH "/nosuchcommand x" "$owner_TOKEN"); expect 4.5 "unknown command" 400 "$s"
  s=$(post $CH "//literal" "$owner_TOKEN")
  pass_if 4.6 "//literal is posted as a literal message" "status $s, body '$(field message.body)'" [ "$s" = 201 -a "$(field message.body)" = "//literal" ]

  s=$(post $CH "/topic New topic" "$owner_TOKEN"); tp=$(field channel.topic); nl=$(pj "d['message'] is None")
  st=$(chan_field "$owner_TOKEN" $CH "c['topic']")
  pass_if 4.7 "/topic returns 201 with message null and the new channel.topic, and it persists" "status $s, topic '$tp', message null '$nl', stored '$st'" \
    [ "$s" = 201 -a "$nl" = True -a "$tp" = "New topic" -a "$st" = "['New topic']" ]

  mkchan "$owner_TOKEN" "priv$RUN" ',"is_private":true'; PV=$CID
  s=$(post $PV "/invite @$m1_NAME" "$owner_TOKEN"); ju=$(field invited_user.id)
  s2=$(call GET $N0/api/channels/$PV/messages "" "$m1_TOKEN")
  pass_if 4.8 "/invite returns 201 with message null and invited_user, who can then read the channel" "status $s, invited_user '$ju', invited user reads: $s2" \
    [ "$s" = 201 -a "$ju" = "$m1_ID" -a "$s2" = 200 ]
  s=$(post $PV "/invite @nosuchuser$RUN" "$owner_TOKEN"); expect 4.9 "/invite of an unknown user" 404 "$s"

  s=$(post $CH "/archive" "$m1_TOKEN"); s_plain=$s
  st=$(chan_field "$owner_TOKEN" $CH "c['is_archived']")
  pass_if 4.12 "a plain member's /archive returns 403 and the channel stays unarchived" "status $s, is_archived $st" [ "$s" = 403 -a "$st" = "[False]" ]
  s=$(post $CH "/archive" "$owner_TOKEN"); a=$(field channel.is_archived); n=$(pj "d['message'] is None"); s2=$(post $CH "after archive" "$owner_TOKEN")
  pass_if 4.10 "/archive returns 201 with message null and is_archived true, and posting then returns 423" "status $s, is_archived '$a', message null '$n', post after: $s2" \
    [ "$s" = 201 -a "$a" = True -a "$n" = True -a "$s2" = 423 ]
  s=$(post $CH "/unarchive" "$owner_TOKEN"); a=$(field channel.is_archived); n=$(pj "d['message'] is None"); s2=$(post $CH "after unarchive" "$owner_TOKEN")
  pass_if 4.11 "/unarchive returns 201 with message null and is_archived false, and posting works again" "status $s, is_archived '$a', message null '$n', post after: $s2" \
    [ "$s" = 201 -a "$a" = False -a "$n" = True -a "$s2" = 201 ]
  call GET $N0/api/channels/$CH/messages "" "$owner_TOKEN" >/dev/null
  pass_if 4.13 "no command body is in the channel's messages" "found: $(pj "[m['body'] for m in d['messages'] if m['body'].startswith('/') and not m['body'].startswith('//') and not m['body'].startswith('/me')]")" \
    [ "$(pj "[m['body'] for m in d['messages'] if m['body'].startswith('/') and not m['body'].startswith('//') and not m['body'].startswith('/me')]")" = "[]" ]
}

boot; rules_c_4; finish
