#!/bin/sh
# Self-update: pull main, sync dependencies, run tests, restart the watcher.
# Aborts without changes when any step fails.
set -eu

cd "$HOME/alditalk-refill"

git fetch origin main
LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse origin/main)

if [ "$LOCAL" = "$REMOTE" ]; then
    echo "$(date -Is) already up to date"
    exit 0
fi

echo "$(date -Is) updating $LOCAL -> $REMOTE"
git reset --hard origin/main
rollback() {
    echo "$(date -Is) update failed, rolling back to $LOCAL"
    git reset --hard "$LOCAL" || true
    systemctl --user try-restart alditalk-refill-server.service 2>/dev/null || true
    exit 1
}
if ! .venv/bin/python -m pip install -q -r requirements.txt; then
    rollback
fi
if ! .venv/bin/python -m unittest; then
    rollback
fi

systemctl --user restart alditalk-refill-server.service || true
systemctl --user try-restart alditalk-refill.service 2>/dev/null || true
for u in $(systemctl --user list-units 'alditalk-refill@*' --no-legend --plain 2>/dev/null | awk '$3=="active" {print $1}'); do
    systemctl --user try-restart "$u"
done
echo "$(date -Is) update complete"
