import base64
import binascii
import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlencode, urljoin, urlsplit

if os.name == "nt":
    import msvcrt
else:
    import fcntl

try:
    import requests
except ImportError:
    sys.exit("Install dependencies with: python3 -m pip install -r requirements.txt")

PORTAL_BASE = "https://www.alditalk-kundenportal.de"
AUTH_BASE = "https://login.alditalk-kundenbetreuung.de"
AUTH_API = f"{AUTH_BASE}/signin/json/authenticate"
REALM = "/alditalk"
SERVICE = "Login"
OVERVIEW_URL = f"{PORTAL_BASE}/portal/auth/uebersicht/"
BFF207 = "/scs/bff/scs-207-customer-master-data-bff/customer-master-data"
BFF209 = "/scs/bff/scs-209-selfcare-dashboard-bff/selfcare-dashboard"

KIB_PER_GB = 1048576
RESEND_API_URL = "https://api.resend.com/emails"
CONFIG_PATH = Path(__file__).parent / "config.json"
LOCK_PATH = Path(__file__).parent / ".watch.lock"
DEFAULT_CHROME_PROFILE_PATH = Path(__file__).parent / ".chrome-profile"
JITTER_RANDOM = secrets.SystemRandom()


class SessionDead(Exception):
    pass


class LoginRejected(Exception):
    pass


class OtpRequired(RuntimeError):
    pass


def load_config():
    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        cfg = {}
    except json.JSONDecodeError as exc:
        sys.exit(f"Invalid config.json: {exc}")
    if not isinstance(cfg, dict):
        sys.exit("config.json must contain one JSON object.")

    configured_username = cfg.get("username", "")
    configured_password = cfg.get("password", "")
    if os.name != "nt" and CONFIG_PATH.exists() and (
        configured_username or configured_password
    ):
        mode = stat.S_IMODE(CONFIG_PATH.stat().st_mode)
        if mode & 0o077:
            sys.exit("config.json contains credentials. Run: chmod 600 config.json")

    username_from_env = os.environ.get("ALDITALK_USERNAME")
    password_from_env = os.environ.get("ALDITALK_PASSWORD")
    cfg["username"] = username_from_env or configured_username
    cfg["password"] = password_from_env or configured_password

    if not isinstance(cfg["username"], str) or not isinstance(cfg["password"], str):
        sys.exit("username and password must be strings.")
    placeholders = ("HIER", "YOUR_PHONE", "YOUR_PASSWORD")
    if any(
        marker in value.upper()
        for marker in placeholders
        for value in (cfg["username"], cfg["password"])
    ):
        sys.exit(
            "Fill username/password into config.json or set "
            "ALDITALK_USERNAME and ALDITALK_PASSWORD."
        )
    if not cfg["username"] or not cfg["password"]:
        sys.exit(
            "Fill username/password into config.json or set "
            "ALDITALK_USERNAME and ALDITALK_PASSWORD."
        )

    try:
        interval = int(cfg.get("watch_interval_seconds", 3600))
        jitter = float(cfg.get("jitter_fraction", 0.2))
    except (TypeError, ValueError):
        sys.exit("watch_interval_seconds and jitter_fraction must be numbers.")
    if interval < 60:
        sys.exit("watch_interval_seconds must be at least 60.")
    if not 0 <= jitter <= 0.5:
        sys.exit("jitter_fraction must be between 0 and 0.5.")

    otp_command = cfg.get("otp_command")
    if otp_command in (None, ""):
        otp_command = None
    elif not isinstance(otp_command, list) or not otp_command or not all(
        isinstance(arg, str) and arg for arg in otp_command
    ):
        sys.exit("otp_command must be a non-empty JSON array of command arguments.")
    try:
        otp_timeout = int(cfg.get("otp_timeout_seconds", 120))
    except (TypeError, ValueError):
        sys.exit("otp_timeout_seconds must be a number.")
    if not 30 <= otp_timeout <= 600:
        sys.exit("otp_timeout_seconds must be between 30 and 600.")

    transport = str(cfg.get("transport", "browser")).lower()
    if transport not in ("browser", "api"):
        sys.exit("transport must be browser or api.")

    raw_alerts = cfg.get("alerts")
    if raw_alerts is None:
        alerts_cfg = None
    else:
        if not isinstance(raw_alerts, dict):
            sys.exit("alerts must be an object or null.")
        api_key = raw_alerts.get("resend_api_key")
        if not isinstance(api_key, str) or not api_key:
            sys.exit("alerts.resend_api_key must be a non-empty string.")
        for field in ("from", "to"):
            value = raw_alerts.get(field)
            if not isinstance(value, str) or "@" not in value:
                sys.exit(f"alerts.{field} must contain an email address.")
        try:
            failure_threshold = int(raw_alerts.get("failure_threshold", 3))
        except (TypeError, ValueError):
            sys.exit("alerts.failure_threshold must be a number.")
        if failure_threshold < 1:
            sys.exit("alerts.failure_threshold must be at least 1.")
        alerts_cfg = {
            "resend_api_key": api_key,
            "from": raw_alerts["from"],
            "to": raw_alerts["to"],
            "on_booking": bool(raw_alerts.get("on_booking", True)),
            "on_failure": bool(raw_alerts.get("on_failure", True)),
            "failure_threshold": failure_threshold,
        }

    chrome_path = cfg.get("chrome_path")
    if chrome_path is not None and not isinstance(chrome_path, str):
        sys.exit("chrome_path must be a string or null.")

    profile_setting = cfg.get("chrome_profile_path")
    if profile_setting in (None, ""):
        chrome_profile_path = DEFAULT_CHROME_PROFILE_PATH
    elif not isinstance(profile_setting, str):
        sys.exit("chrome_profile_path must be a string or null.")
    else:
        chrome_profile_path = Path(profile_setting).expanduser()
        if not chrome_profile_path.is_absolute():
            chrome_profile_path = Path(__file__).parent / chrome_profile_path
    chrome_profile_path = chrome_profile_path.resolve()
    project_path = Path(__file__).parent.resolve()
    home_path = Path.home().resolve()
    root_path = Path(chrome_profile_path.anchor)
    if chrome_profile_path in (project_path, home_path, root_path):
        sys.exit("chrome_profile_path must name a dedicated subdirectory.")

    cfg["watch_interval_seconds"] = interval
    cfg["jitter_fraction"] = jitter
    cfg["otp_command"] = otp_command
    cfg["otp_timeout_seconds"] = otp_timeout
    cfg["transport"] = transport
    cfg["chrome_path"] = chrome_path
    cfg["chrome_profile_path"] = chrome_profile_path
    cfg["alerts"] = alerts_cfg
    return cfg


def resolve_secret(value):
    if value.startswith("env:"):
        return os.environ.get(value[4:]) or None
    return value or None


def send_alert(alerts_cfg, subject, body):
    if not alerts_cfg:
        return False
    api_key = resolve_secret(alerts_cfg["resend_api_key"])
    if not api_key:
        print("Alert skipped: resend API key unavailable (env var unset?).")
        return False
    try:
        r = requests.post(
            RESEND_API_URL,
            json={
                "from": alerts_cfg["from"],
                "to": [alerts_cfg["to"]],
                "subject": subject,
                "text": body,
            },
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=20,
        )
    except requests.RequestException as e:
        print(f"Alert delivery failed: {e}")
        return False
    if r.status_code >= 300:
        print(f"Alert delivery failed: HTTP {r.status_code}: {r.text[:200]}")
        return False
    return True


def acquire_watch_lock():
    lock_file = LOCK_PATH.open("a+", encoding="utf-8")
    try:
        if os.name == "nt":
            lock_file.seek(0)
            if not lock_file.read(1):
                lock_file.write(" ")
                lock_file.flush()
            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        lock_file.close()
        sys.exit("Another ALDI refill process already runs in this directory.")
    lock_file.seek(0)
    lock_file.truncate()
    lock_file.write(str(os.getpid()))
    lock_file.flush()
    return lock_file


def request_shutdown(_signal_number, _frame):
    raise KeyboardInterrupt


class AldiTalk:
    def __init__(self, username, password, *, otp_command=None, otp_timeout_seconds=120):
        self.username = username
        self.password = password
        self.otp_command = otp_command
        self.otp_timeout_seconds = otp_timeout_seconds
        self.session = None
        self.contract_id = None

    def _fresh_session(self):
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                ),
                "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            }
        )

    def _auth_headers(self):
        return {
            "Accept-API-Version": "protocol=1.0,resource=2.1",
            "Content-Type": "application/json",
            "X-Username": "anonymous",
            "X-Password": "anonymous",
            "X-NoSession": "true",
            "Origin": AUTH_BASE,
            "Referer": f"{AUTH_BASE}/signin/XUI/",
        }

    def login(self):
        self._fresh_session()
        self.session.get(OVERVIEW_URL, allow_redirects=True, timeout=30)

        r = self.session.post(
            AUTH_API,
            params={
                "realm": REALM,
                "authIndexType": "service",
                "authIndexValue": SERVICE,
            },
            json={},
            headers=self._auth_headers(),
            timeout=30,
        )
        r.raise_for_status()
        payload = r.json()
        auth_id = payload.get("authId")
        callbacks = payload.get("callbacks")
        if not auth_id or callbacks is None:
            raise RuntimeError("Unexpected authentication response.")

        pow_work, pow_difficulty = self._extract_pow(callbacks)
        pow_solution = self._solve_pow(pow_work, pow_difficulty) if pow_work else "0"
        callbacks = self._fill_callbacks(callbacks, pow_solution)

        r = self.session.post(
            AUTH_API,
            params={
                "realm": REALM,
                "authIndexType": "service",
                "authIndexValue": SERVICE,
            },
            json={"authId": auth_id, "callbacks": callbacks},
            headers=self._auth_headers(),
            timeout=30,
        )
        r.raise_for_status()
        result = r.json()
        success_url = result.get("successUrl")
        if not success_url:
            reason = str(
                result.get("message") or result.get("detail") or "unknown response"
            )
            lowered = reason.lower()
            if "passwort" in lowered or "password" in lowered or "login" in lowered:
                raise LoginRejected(reason)
            raise RuntimeError(f"Login failed: {reason}")

        resolved = (
            success_url
            if success_url.startswith("http")
            else urljoin(AUTH_BASE, success_url)
        )
        self.session.headers.update({"Accept": "text/html,application/xhtml+xml,*/*"})
        self.session.get(resolved, allow_redirects=True, timeout=60)

        r = self.session.get(OVERVIEW_URL, allow_redirects=True, timeout=30)
        final_url = r.url or ""
        if r.status_code != 200 or AUTH_BASE in final_url or "/signin" in final_url:
            raise RuntimeError(
                "Login failed: portal did not reach authenticated overview."
            )

        self.contract_id = self._get_contract_id()
        print("Logged in.")

    def _extract_pow(self, callbacks):
        for cb in callbacks:
            if cb.get("type") != "TextOutputCallback":
                continue
            message = next(
                (
                    o.get("value", "")
                    for o in cb.get("output", [])
                    if o.get("name") == "message"
                ),
                "",
            )
            work_match = re.search(r'var work\s*=\s*"([^"]+)"', message)
            diff_match = re.search(r"var difficulty\s*=\s*(\d+)", message)
            if work_match:
                return work_match.group(1), int(
                    diff_match.group(1)
                ) if diff_match else 3
        return None, 3

    def _solve_pow(self, work, difficulty):
        if not isinstance(difficulty, int) or not 1 <= difficulty <= 7:
            raise RuntimeError(
                "Authentication returned an invalid proof-of-work difficulty."
            )
        prefix = "0" * difficulty
        nonce = 0
        deadline = time.monotonic() + 30
        while True:
            digest = hashlib.sha1(
                f"{work}{nonce}".encode(), usedforsecurity=False
            ).hexdigest()
            if digest.startswith(prefix):
                return str(nonce)
            nonce += 1
            if nonce % 10_000 == 0 and time.monotonic() >= deadline:
                raise RuntimeError("Authentication proof-of-work exceeded 30 seconds.")

    def _fill_callbacks(self, callbacks, pow_solution):
        for cb in callbacks:
            ctype = cb.get("type", "")
            inputs = cb.get("input", [])
            outputs = cb.get("output", [])
            if ctype == "HiddenValueCallback":
                is_pow = any(
                    o.get("name") == "id" and o.get("value") == "proofOfWorkNonce"
                    for o in outputs
                )
                if is_pow:
                    for item in inputs:
                        item["value"] = pow_solution
                    continue
                fallback = next(
                    (o.get("value") for o in outputs if o.get("name") == "value"), None
                )
                if fallback is not None:
                    for item in inputs:
                        item["value"] = fallback
                continue
            if ctype == "NameCallback":
                for item in inputs:
                    item["value"] = self.username
            elif ctype == "PasswordCallback":
                for item in inputs:
                    item["value"] = self.password
            elif ctype == "ConfirmationCallback":
                for item in inputs:
                    item["value"] = 2
        return callbacks

    def _portal_request(self, method, path, *, params=None, body=None):
        request = getattr(self.session, method)
        kwargs = {
            "headers": {
                "Accept": "application/json, text/plain, */*",
                "Referer": OVERVIEW_URL,
            },
            "allow_redirects": True,
            "timeout": 30,
        }
        if params is not None:
            kwargs["params"] = params
        if body is not None:
            kwargs["json"] = body
            kwargs["headers"].update(
                {"Content-Type": "application/json", "Origin": PORTAL_BASE}
            )
        r = request(PORTAL_BASE + path, **kwargs)
        ctype = r.headers.get("content-type", "")
        final_url = r.url or ""
        if (
            r.status_code == 490
            or r.status_code in (401, 403)
            or AUTH_BASE in final_url
            or "/signin" in final_url
        ):
            raise SessionDead(f"HTTP {r.status_code} on {path}")
        r.raise_for_status()
        if "text/html" in ctype:
            raise SessionDead(f"Portal returned a login page on {path}")
        return r

    def _portal_request_json(self, method, path, *, params=None, body=None):
        r = self._portal_request(method, path, params=params, body=body)
        try:
            return r.json()
        except ValueError as exc:
            raise RuntimeError(f"Portal returned invalid JSON on {path}") from exc

    def _portal_get_json(self, path, params=None):
        return self._portal_request_json("get", path, params=params or {})

    def _portal_post_json(self, path, body):
        return self._portal_request_json("post", path, body=body)

    def _portal_post_status(self, path, body):
        return self._portal_request("post", path, body=body).status_code

    def _decode_msisdn(self):
        lgrs = self.session.cookies.get("lgrs_id", "")
        if not lgrs:
            return ""
        try:
            return base64.b64decode(lgrs + "=" * (-len(lgrs) % 4)).decode()
        except (binascii.Error, UnicodeDecodeError, ValueError):
            return ""

    def _get_contract_id(self):
        params = {}
        msisdn = self._decode_msisdn()
        if msisdn:
            params["msisdn"] = msisdn
        payload = self._portal_get_json(f"{BFF207}/v1/navigation-list", params)
        subs = payload.get("userDetails", {}).get("subscriptions", [])
        if not subs or not subs[0].get("contractId"):
            raise RuntimeError("No subscription in navigation-list.")
        return subs[0]["contractId"]

    def get_offer_snapshot(self):
        payload = self._portal_get_json(
            f"{BFF209}/v1/offers",
            {"contractId": self.contract_id, "productType": ""},
        )
        offers = payload.get("subscribedOffers", [])
        if not offers:
            raise RuntimeError("The offers response has no subscribed offers.")
        offer = next(
            (
                candidate
                for candidate in offers
                if candidate.get("isOnDemandRefillApplicable") is True
                and candidate.get("status") == "active"
            ),
            None,
        )
        if offer is None:
            raise RuntimeError("No active offer is eligible for on-demand refill.")
        packs = [p for p in offer.get("pack", []) if p.get("type") == "data"]
        return payload, offer, packs

    @staticmethod
    def remaining_kb(packs):
        standard = next(
            (
                p
                for p in packs
                if p.get("balanceAttributeReference") != "dataGrantAmountFUP"
            ),
            packs[0] if packs else None,
        )
        if standard is None:
            raise RuntimeError("No data pack found.")
        if standard.get("unit") != "kilobytes":
            raise RuntimeError(f"Unexpected data unit: {standard.get('unit')!r}")
        try:
            return int(standard["allocated"]) - int(standard["used"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(
                "Data pack has invalid allocated or used values."
            ) from exc

    @staticmethod
    def _positive_offer_int(offer, field):
        try:
            value = int(offer[field])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(f"Offer has an invalid {field}.") from exc
        if value <= 0:
            raise RuntimeError(f"Offer has an invalid {field}.")
        return value

    @classmethod
    def refill_is_due(cls, offer, packs):
        if offer.get("isOnDemandRefillApplicable") is not True:
            return False
        if offer.get("status") != "active":
            return False
        threshold = cls._positive_offer_int(offer, "refillThresholdValueUid")
        return cls.remaining_kb(packs) <= threshold

    @classmethod
    def _refill_payload(cls, offer):
        required = (
            "offerId",
            "subscriptionId",
            "resourceId",
            "onDemandAmountValueUid",
            "refillThresholdValueUid",
        )
        missing = [field for field in required if not offer.get(field)]
        if missing:
            raise RuntimeError(f"Offer is missing refill fields: {', '.join(missing)}")
        cls._positive_offer_int(offer, "onDemandAmountValueUid")
        cls._positive_offer_int(offer, "refillThresholdValueUid")
        return {
            "offerId": str(offer["offerId"]),
            "subscriptionId": str(offer["subscriptionId"]),
            "updateOfferResourceID": str(offer["resourceId"]),
            "amount": str(offer["onDemandAmountValueUid"]),
            "refillThresholdValue": str(offer["refillThresholdValueUid"]),
        }

    def _verify_refill(self, previous_remaining_kb, attempts=5, delay_seconds=2):
        for attempt in range(attempts):
            if attempt:
                time.sleep(delay_seconds)
            snapshot = self.get_offer_snapshot()
            if self.remaining_kb(snapshot[2]) > previous_remaining_kb:
                return snapshot
        raise RuntimeError(
            "Booking returned success, but the data balance did not increase."
        )

    def _otp_identifiers(self):
        phone = self._decode_msisdn()
        uid = self.session.cookies.get("user_id", "")
        if not phone or not uid:
            raise OtpRequired("ALDI did not provide the identifiers needed for OTP.")
        return phone, uid

    def _request_otp(self, phone, uid):
        try:
            self._portal_post_status(
                f"{BFF209}/v1/generateOtp",
                {"telephoneNumber": phone, "uid": uid},
            )
        except SessionDead:
            raise
        except requests.RequestException as exc:
            raise OtpRequired(
                "ALDI could not send the OTP. The process stopped to avoid repeated SMS messages."
            ) from exc

    def _read_otp(self, requested_at=None):
        if not self.otp_command:
            raise OtpRequired(
                "ALDI requires SMS verification, but no OTP provider is configured."
            )

        deadline = time.monotonic() + self.otp_timeout_seconds
        command_env = os.environ.copy()
        command_env["ALDITALK_OTP_REQUESTED_AT"] = str(
            int(time.time()) if requested_at is None else requested_at
        )
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            try:
                result = subprocess.run(
                    self.otp_command,
                    capture_output=True,
                    text=True,
                    timeout=max(1, min(15, remaining)),
                    check=False,
                    env=command_env,
                )
            except subprocess.TimeoutExpired:
                continue
            except OSError as exc:
                raise OtpRequired("The configured OTP provider could not start.") from exc

            candidate = result.stdout.strip()
            if result.returncode == 0 and re.fullmatch(r"\d{6}", candidate):
                return candidate

            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(2, remaining))

        raise OtpRequired(
            "The OTP provider did not return one six-digit code before the timeout."
        )

    def _validate_otp(self, phone, uid, otp):
        if not re.fullmatch(r"\d{6}", otp):
            raise OtpRequired("The OTP provider returned an invalid code.")
        try:
            self._portal_post_status(
                f"{BFF209}/v1/validateOtp",
                {"telephoneNumber": phone, "uid": uid, "otp": otp},
            )
        except SessionDead:
            raise
        except requests.RequestException as exc:
            raise OtpRequired(
                "ALDI rejected the OTP. The process stopped to avoid repeated attempts."
            ) from exc

    def book_one_gb(self):
        _, offer, packs = self.get_offer_snapshot()
        previous_remaining = self.remaining_kb(packs)
        if not self.refill_is_due(offer, packs):
            raise RuntimeError("Refill is not eligible at the current data balance.")

        bot_score = self._portal_get_json(f"{BFF209}/v1/offer/validateBotScore")
        otp_required = bot_score.get("botProtectionOtpRequired")
        if not isinstance(otp_required, bool):
            raise RuntimeError("The bot-protection response has an invalid OTP flag.")
        if otp_required:
            if not self.otp_command:
                raise OtpRequired(
                    "ALDI requires SMS verification, but no OTP provider is configured."
                )
            phone, uid = self._otp_identifiers()
            otp_requested_at = int(time.time())
            self._request_otp(phone, uid)
            otp = self._read_otp(otp_requested_at)
            self._validate_otp(phone, uid, otp)

        payload = self._refill_payload(offer)
        self._portal_post_json(f"{BFF209}/v1/offer/updateUnlimited", payload)
        snapshot = self._verify_refill(previous_remaining)
        amount_kb = self._positive_offer_int(offer, "onDemandAmountValueUid")
        print(f"Booked {amount_kb / KIB_PER_GB:g} GB and verified the new balance.")
        return snapshot

    def ensure_session(self):
        if self.session is None or self.contract_id is None:
            self.login()
            return self.get_offer_snapshot()
        try:
            return self.get_offer_snapshot()
        except SessionDead:
            print("Session dead - re-authenticating...")
            self.login()
            return self.get_offer_snapshot()


class ChromeAldiTalk(AldiTalk):
    """Use a normal headed Chrome session, then call the portal API in that page."""

    def __init__(
        self,
        username,
        password,
        *,
        chrome_path=None,
        chrome_profile_path=DEFAULT_CHROME_PROFILE_PATH,
        otp_command=None,
        otp_timeout_seconds=120,
    ):
        super().__init__(
            username,
            password,
            otp_command=otp_command,
            otp_timeout_seconds=otp_timeout_seconds,
        )
        self.chrome_path = chrome_path
        self.chrome_profile_path = Path(chrome_profile_path)
        self._chrome_process = None
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None

    def _find_chrome(self):
        if self.chrome_path:
            configured = Path(self.chrome_path).expanduser()
            if configured.is_file():
                return str(configured)
            resolved = shutil.which(self.chrome_path)
            if resolved:
                return resolved
            raise RuntimeError(f"Chrome was not found at {self.chrome_path!r}.")

        names = (
            "google-chrome",
            "google-chrome-stable",
            "chromium",
            "chromium-browser",
        )
        for name in names:
            resolved = shutil.which(name)
            if resolved:
                return resolved

        candidates = []
        if sys.platform == "darwin":
            candidates.extend(
                [
                    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                    "/Applications/Chromium.app/Contents/MacOS/Chromium",
                ]
            )
        elif os.name == "nt":
            for root_name in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
                root = os.environ.get(root_name)
                if root:
                    candidates.extend(
                        [
                            str(Path(root) / "Google/Chrome/Application/chrome.exe"),
                            str(Path(root) / "Chromium/Application/chrome.exe"),
                        ]
                    )
        candidates.append("/opt/google/chrome/google-chrome")
        for candidate in candidates:
            if Path(candidate).is_file():
                return candidate
        raise RuntimeError(
            "Google Chrome or Chromium is required. Set chrome_path in config.json."
        )

    @staticmethod
    def _free_local_port():
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]

    def _start_browser(self):
        if self._page is not None and not self._page.is_closed():
            return
        if sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
            raise RuntimeError(
                "Browser mode requires a graphical desktop. DISPLAY is not set."
            )
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError(
                "Install dependencies with: python3 -m pip install -r requirements.txt"
            ) from exc

        self.chrome_profile_path.mkdir(mode=0o700, parents=True, exist_ok=True)
        if os.name != "nt":
            self.chrome_profile_path.chmod(0o700)
        chrome = self._find_chrome()
        port = self._free_local_port()
        args = [
            chrome,
            f"--remote-debugging-port={port}",
            "--remote-debugging-address=127.0.0.1",
            f"--remote-allow-origins=http://127.0.0.1:{port}",
            f"--user-data-dir={self.chrome_profile_path}",
            "--profile-directory=Default",
            "--disable-extensions",
            "--start-minimized",
            "--window-position=-32000,-32000",
            "--no-first-run",
            "--no-default-browser-check",
            "about:blank",
        ]
        try:
            self._chrome_process = subprocess.Popen(
                args,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            endpoint = f"http://127.0.0.1:{port}"
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if self._chrome_process.poll() is not None:
                    raise RuntimeError("Chrome stopped during startup.")
                try:
                    response = requests.get(f"{endpoint}/json/version", timeout=1)
                    if response.status_code == 200:
                        break
                except requests.RequestException:
                    pass
                time.sleep(0.2)
            else:
                raise RuntimeError("Chrome did not open its local control port.")

            self._playwright = sync_playwright().start()
            self._browser = self._playwright.chromium.connect_over_cdp(endpoint)
            if not self._browser.contexts:
                raise RuntimeError("Chrome did not create a browser context.")
            self._context = self._browser.contexts[0]
            self._page = self._context.pages[-1]
            if self._page.evaluate("navigator.webdriver") is not False:
                raise RuntimeError(
                    "Chrome exposed browser automation. Headless mode is not supported."
                )
        except Exception:
            self.close()
            raise

    @staticmethod
    def _authenticated_portal_url(url):
        parsed = urlsplit(url)
        return (
            parsed.netloc == "www.alditalk-kundenportal.de"
            and parsed.path.startswith("/portal/auth/")
        )

    def _accept_cookie_dialog(self):
        self._page.evaluate(
            """() => {
                const visit = root => {
                    for (const element of root.querySelectorAll('*')) {
                        const text = (element.innerText || element.textContent || '').trim();
                        const isButton = element.tagName === 'BUTTON'
                            || element.getAttribute('role') === 'button';
                        if (isButton && (text === 'Akzeptieren'
                            || text === 'Alle akzeptieren')) {
                            element.click();
                        }
                        if (element.shadowRoot) visit(element.shadowRoot);
                    }
                };
                visit(document);
            }"""
        )

    def login(self):
        self._start_browser()
        try:
            self._page.goto(OVERVIEW_URL, wait_until="domcontentloaded", timeout=60_000)
            if not self._authenticated_portal_url(self._page.url):
                self._page.wait_for_selector(
                    'input[autocomplete="username"]:visible', timeout=30_000
                )
                self._accept_cookie_dialog()
                proof = self._page.locator(
                    'input[name*="proof" i], input[id*="proof" i]'
                )
                if proof.count():
                    self._page.wait_for_function(
                        "element => Boolean(element && element.value)",
                        arg=proof.first.element_handle(),
                        timeout=10_000,
                    )
                self._page.locator(
                    'input[autocomplete="username"]:visible'
                ).fill(self.username)
                self._page.locator(
                    'input[autocomplete="current-password"]:visible'
                ).fill(self.password)
                buttons = self._page.locator("one-button:visible")
                login_button = next(
                    (
                        buttons.nth(index)
                        for index in range(buttons.count())
                        if " ".join(buttons.nth(index).inner_text().split())
                        == "Anmelden"
                    ),
                    None,
                )
                if login_button is None:
                    raise RuntimeError("The login button is not available.")
                # Usercentrics can cover the component after its consent action
                # runs. Invoke ALDI's component handler without a pointer event.
                login_button.evaluate("element => element.click()")

                deadline = time.monotonic() + 60
                while time.monotonic() < deadline:
                    if self._authenticated_portal_url(self._page.url):
                        break
                    failed = self._page.locator('[role="alert"]').filter(
                        has_text="Anmeldung fehlgeschlagen"
                    )
                    if failed.count() and failed.first.is_visible():
                        raise LoginRejected("The portal rejected the login.")
                    if (
                        self._chrome_process is not None
                        and self._chrome_process.poll() is not None
                    ):
                        raise RuntimeError("Chrome stopped during login.")
                    time.sleep(0.25)
                else:
                    raise RuntimeError("The browser login did not reach the portal.")

            self._page.wait_for_load_state("domcontentloaded", timeout=30_000)
            self.contract_id = self._get_contract_id()
        except (LoginRejected, SessionDead):
            raise
        except Exception as exc:
            raise RuntimeError("Browser login failed.") from exc
        print("Logged in with headed Chrome.")

    def _browser_request(self, method, path, *, params=None, body=None):
        if self._page is None or self._page.is_closed():
            raise SessionDead("The browser page is not available.")
        url = PORTAL_BASE + path
        if params:
            url = f"{url}?{urlencode(params)}"
        attempts = 2 if method.lower() == "get" else 1
        for attempt in range(attempts):
            try:
                result = self._page.evaluate(
                    """async ({method, url, body}) => {
                        const options = {
                            method,
                            credentials: 'include',
                            cache: 'no-store',
                            headers: {'Accept': 'application/json, text/plain, */*'},
                        };
                        if (body !== null) {
                            options.headers['Content-Type'] = 'application/json';
                            options.body = JSON.stringify(body);
                        }
                        try {
                            const response = await fetch(url, options);
                            return {
                                status: response.status,
                                url: response.url,
                                contentType: response.headers.get('content-type') || '',
                                text: await response.text(),
                            };
                        } catch (error) {
                            return {error: String(error)};
                        }
                    }""",
                    {"method": method.upper(), "url": url, "body": body},
                )
                break
            except Exception as exc:
                if attempt + 1 == attempts or self._page.is_closed():
                    raise SessionDead("The browser session is unavailable.") from exc
                try:
                    self._page.wait_for_load_state(
                        "domcontentloaded", timeout=10_000
                    )
                except Exception as wait_exc:
                    raise SessionDead("The browser session is unavailable.") from wait_exc

        if result.get("error") and method.lower() == "get":
            raise SessionDead(f"Portal read failed on {path}.")
        if result.get("error"):
            raise RuntimeError(f"Portal request failed on {path}.")
        status = int(result.get("status", 0))
        final_url = result.get("url", "")
        content_type = result.get("contentType", "")
        if (
            status == 490
            or status in (401, 403)
            or AUTH_BASE in final_url
            or "/signin" in final_url
        ):
            raise SessionDead(f"HTTP {status} on {path}")
        if not 200 <= status < 300:
            raise RuntimeError(f"Portal returned HTTP {status} on {path}.")
        if "text/html" in content_type:
            raise SessionDead(f"Portal returned a login page on {path}")
        return result

    def _portal_request_json(self, method, path, *, params=None, body=None):
        result = self._browser_request(method, path, params=params, body=body)
        try:
            return json.loads(result.get("text", ""))
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"Portal returned invalid JSON on {path}") from exc

    def _portal_post_status(self, path, body):
        return self._browser_request("post", path, body=body)["status"]

    def _cookie_value(self, name):
        if self._context is None:
            return ""
        try:
            cookies = self._context.cookies([PORTAL_BASE, AUTH_BASE])
        except Exception as exc:
            raise SessionDead("The browser cookies are unavailable.") from exc
        return next((cookie["value"] for cookie in cookies if cookie["name"] == name), "")

    def _decode_msisdn(self):
        value = self._cookie_value("lgrs_id")
        if not value:
            return ""
        try:
            return base64.b64decode(value + "=" * (-len(value) % 4)).decode()
        except (binascii.Error, UnicodeDecodeError, ValueError):
            return ""

    def _otp_identifiers(self):
        phone = self._decode_msisdn()
        uid = self._cookie_value("user_id")
        if not phone or not uid:
            raise OtpRequired("ALDI did not provide the identifiers needed for OTP.")
        return phone, uid

    def _request_otp(self, phone, uid):
        try:
            self._portal_post_status(
                f"{BFF209}/v1/generateOtp",
                {"telephoneNumber": phone, "uid": uid},
            )
        except SessionDead:
            raise
        except RuntimeError as exc:
            raise OtpRequired(
                "ALDI could not send the OTP. The process stopped to avoid repeated SMS messages."
            ) from exc

    def _validate_otp(self, phone, uid, otp):
        if not re.fullmatch(r"\d{6}", otp):
            raise OtpRequired("The OTP provider returned an invalid code.")
        try:
            self._portal_post_status(
                f"{BFF209}/v1/validateOtp",
                {"telephoneNumber": phone, "uid": uid, "otp": otp},
            )
        except SessionDead:
            raise
        except RuntimeError as exc:
            raise OtpRequired(
                "ALDI rejected the OTP. The process stopped to avoid repeated attempts."
            ) from exc

    def ensure_session(self):
        if self._page is None or self._page.is_closed() or self.contract_id is None:
            self.login()
            return self.get_offer_snapshot()
        try:
            return self.get_offer_snapshot()
        except SessionDead:
            print("Session dead - restarting Chrome and re-authenticating...")
            self.close()
            self.login()
            return self.get_offer_snapshot()

    def close(self):
        browser, playwright, process = (
            self._browser,
            self._playwright,
            self._chrome_process,
        )
        self._page = None
        self._context = None
        self._browser = None
        self._playwright = None
        self._chrome_process = None
        self.contract_id = None
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass
        if playwright is not None:
            try:
                playwright.stop()
            except Exception:
                pass
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def cmd_check(client):
    payload, offer, packs = client.ensure_session()
    rem = client.remaining_kb(packs)
    print(f"Offer: {offer.get('offerName')}  status={offer.get('status')}")
    for p in packs:
        a, u = int(p.get("allocated", 0)), int(p.get("used", 0))
        tag = (
            "roaming"
            if p.get("balanceAttributeReference") == "dataGrantAmountFUP"
            else "domestic"
        )
        print(
            f"  [{tag}] left={(a - u) / KIB_PER_GB:.2f} GB of {a / KIB_PER_GB:.2f} GB"
        )
    print(f"Balance: {payload.get('totalBalance')} EUR")
    print(f"Remaining (domestic): {rem / KIB_PER_GB:.2f} GB")


def cmd_book(client):
    _, _, packs = client.ensure_session()
    print(f"Remaining before booking: {client.remaining_kb(packs) / KIB_PER_GB:.2f} GB")
    _, _, packs = client.book_one_gb()
    print(f"Remaining after booking:  {client.remaining_kb(packs) / KIB_PER_GB:.2f} GB")


def cmd_watch(cfg, client):
    interval = int(cfg.get("watch_interval_seconds", 3600))
    jitter = float(cfg.get("jitter_fraction", 0.2))
    alerts = cfg.get("alerts")
    failures = 0
    while True:
        try:
            _, offer, packs = client.ensure_session()
            rem = client.remaining_kb(packs)
            print(f"[{time.strftime('%F %T')}] {rem / KIB_PER_GB:.2f} GB remaining")
            if client.refill_is_due(offer, packs):
                print("At or below the refill threshold, booking...")
                _, _, packs_after = client.book_one_gb()
                if alerts and alerts["on_booking"]:
                    rem_after = client.remaining_kb(packs_after) / KIB_PER_GB
                    send_alert(
                        alerts,
                        "ALDI TALK refill booked",
                        f"Booked 1 GB at {time.strftime('%F %T')}. "
                        f"Domestic balance is now about {rem_after:.2f} GB.",
                    )
            failures = 0
        except LoginRejected as e:
            if alerts and alerts["on_failure"]:
                send_alert(
                    alerts,
                    "ALDI TALK watcher stopped: login rejected",
                    f"The watcher exited at {time.strftime('%F %T')}.\n{e}",
                )
            sys.exit(f"FATAL credentials rejected: {e}")
        except OtpRequired as e:
            if alerts and alerts["on_failure"]:
                send_alert(
                    alerts,
                    "ALDI TALK watcher stopped: SMS verification required",
                    f"The watcher exited at {time.strftime('%F %T')}.\n{e}\n"
                    "Log in once through the portal to clear it.",
                )
            sys.exit(f"FATAL OTP automation stopped: {e}")
        except (SessionDead, RuntimeError, requests.RequestException, ValueError) as e:
            failures += 1
            wait = min(1800, interval * (2 ** min(failures - 1, 4)))
            print(f"[{time.strftime('%F %T')}] Error ({e}); backing off {wait}s")
            if (
                alerts
                and alerts["on_failure"]
                and failures == alerts["failure_threshold"]
            ):
                send_alert(
                    alerts,
                    f"ALDI TALK watcher failing ({failures} consecutive cycles)",
                    f"Last error at {time.strftime('%F %T')}:\n{e}\n"
                    "The watcher keeps retrying with backoff.",
                )
            time.sleep(wait)
            continue
        time.sleep(interval * (1 + JITTER_RANDOM.uniform(-jitter, jitter)))


def cmd_probe(client):
    start = time.time()
    print("Probing session lifetime (read-only, every 5 min). Ctrl+C to stop.")
    while True:
        try:
            _, _, packs = client.ensure_session()
            hours = (time.time() - start) / 3600
            print(
                f"[{time.strftime('%F %T')}] alive ({client.remaining_kb(packs) / KIB_PER_GB:.2f} GB) - uptime {hours:.2f} h"
            )
        except (SessionDead, RuntimeError, requests.RequestException, ValueError) as e:
            print(
                f"[{time.strftime('%F %T')}] session ended after {(time.time() - start) / 3600:.2f} h: {e}"
            )
            return
        time.sleep(300)


def main():
    usage = "Usage: aldi.py check|book|watch|probe   (check/probe are read-only)"
    if len(sys.argv) != 2 or sys.argv[1] not in ("check", "book", "watch", "probe"):
        sys.exit(usage)
    signal.signal(signal.SIGTERM, request_shutdown)
    cfg = load_config()
    common = {
        "otp_command": cfg["otp_command"],
        "otp_timeout_seconds": cfg["otp_timeout_seconds"],
    }
    if cfg["transport"] == "browser":
        client = ChromeAldiTalk(
            cfg["username"],
            cfg["password"],
            chrome_path=cfg["chrome_path"],
            chrome_profile_path=cfg["chrome_profile_path"],
            **common,
        )
    else:
        client = AldiTalk(cfg["username"], cfg["password"], **common)
    process_lock = (
        acquire_watch_lock()
        if cfg["transport"] == "browser" or sys.argv[1] in ("book", "watch")
        else None
    )
    try:
        {
            "check": lambda: cmd_check(client),
            "book": lambda: cmd_book(client),
            "watch": lambda: cmd_watch(cfg, client),
            "probe": lambda: cmd_probe(client),
        }[sys.argv[1]]()
    except LoginRejected as e:
        sys.exit(f"FATAL credentials rejected: {e}")
    except OtpRequired as e:
        sys.exit(f"FATAL OTP automation stopped: {e}")
    except SessionDead as e:
        sys.exit(f"\nSession could not be established.\n{e}")
    except (RuntimeError, requests.RequestException, ValueError) as e:
        sys.exit(f"FATAL: {e}")
    except KeyboardInterrupt:
        print("Stopped.")
    finally:
        close = getattr(client, "close", None)
        if close is not None:
            close()
        if process_lock is not None:
            process_lock.close()


if __name__ == "__main__":
    main()
