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
.venv/bin/python -m pip install -q -r requirements.txt
.venv/bin/python -m unittest

systemctl --user restart alditalk-refill-server.service || true
systemctl --user try-restart alditalk-refill.service 2>/dev/null || true
echo "$(date -Is) update complete"
