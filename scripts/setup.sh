#!/bin/sh
# One-command setup for a fresh clone.
# Usage: ./scripts/setup.sh [--with-autostart]
# Supports Linux and macOS. Windows users: follow README "Setup on Windows".
set -eu

cd "$(dirname "$0")/.."
echo "== ALDI TALK refill setup =="

# 1. Python
if ! command -v python3 >/dev/null 2>&1; then
    echo "ERROR: python3 not found. Install Python 3.10 or later first."
    exit 1
fi
PYV=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
echo "Python: $PYV"

# 2. Browser
BROWSER=""
for b in google-chrome google-chrome-stable chromium chromium-browser \
         "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"; do
    if command -v "$b" >/dev/null 2>&1 || [ -x "$b" ]; then
        BROWSER="$b"
        break
    fi
done
[ -n "$BROWSER" ] && echo "Browser: $BROWSER" || {
    echo "WARNING: no Chrome/Chromium found. Install Google Chrome,"
    echo "then set \"chrome_path\" in config.json if detection fails."
}

# 3. Virtual environment + dependencies
if [ ! -x .venv/bin/python ]; then
    echo "Creating virtual environment..."
    python3 -m venv .venv
fi
echo "Installing dependencies..."
.venv/bin/python -m pip install -q -r requirements.txt

# 4. Configuration
if [ ! -f config.json ]; then
    cp config.example.json config.json
    chmod 600 config.json 2>/dev/null || true
    echo ""
    echo "Created config.json. EDIT IT NOW:"
    echo "  - username: your portal phone number (leading 0)"
    echo "  - password: your portal password"
    echo "  - alerts:   optional Resend email settings"
else
    echo "Config: existing config.json kept."
fi

# 5. Self-test
echo "Running unit tests..."
.venv/bin/python -m unittest 2>/dev/null && echo "Tests passed." || {
    echo "ERROR: tests failed on this machine. Report the output."
    exit 1
}

# 6. Autostart (Linux only)
case "${1:-}" in
    --with-autostart)
        if [ "$(uname)" = "Linux" ] && command -v systemctl >/dev/null 2>&1; then
            mkdir -p ~/.config/systemd/user
            ln -sfn "$PWD/systemd/alditalk-refill.service" \
                ~/.config/systemd/user/alditalk-refill.service
            systemctl --user daemon-reload
            echo "Autostart installed. Start after login:"
            echo "  systemctl --user enable --now alditalk-refill.service"
        else
            echo "--with-autostart needs Linux with systemd."
            echo "On macOS use a LaunchAgent; on Windows use Task Scheduler."
        fi
        ;;
esac

echo ""
echo "== Next steps =="
echo "1. Fill username/password into config.json (chmod 600 kept)."
echo "2. Read-only test:      .venv/bin/python aldi.py check"
echo "3. Watcher (foreground): .venv/bin/python aldi.py watch"
