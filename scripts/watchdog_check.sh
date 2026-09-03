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
ALERT_STATE="$DIR/.last_alert"
ALERT_COOLDOWN="${WATCHDOG_ALERT_COOLDOWN:-10800}"

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

should_send_alert() {
    KEY=$1
    if [ ! -f "$ALERT_STATE" ]; then
        return 0
    fi
    PREV_KEY=$(jq -r '.key // empty' "$ALERT_STATE" 2>/dev/null || true)
    PREV_TS=$(jq -r '.ts // 0' "$ALERT_STATE" 2>/dev/null || true)
    NOW=$(date +%s)
    if [ "$KEY" != "$PREV_KEY" ]; then
        return 0
    fi
    if [ $((NOW - PREV_TS)) -ge "$ALERT_COOLDOWN" ]; then
        return 0
    fi
    echo "$(date -Is) alert suppressed (duplicate $KEY within cooldown)"
    return 1
}

record_alert() {
    KEY=$1
    echo "{\"key\":\"$KEY\",\"ts\":$(date +%s)}" > "$ALERT_STATE"
}

clear_alert() {
    if [ -f "$ALERT_STATE" ]; then
        PREV_KEY=$(jq -r '.key // empty' "$ALERT_STATE" 2>/dev/null || true)
        rm -f "$ALERT_STATE"
        if [ -n "$PREV_KEY" ]; then
            send_mail "check cycle recovered" \
                "Watchdog check passed. The watcher is active and balance read succeeded (remaining ${REMAINING} GB)." || true
        fi
    fi
}

send_throttled_alert() {
    KEY=$1
    SUBJECT=$2
    BODY=$3
    if should_send_alert "$KEY"; then
        send_mail "$SUBJECT" "$BODY"
        record_alert "$KEY"
    fi
}

if [ ! -f "$HEARTBEAT" ]; then
    send_throttled_alert "no_heartbeat" "no heartbeat file" \
        "The watcher server has never delivered a heartbeat. It is probably down."
    exit 0
fi

AGE=$(( $(date +%s) - $(stat -c %Y "$HEARTBEAT") ))
if [ "$AGE" -gt "$MAX_AGE" ]; then
    send_throttled_alert "silent_$((AGE / 3600))h" "watcher silent for $((AGE / 60)) minutes" \
        "No heartbeat for $((AGE / 60)) minutes (limit $((MAX_AGE / 60))). The f5server watcher or its network path is down."
    exit 0
fi

SERVICE=$(jq -r '.service_active' "$HEARTBEAT")
REMAINING=$(jq -r '.remaining_gb' "$HEARTBEAT")
MINUTES=$(jq -r '.minutes_since_cycle' "$HEARTBEAT")
BOOKINGS=$(jq -r '.bookings_today' "$HEARTBEAT")
LAST_ERROR=$(jq -r '.last_error // empty' "$HEARTBEAT")

HAS_FAILURE=0

if [ "$SERVICE" != "true" ]; then
    HAS_FAILURE=1
    send_throttled_alert "service_inactive" "watcher service not active" \
        "Heartbeat arrived but reports the systemd service is not active."
fi

case "$MINUTES" in
    ''|null|[0-9]*) ;;
    *) MINUTES=null ;;
esac
if [ "$MINUTES" != "null" ] && [ "$MINUTES" -gt 100 ]; then
    HAS_FAILURE=1
    send_throttled_alert "stuck_cycle" "no check cycle for $MINUTES minutes" \
        "Heartbeat is fresh but the last successful balance read was $MINUTES minutes ago. Chrome, login, or the portal response may be stuck. Last error: ${LAST_ERROR:-none}."
fi

IS_NUM=$(echo "$REMAINING" | grep -cE '^[0-9]+(\.[0-9]+)?$' || true)
if [ "$IS_NUM" = "1" ] && [ "$(awk -v r="$REMAINING" 'BEGIN{print (r<1.0)}')" = "1" ] && [ "$BOOKINGS" = "0" ]; then
    HAS_FAILURE=1
    send_throttled_alert "below_threshold" "balance below threshold with no booking today" \
        "Remaining data is ${REMAINING} GB, but no refill was verified today. The writer may be stuck below the threshold."
fi

if [ "$HAS_FAILURE" = "0" ]; then
    clear_alert
fi

echo "$(date -Is) check passed (age ${AGE}s, remaining $REMAINING GB, cycle ${MINUTES}min ago)"
