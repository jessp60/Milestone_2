#!/bin/sh
mkdir -p /app
python3 /app/app.py >/tmp/huddle-8000.log 2>&1 &
PORT=8001 python3 /app/app.py >/tmp/huddle-8001.log 2>&1 &
PORT=8002 python3 /app/app.py >/tmp/huddle-8002.log 2>&1 &
wait
