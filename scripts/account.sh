#!/bin/sh
# Manage one account instance of the refill watcher.
# Usage:
#   scripts/account.sh [--no-check] [--no-start] add <name>       prompt, validate, start
#   scripts/account.sh [--no-check] [--no-start] configure <name> re-prompt, validate, restart
#   scripts/account.sh check <name>       read-only portal check (instance must be stopped)
#   scripts/account.sh list               show all instances and their state
#   scripts/account.sh remove <name>      stop and archive the account directory
#
# Flags: --no-check skips the live read-only portal check, --no-start never
# enables or starts the systemd unit. Env: ALDITALK_ACCOUNTS_DIR overrides
# ~/alditalk-accounts for sandbox testing; systemd units only run the default
# path, so starting from an override directory is refused.
set -eu

# systemctl --user needs the session bus. Shells started without a desktop
# session (snap/Orca terminals, plain ssh) often leave XDG_RUNTIME_DIR unset,
# so every systemctl call fails with "Connection refused" and set -eu aborts
# the script. Point it at this user's runtime dir when systemd created one.
if [ -z "${XDG_RUNTIME_DIR:-}" ] && [ -d "/run/user/$(id -u)" ]; then
    XDG_RUNTIME_DIR="/run/user/$(id -u)"
    export XDG_RUNTIME_DIR
fi

cd "$(dirname "$0")/.."
REPO="$PWD"
ACCOUNTS="${ALDITALK_ACCOUNTS_DIR:-$HOME/alditalk-accounts}"
NO_CHECK=0
NO_START=0

while [ $# -gt 0 ]; do
    case "$1" in
        --no-check) NO_CHECK=1; shift ;;
        --no-start) NO_START=1; shift ;;
        --*) echo "Unknown flag: $1" >&2; exit 1 ;;
        *) break ;;
    esac
done

CMD="${1:-}"
NAME="${2:-}"

die() { echo "Error: $*" >&2; exit 1; }

valid_name() { printf '%s' "$1" | grep -qE '^[a-z0-9][a-z0-9_-]{0,31}$'; }

staggered_interval() {
    # 3600 s base plus a name-derived offset (0-899 s) so accounts drift apart.
    OFFSET=$(printf '%s' "$1" | cksum | cut -d' ' -f1)
    echo $((3600 + OFFSET % 900))
}

unit_active() {
    systemctl --user is-active --quiet "alditalk-refill@$1.service"
}

ensure_template_unit() {
    mkdir -p "$HOME/.config/systemd/user"
    ln -sfn "$REPO/systemd/alditalk-refill@.service" \
        "$HOME/.config/systemd/user/alditalk-refill@.service"
    systemctl --user daemon-reload
}

# Reads credentials and the alert recipient from stdin (hidden password on a
# tty), inherits the Resend key/sender from the main config.json, and writes
# the account config atomically with chmod 600.
PROMPT_PY=$(cat <<'PY'
import getpass, json, os, sys

cfg_path, example_path, main_path, mode, interval = sys.argv[1:6]
new = mode == "new"


def fail(msg):
    raise SystemExit("Error: " + msg)


def load(path):
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return {}
    except ValueError as exc:
        fail(path + " is not valid JSON: " + str(exc))
    if not isinstance(data, dict):
        fail(path + " must contain one JSON object.")
    return data


def read_line(prompt):
    if sys.stdin.isatty():
        return input(prompt)
    line = sys.stdin.readline()
    if line == "":
        fail("input ended while waiting for: " + prompt.rstrip())
    return line.rstrip("\n")


def read_secret(prompt):
    if sys.stdin.isatty():
        try:
            return getpass.getpass(prompt)
        except EOFError:
            fail("input ended while waiting for the password.")
    return read_line(prompt)


try:
    cfg = load(cfg_path)
    example = load(example_path)
    main = load(main_path)
    if not cfg:
        cfg = json.loads(json.dumps(example))

    if new:
        username = read_line("Phone number (like 015112345678): ").strip()
        if not username:
            fail("the phone number is required.")
        password = read_secret("Password: ")
        if not password:
            fail("the password is required.")
    else:
        current_user = str(cfg.get("username") or "")
        prompt = "Phone number" + (" [" + current_user + "]" if current_user else "")
        username = read_line(prompt + ": ").strip() or current_user
        password = read_secret("Password (blank = keep current): ")
        if not password:
            password = str(cfg.get("password") or "")
        if not username or not password:
            fail("username and password must not be empty.")
    cfg["username"] = username
    cfg["password"] = password

    current_alerts = cfg.get("alerts") if isinstance(cfg.get("alerts"), dict) else {}
    if new or not current_alerts:
        prompt = "Alert email for this person (blank = no alerts): "
    else:
        keep = current_alerts.get("to", "")
        prompt = (
            "Alert email (Enter = keep current, none = disable)"
            + (" [" + keep + "]" if keep else "")
            + ": "
        )
    answer = read_line(prompt).strip()
    if answer.lower() in ("none", "no", "off", "disable"):
        cfg.pop("alerts", None)
    elif not answer:
        if new or not current_alerts:
            cfg.pop("alerts", None)
    else:
        if "@" not in answer:
            fail("the alert email must contain @.")
        inherited = main.get("alerts") if isinstance(main.get("alerts"), dict) else {}
        api_key = inherited.get("resend_api_key")
        if not isinstance(api_key, str) or not api_key:
            api_key = "env:RESEND_API_KEY"
        sender = inherited.get("from")
        if not isinstance(sender, str) or "@" not in sender:
            sender = "alerts@your-verified-domain.de"
        if not inherited:
            print(
                "Note: main config.json has no alerts; using "
                + api_key + " / " + sender,
                file=sys.stderr,
            )
        try:
            threshold = int(inherited.get("failure_threshold", 3))
        except (TypeError, ValueError):
            threshold = 3
        cfg["alerts"] = {
            "resend_api_key": api_key,
            "from": sender,
            "to": answer,
            "on_booking": False,
            "on_failure": bool(inherited.get("on_failure", True)),
            "failure_threshold": max(1, threshold),
        }

    if interval:
        cfg["watch_interval_seconds"] = int(interval)

    tmp = cfg_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(cfg, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    os.replace(tmp, cfg_path)
    os.chmod(cfg_path, 0o600)
    print("Saved " + cfg_path + " (chmod 600).")
except EOFError:
    fail("input ended unexpectedly.")
except KeyboardInterrupt:
    fail("interrupted.")
PY
)

prompt_config() {
    # $1 config path, $2 example path, $3 main config path, $4 new|update, $5 interval
    "$REPO/.venv/bin/python" -c "$PROMPT_PY" "$1" "$2" "$3" "$4" "$5"
}

validate_config() {
    # Local schema validation through the real loader; never touches the portal.
    env -u ALDITALK_USERNAME -u ALDITALK_PASSWORD ALDITALK_CONFIG_DIR="$1" \
        "$REPO/.venv/bin/python" -c 'import sys; sys.path.insert(0, sys.argv[1])
import aldi
aldi.load_config()' "$REPO"
}

run_check() {
    if command -v xvfb-run >/dev/null 2>&1; then
        env -u ALDITALK_USERNAME -u ALDITALK_PASSWORD ALDITALK_CONFIG_DIR="$1" \
            xvfb-run -a "$REPO/.venv/bin/python" "$REPO/aldi.py" check
    else
        env -u ALDITALK_USERNAME -u ALDITALK_PASSWORD ALDITALK_CONFIG_DIR="$1" \
            "$REPO/.venv/bin/python" "$REPO/aldi.py" check
    fi
}

start_instance() {
    if [ "$ACCOUNTS" != "$HOME/alditalk-accounts" ]; then
        die "The systemd template runs %h/alditalk-accounts/<name>, so '$ACCOUNTS' cannot start as an instance. Use --no-start for sandbox directories."
    fi
    ensure_template_unit
    systemctl --user enable --now "alditalk-refill@$1.service"
}

LIST_PY=$(cat <<'PY'
import json, os, subprocess, sys, time

accounts = sys.argv[1]
repo = sys.argv[2] if len(sys.argv) > 2 else None
entries = []
archived = 0
if os.path.isdir(accounts):
    for name in sorted(os.listdir(accounts)):
        if ".removed-" in name:
            archived += 1
        elif os.path.isdir(os.path.join(accounts, name)):
            entries.append(name)

rows = []
if repo:
    rows.append(("main", repo, "alditalk-refill-server.service"))
for name in entries:
    rows.append((name, os.path.join(accounts, name), "alditalk-refill@" + name + ".service"))

print(f"{'ACCOUNT':<16} {'STATE':<9} {'REMAINING':>10} {'INTERVAL':>9}  LAST CYCLE")
for name, path, unit in rows:
    cfg, state = {}, {}
    try:
        with open(os.path.join(path, "config.json"), encoding="utf-8") as handle:
            cfg = json.load(handle)
    except (OSError, ValueError):
        pass
    try:
        with open(os.path.join(path, ".watch-state.json"), encoding="utf-8") as handle:
            state = json.load(handle)
    except (OSError, ValueError):
        pass
    try:
        active = (
            subprocess.run(
                ["systemctl", "--user", "is-active", unit],
                capture_output=True,
            ).returncode
            == 0
        )
    except OSError:
        active = False
    remaining = state.get("remaining_gb")
    remaining_s = f"{remaining:g} GB" if isinstance(remaining, (int, float)) else "-"
    interval = cfg.get("watch_interval_seconds", "?")
    interval_s = str(interval) + "s" if isinstance(interval, int) else str(interval)
    last = state.get("last_cycle_ts")
    if isinstance(last, (int, float)):
        minutes = max(0.0, (time.time() - last) / 60)
        last_s = f"{minutes:.0f} min ago" if minutes < 120 else f"{minutes / 60:.1f} h ago"
    else:
        last_s = "-"
    print(
        f"{name:<16} {'active' if active else 'inactive':<9} "
        f"{remaining_s:>10} {interval_s:>9}  {last_s}"
    )
if not entries:
    print("No accounts under " + accounts + ".")
if archived:
    print(f"{archived} archived (.removed-*) kept under {accounts}")
PY
)

case "$CMD" in
    add)
        valid_name "$NAME" || die "Name must match [a-z0-9][a-z0-9_-]{0,31}. Example: scripts/account.sh add mom"
        [ -e "$ACCOUNTS/$NAME" ] && die "Account '$NAME' already exists."
        [ -x "$REPO/.venv/bin/python" ] || die "Run scripts/setup.sh in the repo first."
        mkdir -p "$ACCOUNTS/$NAME"
        chmod 700 "$ACCOUNTS/$NAME"
        INTERVAL=$(staggered_interval "$NAME")
        prompt_config "$ACCOUNTS/$NAME/config.json" "$REPO/config.example.json" \
            "$REPO/config.json" new "$INTERVAL" \
            || die "Prompting aborted for '$NAME'. Discard it with: scripts/account.sh remove $NAME"
        validate_config "$ACCOUNTS/$NAME" \
            || die "config.json for '$NAME' failed validation (see message above)."
        echo "Scaffolded $ACCOUNTS/$NAME (interval ${INTERVAL}s)."
        if [ "$NO_CHECK" -eq 0 ]; then
            echo "Running read-only check for '$NAME'..."
            run_check "$ACCOUNTS/$NAME" \
                || die "Read-only check failed; '$NAME' was not started. Fix the credentials and retry, or use --no-check."
        fi
        if [ "$NO_START" -eq 0 ]; then
            start_instance "$NAME"
            echo "Started alditalk-refill@$NAME.service."
        fi
        ;;
    configure)
        valid_name "$NAME" || die "Name must match [a-z0-9][a-z0-9_-]{0,31}."
        CFG="$ACCOUNTS/$NAME/config.json"
        [ -f "$CFG" ] || die "No account '$NAME' under $ACCOUNTS. Use: scripts/account.sh add $NAME"
        [ -x "$REPO/.venv/bin/python" ] || die "Run scripts/setup.sh in the repo first."
        prompt_config "$CFG" "$REPO/config.example.json" "$REPO/config.json" update "" \
            || die "Prompting aborted; '$NAME' kept its previous config."
        validate_config "$ACCOUNTS/$NAME" \
            || die "config.json for '$NAME' failed validation (see message above)."
        if unit_active "$NAME"; then
            echo "Stopping alditalk-refill@$NAME.service to apply the new config..."
            systemctl --user stop "alditalk-refill@$NAME.service"
        fi
        if [ "$NO_CHECK" -eq 0 ]; then
            echo "Running read-only check for '$NAME'..."
            run_check "$ACCOUNTS/$NAME" \
                || die "Read-only check failed; '$NAME' is left stopped. Fix the credentials and retry, or use --no-check."
        fi
        if [ "$NO_START" -eq 0 ]; then
            start_instance "$NAME"
            echo "Started alditalk-refill@$NAME.service."
        else
            echo "Left stopped (--no-start)."
        fi
        ;;
    check)
        valid_name "$NAME" || die "Name must match [a-z0-9][a-z0-9_-]{0,31}."
        [ -f "$ACCOUNTS/$NAME/config.json" ] || die "No account '$NAME' under $ACCOUNTS."
        if unit_active "$NAME"; then
            die "'$NAME' is running; stop it first: systemctl --user stop alditalk-refill@$NAME.service"
        fi
        run_check "$ACCOUNTS/$NAME" || die "Read-only check failed for '$NAME'."
        ;;
    list)
        "$REPO/.venv/bin/python" -c "$LIST_PY" "$ACCOUNTS" "$REPO"
        ;;
    remove)
        valid_name "$NAME" || die "Name must match [a-z0-9][a-z0-9_-]{0,31}."
        [ -d "$ACCOUNTS/$NAME" ] || die "No account '$NAME' under $ACCOUNTS."
        systemctl --user disable --now "alditalk-refill@$NAME.service" 2>/dev/null || true
        STAMP=$(date +%Y%m%d-%H%M%S)
        mv "$ACCOUNTS/$NAME" "$ACCOUNTS/$NAME.removed-$STAMP"
        chmod -R go-rwx "$ACCOUNTS/$NAME.removed-$STAMP"
        echo "Stopped and archived to $ACCOUNTS/$NAME.removed-$STAMP."
        echo "Delete the archive when you no longer need their session data."
        ;;
    *)
        cat <<'EOF'
Usage: scripts/account.sh [--no-check] [--no-start] <command> [name]

Commands:
  add <name>         scaffold an account, prompt for credentials and alert
                     email, validate the config, run a read-only check, start
  configure <name>   re-prompt for credentials/alerts, validate, restart
  check <name>       read-only portal check (instance must be stopped)
  list               show all instances and their state
  remove <name>      stop and archive the account directory

Flags:
  --no-check   skip the live read-only portal check
  --no-start   do not enable or start the systemd unit
EOF
        exit 1
        ;;
esac
