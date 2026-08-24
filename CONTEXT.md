# Context for AI agents

Operational facts for setting up and running this client. `aldi.py` is the source of truth. Read this before changing code.

## Production reference (f5server)

- Host: Tailscale `f5server` (100.112.98.110), Ubuntu 22.04, Alaska US
- Path `~/alditalk-refill`; clone via deploy key `~/.ssh/alditalk_deploy_key`, repo `core.sshCommand` set
- Runtime: systemd user unit `alditalk-refill-server.service` (Xvfb-wrapped), Linger on
- Self-update: timer daily 06:00 Berlin ±30 min; test-gated; resets to origin/main
- Resend key `~/.alditalk/resend.env` via drop-in; sender `alerts@mail.epicexcelsior.com`

## Why headed Chrome

ALDI's risk engine verdicts, tested 2026-08:

| Approach | Bot check |
| --- | --- |
| Headed Chrome, persistent profile | Passes, no OTP |
| Headless Chrome | SMS OTP, even with webdriver hidden |
| Pure HTTP client | Cloudflare passes reads, OTP demanded before booking |

Never replace headed Chrome with headless or plain HTTP. Servers use Xvfb.

## Auth flow

1. GET portal overview page.
2. POST ForgeRock `login.alditalk-kundenbetreuung.de/signin/json/authenticate` (realm `/alditalk`, service `Login`). Callbacks:
   - `TextOutputCallback` holds PoW challenge (`var work`, `var difficulty`): find nonce where `sha1(work+nonce)` has N leading zeros -> `HiddenValueCallback` named `proofOfWorkNonce`
   - `NameCallback`/`PasswordCallback` = credentials; `ConfirmationCallback` = `2`
   - Other `HiddenValueCallback`s echo their output value back
3. Follow returned `successUrl` with redirects; OIDC callback sets portal cookies.
4. Verify: overview URL loads with 200 and no `/signin` in final URL.

Endpoints (portal host, same-origin):

```
GET  .../scs-207-customer-master-data-bff/customer-master-data/v1/navigation-list?msisdn=
GET  .../scs-209-selfcare-dashboard-bff/selfcare-dashboard/v1/offers?contractId=&productType=
GET  .../selfcare-dashboard/v1/offer/validateBotScore
POST .../selfcare-dashboard/v1/generateOtp        (OTP flows only)
POST .../selfcare-dashboard/v1/validateOtp        (OTP flows only)
POST .../selfcare-dashboard/v1/offer/updateUnlimited
```

Fields: domestic pack = `pack[]` item with `type=="data"` and `balanceAttributeReference != "dataGrantAmountFUP"`; values in KiB. Booking amount/threshold come live from `refillThresholdValueUid`. HTTP 490 = session dead, re-login once.

## Setup runbook

1. Install Python 3.10+ and Chrome.
2. Clone into home directory; run `./scripts/setup.sh` (Windows: README steps).
3. Fill credentials into `config.json` (number format like portal form, leading `0`).
4. Verify read-only: `.venv/bin/python aldi.py check` (servers: prefix `xvfb-run -a`). Success prints offer name, `active`, balances.
5. Autostart: Linux systemd unit (desktop or `-server` variant), macOS LaunchAgent, Windows Task Scheduler "only when logged on".

One Chrome profile per account. Never two watchers on one profile.

## Operating procedures

```bash
# health
systemctl --user is-active alditalk-refill-server.service
journalctl --user -u alditalk-refill-server -n 20 -o cat --no-pager

# force update now
systemctl --user start alditalk-refill-update.service

# test alert email
cd ~/alditalk-refill && set -a && . ~/.alditalk/resend.env && set +a
xvfb-run -a .venv/bin/python -c "
import json,pathlib,aldi,time
cfg=json.loads((pathlib.Path.home()/'alditalk-refill/config.json').read_text())
print(aldi.send_alert(cfg['alerts'],'ALDI test',time.strftime('%F %T')))"
```

Reboot persistence requires `loginctl show-user <user> | grep Linger=yes`.

## Failure playbook

| Symptom | Meaning | Action |
| --- | --- | --- |
| `Session dead - restarting Chrome` | Normal self-heal | None |
| `FATAL OTP automation stopped` | Risk engine wants SMS | User logs in via real browser once; restart |
| `FATAL credentials rejected` | Bad password | User updates config; restart |
| Booking verified failure | Not applied | Check portal manually; report; no retry |
| Chrome crashes | Display/RAM problem | Check Xvfb, memory |
| Update git error 128 | Deploy key broken | Recreate key, reset `core.sshCommand` |

## Multi-account hosting

One server can host a few accounts. `ALDITALK_CONFIG_DIR` moves config, lock, and default Chrome profile into a per-account directory, so instances stay isolated.

```bash
scripts/account.sh add <name>     # scaffold ~/alditalk-accounts/<name>, staggered interval
scripts/account.sh list
scripts/account.sh remove <name>  # stop + archive
```

Mechanics:

- Template unit: `systemd/alditalk-refill@.service`, instance name = folder name. Shares the repo venv.
- Interval offset: base 3600 s plus a name checksum (0-899 s), so accounts never poll in sync.
- Update script restarts every active `alditalk-refill@*` instance after pulling.
- Alert routing: set each account's own `alerts.to`; one shared Resend key is fine.

Limits and risks. Keep the total at five or fewer:

1. All accounts log in from one IP. Risk flagging is per-account today; a future pattern ban hits everyone at once, owner included.
2. You hold their portal credentials and session cookies on your disk. You are their breach radius.
3. OTP demands halt that person's watcher until they act on their phone.

Consent checklist per person before adding them:

- They know a script holds their password and books refills for them.
- They know which email receives alerts.
- They have a way to reach you when their watcher stops.

## Guardrails

- Never commit/print/transmit `config.json` or `.chrome-profile` (live credentials).
- Never invent keys, numbers, or codes; never enable `otp_command` unprompted.
- Never bypass the bot-check sequence or force bookings to test. `check`/`probe` are safe.
- Stop at the first SMS prompt; ask the user. No retry loops against a risk engine.
- Keep dependencies minimal: requests + playwright only.

## Traps

- Counting watchers: `pgrep -fc "aldi.py watch"` includes wrapper shells. Use `ps`.
- `EnvironmentFile` only applies inside systemd; manual shells must source the env file.
- Balance can rise between cycles without booking (ALDI accounting lag). Trust current-cycle values only.
- Restricted Resend keys cannot list domains; HTTP 200 with `id` is delivery proof.
