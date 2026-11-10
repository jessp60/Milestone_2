#!/usr/bin/env bash
# Check for C.3 (files). Prints PASS/FAIL per rule in /spec/c/c-3.md.
# Usage: bash /spec/c/checks/check_c_3.sh      Exit 0 only if every rule passes.
set -u
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

rules_c_3() {
  local s T=/tmp/check_c_f_$RUN
  setup_pair || { fail_all 3.1 3.2 3.3 3.4 3.5 3.6 3.7 3.8 3.9 3.10 3.11 3.12 3.13 3.14 3.15 3.16; return; }
  printf 'hello file %s\n' "$RUN" > $T.txt
  FF='["id","uploader_id","filename","content_type","size","created_at"]'

  s=$(upload "$owner_TOKEN" $T.txt notes.txt text/plain); F1=$(field file.id)
  pass_if 3.1 "upload returns 201 {file} with all FileObj fields that match the upload" "status $s, body $(head -c 250 $BODY)" \
    [ "$s" = 201 -a "$(pj "all(k in d['file'] for k in $FF) and d['file']['filename']=='notes.txt' and d['file']['content_type']=='text/plain' and d['file']['size']==$(wc -c < $T.txt) and d['file']['uploader_id']==$owner_ID")" = True ]
  s=$(call GET $N1/api/files/$F1 "" "$m1_TOKEN")
  pass_if 3.2 "GET /api/files/{id} on another node returns 200 {file}" "status $s, body $(head -c 200 $BODY)" [ "$s" = 200 -a "$(field file.id)" = "$F1" -a "$(field file.filename)" = notes.txt ]
  s=$(call GET $N0/api/files/99999999 "" "$owner_TOKEN"); expect 3.3 "unknown file" 404 "$s"

  curl -s -D $T.h -o $T.dl --max-time 5 -H "Authorization: Bearer $owner_TOKEN" $N0/api/files/$F1/download
  pass_if 3.4 "download returns exactly the uploaded bytes" "bytes differ or no answer" cmp -s $T.txt $T.dl
  pass_if 3.5 "download has the matching Content-Type and a Content-Disposition with the filename" "headers: $(tr -d '\r' < $T.h | grep -i '^content-' | head -c 250)" \
    bash -c "grep -qi '^content-type: *text/plain' $T.h && grep -i '^content-disposition:' $T.h | grep -q notes.txt"
  rm -f $T.dl; curl -s -o $T.dl --max-time 5 -H "Authorization: Bearer $m1_TOKEN" $N2/api/files/$F1/download
  pass_if 3.6 "download works on a different node" "bytes differ or no answer" cmp -s $T.txt $T.dl
  s=$(call GET $N0/api/files/99999999/download "" "$owner_TOKEN"); expect 3.7 "download of an unknown file" 404 "$s"

  head -c 10485761 /dev/zero > $T.big; head -c 10485760 /dev/zero > $T.max
  s=$(upload "$owner_TOKEN" $T.big big.bin application/octet-stream); s2=$(upload "$owner_TOKEN" $T.max max.bin application/octet-stream)
  pass_if 3.8 "10 MiB + 1 byte returns 413; exactly 10 MiB returns 201" "got $s and $s2" [ "$s" = 413 -a "$s2" = 201 ]
  s=$(upload "" $T.txt notes.txt text/plain); expect 3.9 "upload without token" 401 "$s"
  s=$(curl -s -o "$BODY" -w '%{http_code}' --max-time 5 -X POST $N0/api/files -H "Authorization: Bearer $owner_TOKEN" -F "other=abc"); expect 3.10 "multipart without a file part" 400 "$s"

  s=$(call POST $N0/api/channels/$GID/messages "{\"body\":\"here\",\"file_ids\":[$F1]}" "$owner_TOKEN")
  pass_if 3.11 "attaching an own file returns 201 and message.files lists it" "status $s, body $(head -c 250 $BODY)" \
    [ "$s" = 201 -a "$(pj "[f['id'] for f in d['message']['files']]")" = "[$F1]" ]
  call GET $N1/api/channels/$GID/messages "" "$owner_TOKEN" >/dev/null
  pass_if 3.12 "the message listing shows the file" "body $(head -c 250 $BODY)" [ "$(pj "[f['id'] for m in d['messages'] for f in m['files']]")" = "[$F1]" ]

  call GET $N0/api/channels/$GID/messages "" "$owner_TOKEN" >/dev/null; before=$(pj "len(d['messages'])")
  upload "$owner_TOKEN" $T.txt other.txt text/plain >/dev/null; F2=$(field file.id)
  s=$(call POST $N0/api/channels/$GID/messages "{\"body\":\"stolen\",\"file_ids\":[$F2]}" "$m1_TOKEN"); expect 3.13 "attaching another user's file" 400 "$s"
  s=$(call POST $N0/api/channels/$GID/messages "{\"body\":\"again\",\"file_ids\":[$F1]}" "$owner_TOKEN"); expect 3.14 "attaching an already attached file" 400 "$s"
  s=$(call POST $N0/api/channels/$GID/messages '{"body":"ghost","file_ids":[99999999]}' "$owner_TOKEN"); expect 3.15 "attaching an unknown file id" 400 "$s"
  call GET $N0/api/channels/$GID/messages "" "$owner_TOKEN" >/dev/null
  pass_if 3.16 "no message was posted by the three rejected attaches" "messages before $before, after $(pj "len(d['messages'])")" [ "$(pj "len(d['messages'])")" = "$before" ]
  rm -f $T.*
}

boot; rules_c_3; finish
