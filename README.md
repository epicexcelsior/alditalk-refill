# ALDI TALK refill

This client watches one ALDI TALK tariff with free unlimited refills. It books 1 GB when your data falls below ALDI's live threshold.

The client runs a real Chrome window with its own profile. It sends portal backend requests from that logged-in page. It does not click dashboard buttons.

ALDI TALK uses private endpoints. ALDI can change these endpoints at any time. The [service terms](https://www.alditalk.de/leistungsbeschreibung) limit scripts and unauthorized access.

Use this client only with your own account. Do not run a shared credential service.

## Status

Verified on 2026-08-23 with Chrome 151 on Ubuntu:

- A fresh Chrome profile logged in without copied browser state.
- Headed Chrome returned `botProtectionOtpRequired: false`.
- A controlled booking added 1 GB to the domestic balance.
- The packaged watcher detected 0.72 GB, booked 1 GB, and verified the new balance.
- Nineteen automated tests pass.

## How it works

1. The client starts installed Chrome in headed mode. The window starts minimized.
2. Chrome uses the private `.chrome-profile` directory.
3. The client logs in through the normal login page.
4. The client reads your subscription and offer through the portal backend.
5. The client compares your domestic balance with ALDI's live threshold.
6. When a refill is due, the client calls `validateBotScore`.
7. The client submits `updateUnlimited` when ALDI permits the refill.
8. The client verifies that the domestic balance increased.

Each watch cycle loads the dashboard page once. The default interval is one hour plus jitter. This gives about 24 page loads per day.

A read retry runs once when a page reload overlaps a read. An interrupted booking never retries at once. The next cycle reads the balance first.

## Requirements

- Python 3.10 or later
- Google Chrome or Chromium
- A graphical desktop session
- An eligible ALDI TALK offer

Linux and macOS work today. Windows uses the same steps. Windows did not get a native runtime test yet. All platforms need a desktop session. A headless server cannot run this client directly. See "Server deployment" for the virtual display option.

Playwright connects to your installed Chrome. You do not need `playwright install`.

## Quick setup (Linux and macOS)

After cloning, run one command:

```bash
./scripts/setup.sh
```

The script checks Python and Chrome, creates `.venv`, installs dependencies, seeds `config.json`, and runs the tests. Add `--with-autostart` to also install the Linux autostart unit.

Manual setup stays available below.

## Setup on Linux

```bash
cd alditalk-refill
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp config.example.json config.json
chmod 600 config.json
```

Put your phone number and password in `config.json`. Use the same number format as the portal login form. You can instead set `ALDITALK_USERNAME` and `ALDITALK_PASSWORD`.

## Setup on macOS

Install Google Chrome and Python 3 first.

```bash
cd alditalk-refill
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp config.example.json config.json
chmod 600 config.json
```

macOS detects a standard Chrome install automatically. Set `chrome_path` when detection fails.

## Setup on Windows

Install Google Chrome and Python 3 first. Run these commands in PowerShell:

```powershell
cd alditalk-refill
py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item config.example.json config.json
```

Windows support is code-complete but not runtime-tested. Report failures instead of changing code blindly.

## Configuration

```json
{
  "username": "YOUR_PHONE_NUMBER",
  "password": "YOUR_PASSWORD",
  "transport": "browser",
  "chrome_path": null,
  "chrome_profile_path": ".chrome-profile",
  "watch_interval_seconds": 3600,
  "jitter_fraction": 0.2,
  "otp_command": null,
  "otp_timeout_seconds": 120
}
```

Keep `transport` set to `browser`. The `api` transport exists for diagnosis only. It triggered SMS verification during testing.

Set `chrome_path` only when automatic detection fails.

The Chrome profile holds authenticated cookies. Keep it private. Never commit or share it.

Use one profile directory per account. Never run two processes against one profile.

Keep the interval at or above 3600 seconds. Jitter breaks the fixed schedule.

## Email alerts

Alerts are optional. Delete the `alerts` object to run silent.

```json
"alerts": {
  "resend_api_key": "env:RESEND_API_KEY",
  "from": "alerts@your-verified-domain.de",
  "to": "you@example.com",
  "on_booking": true,
  "on_failure": true,
  "failure_threshold": 3
}
```

The client sends mail through [Resend](https://resend.com). `from` must use a domain that you verified in Resend.

`resend_api_key` accepts a literal key or an `env:NAME` indirection. Prefer `env:` and export the variable in the service environment. The client reads it at send time only.

The watcher sends these mails:

- One confirmation after each booked refill, when `on_booking` is true.
- One alert when failures reach `failure_threshold` in a row.
- One final alert before exit on a rejected password or a required SMS check.

A failed delivery never stops the watcher. It prints one log line instead.

## Commands

| Command | Action |
| --- | --- |
| `check` | Read-only. Logs in if needed and prints balances. |
| `probe` | Read-only. Measures session lifetime every 5 minutes. |
| `book` | Books one refill when eligible. Verifies the new balance. |
| `watch` | Runs forever. Checks each interval and books below the threshold. |

Run the commands like this:

```bash
# Linux and macOS
.venv/bin/python aldi.py check

# Windows PowerShell
.venv\Scripts\python.exe aldi.py check
```

`check` prints output that starts like this:

```text
Logged in with headed Chrome.
Offer: Tarif S status=active
```

## Watcher behavior

Start the watcher after desktop login:

```bash
.venv/bin/python aldi.py watch
```

The watcher keeps Chrome open. It restarts Chrome after a dead session. It backs off after transient errors. A rejected password stops the process.

## Autostart

### Linux desktop

The included user service starts the watcher after graphical login. It assumes the project path is `~/alditalk-refill`.

```bash
mkdir -p ~/.config/systemd/user
ln -sfn "$PWD/systemd/alditalk-refill.service" ~/.config/systemd/user/alditalk-refill.service
systemctl --user daemon-reload
systemctl --user enable --now alditalk-refill.service
```

Read logs:

```bash
journalctl --user -u alditalk-refill.service -n 50
```

Stop the service:

```bash
systemctl --user disable --now alditalk-refill.service
```

The service does not restart a fatal exit. The watcher handles transient errors inside its own process.

### macOS and Windows

On macOS, use a LaunchAgent. On Windows, use Task Scheduler with "run only when user is logged on".

## Server deployment

The browser transport needs a display. A headless server needs Xvfb. Xvfb creates a virtual display, and Chrome then runs in "headed" mode inside it.

1. Install `google-chrome-stable` and `xvfb`.
2. Clone the repository and finish setup as above.
3. Test one manual run under `xvfb-run`:

```bash
xvfb-run -a .venv/bin/python aldi.py check
```

4. If the check passes, install `systemd/alditalk-refill-server.service`. It wraps the watcher in Xvfb.
5. Enable auto-updates with `systemd/alditalk-refill-update.timer`. See "Updates".

Test the real bot check from your server IP before you rely on it. See "IP location risk".

## Updates

Continuous delivery uses two parts.

- Continuous integration: `.github/workflows/tests.yml` runs the unit tests on every push.
- Continuous delivery on the server: `systemd/alditalk-refill-update.timer` pulls the main branch, installs dependencies, runs the tests, and restarts the watcher. It runs daily by default. A failed test run aborts the update and keeps the old version.

Enable the update timer on the server:

```bash
ln -sfn "$PWD/systemd/alditalk-refill-update.service" ~/.config/systemd/user/alditalk-refill-update.service
ln -sfn "$PWD/systemd/alditalk-refill-update.timer" ~/.config/systemd/user/alditalk-refill-update.timer
systemctl --user daemon-reload
systemctl --user enable --now alditalk-refill-update.timer
```

## SMS verification fallback

Headed Chrome avoids SMS verification today. ALDI can still demand it for a future session.

If `validateBotScore` returns true, the client follows the portal's official sequence:

1. `POST /v1/generateOtp`
2. Read one new six-digit code.
3. `POST /v1/validateOtp`
4. `POST /v1/offer/updateUnlimited`

Without `otp_command`, the client stops before it sends an SMS.

An OTP provider must print one six-digit code to standard output. The client does not invoke a shell. The client sets `ALDITALK_OTP_REQUESTED_AT` to a Unix timestamp. The provider must ignore older messages.

Do not forward OTP messages through a public webhook or shared service.

## IP location risk

ALDI serves customers in Germany. A login from a foreign IP is unusual. Risk systems flag unusual logins. Testing showed that small deviations already trigger SMS verification.

Rules for a server outside Germany:

1. Run `check` once. Confirm it logs in without an SMS prompt.
2. Run `watch` for one day. Confirm zero OTP prompts in the log.
3. Stop at the first SMS prompt. Move the client to a German IP or accept manual OTP handling.

A German VPS or a home connection in Germany gives the lowest risk.

## One account versus several accounts

Use one project copy and one Chrome profile for your own account.

For friends, each person runs a separate copy on their own computer. Credentials and sessions stay on their machine.

A central service for 5 to 20 accounts raises credential, policy, and anti-bot risks. This repository does not implement that model.

## Instructions for AI agents

Deploy this client on a new machine with this checklist. Read `aldi.py` before you deviate.

1. Install Python 3.10 or later and Google Chrome.
2. Clone the repository into the user's home directory.
3. Create a virtual environment. Install `requirements.txt`.
4. Copy `config.example.json` to `config.json`. On Linux and macOS run `chmod 600 config.json`.
5. Ask the user for their phone number and password. Write them into `config.json`, or set `ALDITALK_USERNAME` and `ALDITALK_PASSWORD`.
6. Run `check`. Confirm the output shows an offer name, status `active`, and balances.
7. Start the watcher after desktop login. Use systemd, a LaunchAgent, or Task Scheduler.

Platform notes:

- Linux and macOS: `.venv/bin/python aldi.py check`
- Windows PowerShell: `.venv\Scripts\python.exe aldi.py check`
- Set `chrome_path` when automatic detection fails.
- On headless servers, follow "Server deployment". Report failures to the user.

Guardrails:

- Never commit, print, transmit, or copy `config.json` or `.chrome-profile`. Both hold live credentials and session cookies.
- Never run two watcher processes against one Chrome profile.
- Never invent or enable `otp_command`. Only the user provides one.
- If login ends in an SMS prompt, stop and tell the user. Do not loop retries.
- Do not book unless the user asked for that exact action. `check` and `probe` are read-only.

## Tests

Run the automated tests:

```bash
.venv/bin/python -m unittest -v
```

Nineteen tests cover login callbacks, offer selection, threshold boundaries, booking payloads, OTP handling, session expiry, write safety, and the browser transport.

The source HAR stays outside this repository. It contains account data.

## Known limits

- The client selects the first subscription in the navigation response.
- macOS and Windows did not get native runtime tests.
- Chrome needs a display, real or virtual.
- Credentials stay in local configuration unless environment variables supply them.
- The Chrome profile holds sensitive session cookies.
- ALDI publishes no idempotency key for the refill request.
- ALDI can change its portal, bot check, authentication, or endpoints at any time.
- ALDI can require SMS verification again.

This design is the lowest-maintenance path verified on one account. Do not treat it as bulletproof.
