#!/bin/sh
# Dead-man's-switch heartbeat: report watcher liveness to the witness host.
# Runs on the writer server (f5server) once per hour.
# Required environment:
#   WATCHDOG_TARGET   scp destination, e.g. epic@100.119.115.55
#   WATCHDOG_PATH     remote directory, default ~/watchdog
set -eu

JOURNAL="journalctl --user -u alditalk-refill-server --no-pager -o short-iso"

SERVICE_ACTIVE=$(systemctl --user is-active alditalk-refill-server.service || true)

LAST_LINE=$($JOURNAL | grep -E "GB remaining|Booked .* verified" | tail -1 || true)
LAST_ERROR=$($JOURNAL | grep -E "\] Error \(" | tail -1 || true)
if [ -n "$LAST_LINE" ]; then
    LAST_TS=$(echo "$LAST_LINE" | cut -c1-19)
    REMAINING=$(echo "$LAST_LINE" | grep -oE "[0-9]+\.[0-9]+ GB remaining" | grep -oE "[0-9]+\.[0-9]+" || echo "null")
else
    LAST_TS=""
    REMAINING="null"
fi

MINUTES_SINCE=null
if [ -n "$LAST_TS" ]; then
    MINUTES_SINCE=$(python3 -c "
from datetime import datetime, timezone
import sys
fmt='%Y-%m-%dT%H:%M:%S'
try:
    then=datetime.strptime('$LAST_TS', fmt).replace(tzinfo=timezone.utc)
    print(int((datetime.now(timezone.utc)-then).total_seconds()//60))
except Exception:
    print('null')")
fi

BOOKINGS_TODAY=$($JOURNAL --since today | grep -c "verified the new balance" || true)

ERROR_MESSAGE=None
ERROR_TS=""
if [ -n "$LAST_ERROR" ]; then
    ERROR_TS=$(echo "$LAST_ERROR" | cut -c1-19)
fi
if [ -n "$LAST_ERROR" ] && { [ -z "$LAST_TS" ] || [ "$ERROR_TS" \> "$LAST_TS" ]; }; then
    ERROR_MESSAGE=$(echo "$LAST_ERROR" | sed -E 's/^.{20}.*\] Error \((.*)\); backing off .*/\1/' | python3 -c '
import json, sys
print(json.dumps(sys.stdin.read().strip()))')
fi

PAYLOAD=$(python3 -c "
import json, time
print(json.dumps({
    'ts': int(time.time()),
    'service_active': '$SERVICE_ACTIVE' == 'active',
    'remaining_gb': $REMAINING,
    'minutes_since_cycle': $MINUTES_SINCE,
    'bookings_today': $BOOKINGS_TODAY,
    'last_error': $ERROR_MESSAGE,
}))")

TARGET="${WATCHDOG_TARGET:?WATCHDOG_TARGET not set}"
REMOTE_DIR="${WATCHDOG_PATH:-watchdog}"
echo "$PAYLOAD" | ssh -o ConnectTimeout=20 -o BatchMode=yes "$TARGET" \
    "mkdir -p '$REMOTE_DIR' && cat > '$REMOTE_DIR/heartbeat.json'"
echo "$(date -Is) heartbeat pushed: $PAYLOAD"
