# ALDI TALK refill

Never think about data refills again. This client watches one ALDI TALK tariff with free unlimited refills and books 1 GB the moment your data falls below ALDI's live threshold.

```
                every ~60 minutes
  ┌──────────────────────────────────────────────┐
  │  aldi.py watch                               │
  │                                              │
  │   real Chrome (headed, own profile)          │
  │        │  logs in like you would             │
  │        ▼                                     │
  │   "how much data is left?"                   │
  │        │                                     │
  │        ├── above threshold ──► sleep         │
  │        │                                     │
  │        └── at/below 1 GB                     │
  │                 ▼                            │
  │            bot check OK? ──► book 1 GB       │
  │                 ▼              (free)        │
  │            verify balance went up            │
  │                 ▼                            │
  │            email confirmation                │
  └──────────────────────────────────────────────┘
```

The client drives a real Chrome window with its own profile. It sends portal backend requests from that logged-in page. It does not click dashboard buttons. If a session dies, it logs in again on its own.

ALDI TALK uses private endpoints and can change them at any time. The [service terms](https://www.alditalk.de/leistungsbeschreibung) limit scripts and unauthorized access. Use this client only with your own account. Do not run a shared credential service.

---

## Contents

- [Status](#status)
- [Platform support](#platform-support)
- [Requirements](#requirements)
- [Quick setup](#quick-setup-linux-and-macos)
- [Configuration](#configuration)
- [Commands](#commands)
- [Watcher behavior](#watcher-behavior)
- [Autostart](#autostart)
- [Server deployment](#server-deployment)
- [Updates](#updates)
- [Email alerts](#email-alerts)
- [SMS verification fallback](#sms-verification-fallback)
- [IP location risk](#ip-location-risk)
- [One account versus several accounts](#one-account-versus-several-accounts)
- [Instructions for AI agents](#instructions-for-ai-agents)
- [Approaches investigated](#approaches-investigated)
- [Tests](#tests)
- [Known limits](#known-limits)

## Status

Verified against the live portal:

| When | What |
| --- | --- |
| 2026-08-23 | Fresh Chrome profile logged in without copied browser state |
| 2026-08-23 | Headed Chrome passed the bot check with no OTP demand |
| 2026-08-23 | Watcher detected 0.72 GB, booked 1 GB, verified the new balance |
| 2026-08-24 | Headless server (Xvfb + headed Chrome) logged in from a US IP |
| 2026-08-24 | Session self-healing observed live: dead session, auto re-login |
| 2026-08-24 | Email alert path verified through Resend |

25 automated tests pass on every push (see CI badge in your repository).

## Platform support

| Platform | State | Autostart | Notes |
| --- | --- | --- | --- |
| Linux desktop | Verified | systemd user unit | The reference setup |
| Linux server (headless) | Verified | systemd + Xvfb | See [Server deployment](#server-deployment) |
| macOS | Code-ready, not runtime-tested | LaunchAgent | Needs Python 3.10+ and installed Chrome |
| Windows | Code-ready, not runtime-tested | Task Scheduler | Same steps as macOS; report failures |

The client works wherever a real desktop Chrome runs. It never works headless without a virtual display. The first `check` run is the test on any new device: if it prints balances, that device works.

## Requirements

- Python 3.10 or later
- Google Chrome or Chromium
- A graphical desktop session (real or Xvfb)
- An eligible ALDI TALK offer

Linux and macOS work today. Windows uses the same steps but did not get a native runtime test yet.

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
  "otp_timeout_seconds": 120,
  "alerts": {
    "resend_api_key": "env:RESEND_API_KEY",
    "from": "alerts@your-verified-domain.de",
    "to": "you@example.com",
    "on_booking": true,
    "on_failure": true,
    "failure_threshold": 3
  }
}
```

Keep `transport` set to `browser`. The `api` transport exists for diagnosis only. It triggered SMS verification during testing.

Set `chrome_path` only when automatic detection fails.

The Chrome profile holds authenticated cookies. Keep it private. Never commit or share it.

Use one profile directory per account. Never run two processes against one profile.

Keep the interval at or above 3600 seconds. Jitter breaks the fixed schedule.

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

Each cycle loads the dashboard page once. The default interval is one hour plus jitter. This gives about 24 page loads per day.

A read retry runs once when a page reload overlaps a read. An interrupted booking never retries at once. The next cycle reads the balance first.

The watcher keeps Chrome open and restarts it after a dead session. It backs off after transient errors with growing waits. A rejected password stops the process.

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
5. Enable auto-updates with `systemd/alditalk-refill-update.timer`. See [Updates](#updates).

Test the real bot check from your server IP before you rely on it. See [IP location risk](#ip-location-risk).

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

## Email alerts

Alerts are optional. Delete the `alerts` object to run silent.

The client sends mail through [Resend](https://resend.com). `from` must use a domain that you verified in Resend.

`resend_api_key` accepts a literal key or an `env:NAME` indirection. Prefer `env:` and export the variable in the service environment. The client reads it at send time only.

The watcher sends these mails:

- One confirmation after each booked refill, when `on_booking` is true.
- One alert when failures reach `failure_threshold` in a row.
- One final alert before exit on a rejected password or a required SMS check.

A failed delivery never stops the watcher. It prints one log line instead.

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

A German VPS or a home connection in Germany gives the lowest risk. A US residential ISP passed during testing; treat that as luck, not a guarantee.

## One account versus several accounts

Use one project copy and one Chrome profile for your own account.

For friends, each person runs a separate copy on their own computer. Credentials and sessions stay on their machine.

A central service for 5 to 20 accounts raises credential, policy, and anti-bot risks. This repository does not implement that model.

## Instructions for AI agents

Deploy this client on a new machine with this checklist. Read `aldi.py` before you deviate.

1. Install Python 3.10 or later and Google Chrome.
2. Clone the repository into the user's home directory.
3. Run `./scripts/setup.sh`. On Windows follow "Setup on Windows" instead.
4. Ask the user for their phone number and password. Write them into `config.json`, or set `ALDITALK_USERNAME` and `ALDITALK_PASSWORD`.
5. Run `check`. Confirm the output shows an offer name, status `active`, and balances.
6. Start the watcher after desktop login. Use systemd, a LaunchAgent, or Task Scheduler.

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

## Approaches investigated

### Headed Chrome with page-context API calls

Result: selected.

A fresh real Chrome 151 profile returned no OTP requirement. One controlled browser refill increased the balance. This transport preserves the real browser fingerprint. It removes routine dependency on dashboard selectors.

### Direct Python client

Result: retained only as `transport: "api"` for diagnosis.

Login, offer reads, and threshold detection work. The bot check returned `botProtectionOtpRequired: true` on the test account.

### Headless Chrome

Result: rejected.

Real Chrome 151 in headless mode reported no WebDriver marker. ALDI still required OTP. Hiding `navigator.webdriver` is not enough.

### `gommzystudio/AldiTalk-True-Unlimited`

Result: not used.

The repository starts a new headless browser every 15 minutes. It clicks `1 GB` without balance verification. Its success log can appear when the click only opened the OTP dialog.

### Direct update without bot validation

Result: rejected.

One public Go project posts directly to `updateUnlimited`. That sequence skips ALDI's bot-validation request. This client keeps `validateBotScore` in the official sequence.

### Browser TLS impersonation

Result: rejected.

TLS impersonation cannot reproduce all headed-browser signals. Real headless Chrome already failed the same bot check.

## Tests

Run the automated tests:

```bash
.venv/bin/python -m unittest -v
```

25 tests cover login callbacks, offer selection, threshold boundaries, booking payloads, OTP handling, session expiry, email alerts, write safety, and the browser transport.

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
- Usage readings can lag behind real consumption. The client books only on a confirmed below-threshold reading, so lag delays a booking; it never causes a wrong one.

This design is the lowest-maintenance path verified on one account. Do not treat it as bulletproof.
