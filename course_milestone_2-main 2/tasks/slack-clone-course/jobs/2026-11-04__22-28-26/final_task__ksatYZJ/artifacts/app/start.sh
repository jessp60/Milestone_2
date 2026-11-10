#!/bin/sh
set -e
mkdir -p /app
python3 /app/app.py 8000 &
p1=$!
python3 /app/app.py 8001 &
p2=$!
python3 /app/app.py 8002 &
p3=$!
trap 'kill $p1 $p2 $p3 2>/dev/null || true' TERM INT EXIT
wait $p1 $p2 $p3
