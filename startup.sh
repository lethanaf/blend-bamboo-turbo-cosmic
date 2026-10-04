#!/bin/sh
# Restart the review download page. Idempotent.
set -eu
if curl -sf -o /dev/null --max-time 1 http://127.0.0.1:8080/; then
  exit 0
fi
mkdir -p /workspace/review
/usr/bin/python3.11 /workspace/polymarket_bot/scripts/package_review.py
nohup /usr/bin/python3.11 /workspace/review/server.py >> /workspace/review/server.log 2>&1 &
i=0
while [ "$i" -lt 20 ]; do
  if curl -sf -o /dev/null --max-time 1 http://127.0.0.1:8080/; then
    exit 0
  fi
  i=$((i + 1))
  sleep 0.3
done
exit 1
