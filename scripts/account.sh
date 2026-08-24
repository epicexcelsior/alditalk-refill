#!/bin/sh
# Manage one account instance of the refill watcher.
# Usage:
#   scripts/account.sh add <name>     scaffold ~/alditalk-accounts/<name> and start it
#   scripts/account.sh remove <name>  stop and archive the account directory
#   scripts/account.sh list           show all instances and their state
set -eu

cd "$(dirname "$0")/.."
ACCOUNTS="$HOME/alditalk-accounts"

valid_name() {
    echo "$1" | grep -qE '^[a-z0-9][a-z0-9_-]{0,31}$'
}

staggered_interval() {
    # 3600 s base plus a name-derived offset (0-899 s) so accounts drift apart.
    OFFSET=$(printf '%s' "$1" | cksum | cut -d' ' -f1)
    echo $((3600 + OFFSET % 900))
}

case "${1:-}" in
    add)
        NAME="${2:-}"
        valid_name "$NAME" || { echo "Name must match [a-z0-9][a-z0-9_-]{0,31}. Example: add mom"; exit 1; }
        [ -d "$ACCOUNTS/$NAME" ] && { echo "Account '$NAME' already exists."; exit 1; }
        [ -x .venv/bin/python ] || { echo "Run scripts/setup.sh in the repo first."; exit 1; }

        mkdir -p "$ACCOUNTS/$NAME"
        cp config.example.json "$ACCOUNTS/$NAME/config.json"
        chmod 600 "$ACCOUNTS/$NAME/config.json"
        INTERVAL=$(staggered_interval "$NAME")
        sed -i "s/\"watch_interval_seconds\": 3600/\"watch_interval_seconds\": $INTERVAL/" \
            "$ACCOUNTS/$NAME/config.json"

        mkdir -p ~/.config/systemd/user
        ln -sfn "$PWD/systemd/alditalk-refill@.service" \
            ~/.config/systemd/user/alditalk-refill@.service
        systemctl --user daemon-reload

        echo "Scaffolded $ACCOUNTS/$NAME (interval ${INTERVAL}s)."
        echo "Next steps:"
        echo "  1. Edit $ACCOUNTS/$NAME/config.json: their number + password."
        echo "  2. Test read-only:"
        echo "       ALDITALK_CONFIG_DIR=$ACCOUNTS/$NAME xvfb-run -a \\"
        echo "         .venv/bin/python aldi.py check"
        echo "  3. Start it:"
        echo "       systemctl --user enable --now alditalk-refill@$NAME.service"
        ;;
    remove)
        NAME="${2:-}"
        valid_name "$NAME" || { echo "Bad name."; exit 1; }
        systemctl --user disable --now "alditalk-refill@$NAME.service" 2>/dev/null || true
        STAMP=$(date +%Y%m%d-%H%M%S)
        mv "$ACCOUNTS/$NAME" "$ACCOUNTS/$NAME.removed-$STAMP"
        chmod -R go-rwx "$ACCOUNTS/$NAME.removed-$STAMP"
        echo "Stopped and archived to $ACCOUNTS/$NAME.removed-$STAMP."
        echo "Delete the archive when you no longer need their session data."
        ;;
    list)
        ls -1 "$ACCOUNTS" 2>/dev/null | grep -v '\.removed-' || echo "No accounts."
        echo "---"
        systemctl --user list-units 'alditalk-refill@*' --no-legend --plain 2>/dev/null || true
        ;;
    *)
        echo "Usage: scripts/account.sh add|remove|list [name]"
        exit 1
        ;;
esac
