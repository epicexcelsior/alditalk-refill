# ALDI TALK refill

This client monitors one eligible ALDI TALK unlimited offer. It books the free data refill at ALDI's live threshold.

The selected design uses a normal headed Chrome session. Chrome starts minimized and keeps a dedicated profile.

The client calls ALDI's portal backend from that Chrome page. It does not automate the dashboard for routine checks or booking.

ALDI TALK uses private endpoints that can change without notice. The [current service terms](https://www.alditalk.de/leistungsbeschreibung) restrict scripts, bots, and unauthorized software access.

Use this project only for your own account. Do not operate a shared credential service.

## Current status

Verified on 2026-08-23 with Google Chrome 151 on Ubuntu:

- A fresh dedicated Chrome profile logged in without copied browser state.
- Headed Chrome returned `botProtectionOtpRequired: false`.
- A controlled headed-Chrome click booked one refill.
- The domestic balance increased after that refill.
- The packaged `check` command logged in and read the real offer and balances.
- The packaged watcher detected 0.72 GB, booked 1 GB, and verified the new balance.
- The installed user service remained active after the refill.
- The browser transport calls portal endpoints from the authenticated page.
- Nineteen automated tests pass.

The packaged browser read and write paths now pass against the real account.

## Why this design is the best current path

The browser and API hybrid gives the best balance of reliability and maintenance:

1. Real headed Chrome passes the current bot check.
2. A persistent profile avoids repeated browser setup.
3. Structured backend calls avoid brittle dashboard selectors.
4. The client reads ALDI's live threshold and refill payload.
5. The client validates the new balance after each booking.
6. The client never retries an interrupted booking request immediately.

Pure HTTP requests triggered SMS verification on the same account. Headless Chrome also triggered SMS verification.

The watcher keeps one Chrome process open. A one-hour interval creates about 24 balance checks each day.

## How the client works

1. The client starts installed Chrome in normal headed mode.
2. Chrome uses the private `.chrome-profile` directory.
3. The client logs in through ALDI's normal login page.
4. The client reads the subscription and eligible offer through the portal backend.
5. The client compares the domestic balance with ALDI's live threshold.
6. The client calls `validateBotScore` only when a refill is due.
7. The client submits `updateUnlimited` when ALDI permits the refill.
8. The client verifies that the domestic balance increased.

If a read request overlaps a page reload, the client retries that read once.

The client does not retry an interrupted write. The next loop reads the balance before it can submit another refill.

## Requirements

- Python 3.10 or later
- Google Chrome or Chromium
- A graphical desktop session
- An eligible ALDI TALK offer

Runs on Linux and macOS today. Windows uses the same steps but has not received a native runtime test. All platforms need a logged-in desktop session. A headless server cannot run this client.

Browser mode does not work in a headless server session. On Linux, `DISPLAY` must exist.

Playwright connects to the installed browser. You do not need `playwright install` or a downloaded Playwright browser.

## Setup on Linux

```bash
cd alditalk-refill
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp config.example.json config.json
chmod 600 config.json
```

Put your telephone number and password in `config.json`.

You can instead set `ALDITALK_USERNAME` and `ALDITALK_PASSWORD`.

## Setup on macOS

Install Google Chrome and Python 3 first.

```bash
cd alditalk-refill
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp config.example.json config.json
chmod 600 config.json
```

The macOS browser path is detected automatically for a standard Chrome installation.

## Setup on Windows

Install Google Chrome and Python 3 first. Run these commands in PowerShell:

```powershell
cd alditalk-refill
py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item config.example.json config.json
```

The watcher lock and browser path support Windows. Native Windows runtime testing remains pending.

## Instructions for AI agents

Use this checklist to deploy this client on a new machine. Read `aldi.py` before you deviate from it.

1. Install Python 3.10 or later and Google Chrome.
2. Clone the repository into the user's home directory.
3. Create a virtual environment and install `requirements.txt`.
4. Copy `config.example.json` to `config.json`. On Linux and macOS run `chmod 600 config.json`.
5. Ask the user for their portal phone number and password. Write them into `config.json`, or set `ALDITALK_USERNAME` and `ALDITALK_PASSWORD` in the session environment.
6. Run the read-only check command. Confirm it prints an offer name, status `active`, and balances.
7. Start the watcher only after desktop login. Use systemd (Linux), a LaunchAgent (macOS), or Task Scheduler with "run only when user is logged on" (Windows).

Platform notes:

- Linux and macOS: `.venv/bin/python aldi.py check`
- Windows PowerShell: `.venv\Scripts\python.exe aldi.py check`
- macOS detects a standard Chrome install automatically. Set `chrome_path` when detection fails.
- Windows is code-complete but not runtime-tested. Report failures to the user instead of improvising fixes.

Guardrails:

- Never commit, print, transmit, or copy `config.json` or `.chrome-profile` contents. Both contain live credentials and session cookies.
- Never run two watcher processes against one Chrome profile.
- Never invent or enable `otp_command`. Only the user provides one.
- If login ends in an SMS verification prompt, stop and tell the user. Do not loop retries.
- Do not book (`book`) unless the user asked for that exact action. `check` and `probe` are the read-only commands.

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

Keep `transport` set to `browser`. The `api` transport exists for diagnosis and currently triggers SMS verification.

Set `chrome_path` only when automatic detection fails.

The Chrome profile contains authenticated cookies. Keep it private and never commit or share it.

Use one profile directory for one account. Do not run two project processes against the same profile.

Set the interval to 3600 seconds. Random jitter prevents checks from using a fixed schedule.

## Verify the account

Run the read-only command:

```bash
.venv/bin/python aldi.py check
```

Expected output starts with:

```text
Logged in with headed Chrome.
Offer: ... status=active
```

The command does not call the bot check or book data.

## Run one eligible refill

Run this command only when the portal marks the refill as eligible:

```bash
.venv/bin/python aldi.py book
```

Expected result:

```text
Booked 1 GB and verified the new balance.
```

The client rejects an early booking. It reads the live eligibility flag and threshold first.

## Run the watcher

```bash
.venv/bin/python aldi.py watch
```

The watcher keeps Chrome and the authenticated session open. It restarts Chrome after a dead session.

The watcher backs off after transient failures. A credential rejection or unresolved SMS verification stops the process.

Run the watcher after desktop login. Do not run it through headless cron or a server without a desktop.

Keep the service logs during early use. They record checks, verified refills, backoff, and fatal failures.

## Run at desktop startup

The included Linux user service starts the watcher after graphical login. It assumes the project path is `~/alditalk-refill`.

Install and start it:

```bash
mkdir -p ~/.config/systemd/user
ln -sfn "$PWD/systemd/alditalk-refill.service" ~/.config/systemd/user/alditalk-refill.service
systemctl --user daemon-reload
systemctl --user enable --now alditalk-refill.service
systemctl --user status alditalk-refill.service
```

Read its recent logs:

```bash
journalctl --user -u alditalk-refill.service -n 50
```

Stop and disable it:

```bash
systemctl --user disable --now alditalk-refill.service
```

The service does not restart a fatal exit. The watcher handles transient errors inside its process.

On macOS, use a LaunchAgent.

On Windows, use Task Scheduler with the option to run only when the user is logged on.

## SMS verification fallback

Headed Chrome currently avoids SMS verification. ALDI can still require it for a future session.

If `validateBotScore` returns true, the client follows the portal's official sequence:

1. `POST /v1/generateOtp`
2. Read one new six-digit code.
3. `POST /v1/validateOtp`
4. `POST /v1/offer/updateUnlimited`

Without `otp_command`, the client stops before it sends an SMS.

An OTP provider must return one six-digit code on standard output. The client does not invoke a shell.

The client sets `ALDITALK_OTP_REQUESTED_AT` to a Unix timestamp. The provider must ignore older messages.

Do not forward OTP messages through a public webhook or shared service.

## One account versus several accounts

Use one local project copy and one Chrome profile for your own account first.

For friends, each person can run a separate copy on their own computer. This keeps credentials and sessions on their machine.

A central service for 5 to 20 accounts creates larger credential, policy, and anti-bot risks. This repository does not implement that model.

Do not synchronize many accounts behind one host. ALDI publishes no automation rate limit or safe polling interval.

## Approaches investigated

### Headed Chrome with page-context API calls

Result: selected.

A fresh real Chrome 151 profile returned no OTP requirement. One controlled browser refill increased the balance.

This transport preserves the real browser fingerprint. It removes routine dependency on dashboard selectors.

### Direct Python client

Result: retained only as `transport: "api"` for diagnosis.

Login, offer reads, and threshold detection work. The bot check returned `botProtectionOtpRequired: true` on the test account.

### Headless Chrome

Result: rejected.

Real Chrome 151 in headless mode reported no WebDriver marker. ALDI still required OTP.

This result shows that hiding `navigator.webdriver` is insufficient.

### `gommzystudio/AldiTalk-True-Unlimited`

Result: not used.

The [repository](https://github.com/gommzystudio/AldiTalk-True-Unlimited) starts a new headless browser every 15 minutes. It clicks `1 GB` without balance verification.

Its success log can occur when the click only opens the OTP dialog.

### `Liljanameti/alditalk-auto`

Result: not used.

The [repository](https://github.com/Liljanameti/alditalk-auto) uses Puppeteer stealth. Its source reports that stealth remains insufficient when ALDI requires OTP.

### Direct update without bot validation

Result: rejected.

One public Go project posts directly to `updateUnlimited`. That sequence skips ALDI's bot-validation request.

This client keeps `validateBotScore` in the official sequence.

### Browser TLS impersonation

Result: rejected.

TLS impersonation cannot reproduce all headed-browser signals. Real headless Chrome already failed the same bot check.

### Official OTP endpoints

Result: implemented as a fallback.

The portal bundle uses `generateOtp` and `validateOtp`. A live OTP-generation request succeeded and sent one SMS.

The endpoints can return empty success bodies. The client checks their HTTP status without requiring JSON.

## Tests and evidence

Run the automated tests:

```bash
.venv/bin/python -m unittest -v
```

The tests cover login callbacks, offer selection, threshold boundaries, booking payloads, OTP handling, session expiry, write safety, and browser transport behavior.

The source HAR is outside this repository. It contains account data and must remain private.

## Known limits

- The client selects the first subscription in ALDI's navigation response.
- macOS and Windows have not received native runtime tests.
- Chrome must run inside a graphical desktop session.
- Credentials remain in local configuration unless environment variables supply them.
- The dedicated Chrome profile contains sensitive session cookies.
- ALDI publishes no idempotency key for the refill request.
- ALDI can change its portal, bot check, authentication, or private endpoints.
- ALDI can require SMS verification again.

Do not call this implementation bulletproof. The selected design is the lowest-maintenance path verified on the current account and portal.
