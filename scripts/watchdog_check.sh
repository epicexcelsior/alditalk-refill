#!/bin/sh
# Witness-side check: alert when the writer goes silent or gets stuck.
# Runs on the witness host (music cloud VPS) once per hour.
# Required environment:
#   RESEND_API_KEY   sending key (already present in /etc/music-system/resend.env)
#   WATCHDOG_FROM    sender, e.g. "ALDI watchdog <alerts@mail.epicexcelsior.com>"
#   WATCHDOG_TO      recipient
# Optional:
#   WATCHDOG_DIR     heartbeat directory, default ~/watchdog
#   WATCHDOG_MAX_AGE seconds before silence alerts, default 5400
set -eu

DIR="${WATCHDOG_DIR:-$HOME/watchdog}"
MAX_AGE="${WATCHDOG_MAX_AGE:-5400}"
HEARTBEAT="$DIR/heartbeat.json"

send_mail() {
    SUBJECT=$1
    BODY=$2
    curl -fsS --max-time 20 https://api.resend.com/emails \
        -H "Authorization: Bearer $RESEND_API_KEY" \
        -H 'Content-Type: application/json' \
        -d "$(jq -n --arg from "$WATCHDOG_FROM" --arg to "$WATCHDOG_TO" \
                --arg subject "ALDI refill: $SUBJECT" --arg text "$BODY" \
                '{from:$from,to:[$to],subject:$subject,text:$text}')" >/dev/null
    echo "$(date -Is) ALERT SENT: $SUBJECT"
}

if [ ! -f "$HEARTBEAT" ]; then
    send_mail "no heartbeat file" "The watcher server has never delivered a heartbeat. It is probably down."
    exit 0
fi

AGE=$(( $(date +%s) - $(stat -c %Y "$HEARTBEAT") ))
if [ "$AGE" -gt "$MAX_AGE" ]; then
    send_mail "watcher silent for $((AGE / 60)) minutes" \
        "No heartbeat for $((AGE / 60)) minutes (limit $((MAX_AGE / 60))). The f5server watcher or its network path is down."
    exit 0
fi

SERVICE=$(jq -r '.service_active' "$HEARTBEAT")
REMAINING=$(jq -r '.remaining_gb' "$HEARTBEAT")
MINUTES=$(jq -r '.minutes_since_cycle' "$HEARTBEAT")
BOOKINGS=$(jq -r '.bookings_today' "$HEARTBEAT")

[ "$SERVICE" = "true" ] || send_mail "watcher service not active" \
    "Heartbeat arrived but reports the systemd service is not active."

case "$MINUTES" in
    ''|null|[0-9]*) ;;
    *) MINUTES=null ;;
esac
if [ "$MINUTES" != "null" ] && [ "$MINUTES" -gt 100 ]; then
    send_mail "no check cycle for $MINUTES minutes" \
        "Heartbeat is fresh but the last successful balance read was $MINUTES minutes ago. Chrome or login may be stuck."
fi

IS_NUM=$(echo "$REMAINING" | grep -cE '^[0-9]+(\.[0-9]+)?$' || true)
if [ "$IS_NUM" = "1" ] && [ "$(echo "$REMAINING < 1.0" | bc)" = "1" ] && [ "$BOOKINGS" = "0" ]; then
    send_mail "balance below threshold with no booking today" \
        "Remaining data is ${REMAINING} GB, but no refill was verified today. The writer may be stuck below the threshold."
fi

echo "$(date -Is) check passed (age ${AGE}s, remaining $REMAINING GB, cycle ${MINUTES}min ago)"
