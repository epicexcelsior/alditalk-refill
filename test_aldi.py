import unittest
import json
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

import aldi


class FakeResponse:
    def __init__(
        self,
        payload,
        status_code=200,
        content_type="application/json",
        url=None,
    ):
        self._payload = payload
        self.status_code = status_code
        self.headers = {"content-type": content_type}
        self.url = url or aldi.PORTAL_BASE + "/api"
        self.text = ""

    def json(self):
        if self._payload is None:
            raise ValueError("response body is empty")
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
    def __init__(self, get_responses=None, post_response=None, post_responses=None):
        self.get_responses = list(get_responses or [])
        self.post_response = post_response or FakeResponse({"status": "ok"})
        self.post_responses = list(post_responses or [])
        self.post_calls = []
        self.headers = {}
        self.cookies = {}

    def get(self, url, **kwargs):
        return self.get_responses.pop(0)

    def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        if self.post_responses:
            return self.post_responses.pop(0)
        return self.post_response


class FakeBrowserPage:
    def __init__(self, result):
        self.results = list(result) if isinstance(result, list) else [result]
        self.calls = []
        self.waits = []

    def is_closed(self):
        return False

    def evaluate(self, script, argument=None):
        self.calls.append((script, argument))
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def wait_for_load_state(self, state, timeout=None):
        self.waits.append((state, timeout))


def data_pack(allocated=2_000_000, used=1_500_000):
    return {
        "type": "data",
        "unit": "kilobytes",
        "allocated": str(allocated),
        "used": str(used),
        "balanceAttributeReference": "domesticData",
    }


def offer(**overrides):
    value = {
        "offerId": "offer-live",
        "subscriptionId": "subscription-live",
        "resourceId": "resource-live",
        "status": "active",
        "isOnDemandRefillApplicable": True,
        "refillThresholdValueUid": "1048576",
        "onDemandAmountValueUid": "2097152",
        "pack": [data_pack()],
    }
    value.update(overrides)
    return value


class AldiTalkTests(unittest.TestCase):
    def make_client(self):
        client = aldi.AldiTalk("user", "password")
        client.contract_id = "contract-navigation"
        return client

    def test_snapshot_selects_the_refill_eligible_offer(self):
        client = self.make_client()
        inactive = offer(
            offerId="old-offer",
            status="inactive",
            isOnDemandRefillApplicable=False,
        )
        active = offer()
        client._portal_get_json = lambda path, params=None: {
            "subscribedOffers": [inactive, active]
        }

        _, selected, packs = client.get_offer_snapshot()

        self.assertEqual(selected["offerId"], "offer-live")
        self.assertEqual(packs, active["pack"])

    def test_login_fills_callbacks_and_reaches_the_portal(self):
        callbacks = [
            {"type": "NameCallback", "input": [{"name": "username", "value": ""}]},
            {
                "type": "PasswordCallback",
                "input": [{"name": "password", "value": ""}],
            },
        ]
        session = FakeSession(
            get_responses=[
                FakeResponse({}),
                FakeResponse({}),
                FakeResponse({}, url=aldi.OVERVIEW_URL),
                FakeResponse(
                    {
                        "userDetails": {
                            "subscriptions": [{"contractId": "contract-live"}]
                        }
                    }
                ),
            ],
            post_responses=[
                FakeResponse({"authId": "auth-live", "callbacks": callbacks}),
                FakeResponse({"successUrl": "/continue"}),
            ],
        )
        client = self.make_client()

        with (
            patch.object(aldi.requests, "Session", return_value=session),
            redirect_stdout(StringIO()),
        ):
            client.login()

        submitted = session.post_calls[1][1]["json"]["callbacks"]
        self.assertEqual(submitted[0]["input"][0]["value"], "user")
        self.assertEqual(submitted[1]["input"][0]["value"], "password")
        self.assertEqual(client.contract_id, "contract-live")

    def test_booking_payload_uses_each_live_offer_field(self):
        client = self.make_client()
        live_offer = offer()
        client.get_offer_snapshot = lambda: ({}, live_offer, live_offer["pack"])
        client.session = FakeSession(
            get_responses=[FakeResponse({"botProtectionOtpRequired": False})]
        )
        client._verify_refill = lambda previous: ({}, live_offer, live_offer["pack"])

        with redirect_stdout(StringIO()):
            client.book_one_gb()

        _, request = client.session.post_calls[0]
        self.assertEqual(
            request["json"],
            {
                "offerId": "offer-live",
                "subscriptionId": "subscription-live",
                "updateOfferResourceID": "resource-live",
                "amount": "2097152",
                "refillThresholdValue": "1048576",
            },
        )

    def test_booking_refuses_an_ineligible_offer(self):
        client = self.make_client()
        ineligible = offer(isOnDemandRefillApplicable=False)
        client.get_offer_snapshot = lambda: ({}, ineligible, ineligible["pack"])
        client.session = FakeSession(
            get_responses=[FakeResponse({"botProtectionOtpRequired": False})]
        )

        with self.assertRaisesRegex(RuntimeError, "not eligible"):
            client.book_one_gb()

        self.assertEqual(client.session.post_calls, [])

    def test_booking_stops_before_requesting_otp_without_a_provider(self):
        client = self.make_client()
        live_offer = offer()
        client.get_offer_snapshot = lambda: ({}, live_offer, live_offer["pack"])
        client.session = FakeSession(
            get_responses=[FakeResponse({"botProtectionOtpRequired": True})]
        )

        with self.assertRaisesRegex(aldi.OtpRequired, "OTP provider"):
            client.book_one_gb()

        self.assertEqual(client.session.post_calls, [])

    def test_booking_completes_the_official_otp_flow(self):
        client = aldi.AldiTalk(
            "user",
            "password",
            otp_command=["otp-reader"],
        )
        client.contract_id = "contract-navigation"
        live_offer = offer()
        client.get_offer_snapshot = lambda: ({}, live_offer, live_offer["pack"])
        client._decode_msisdn = lambda: "test-phone"
        client._read_otp = Mock(return_value="123456")
        client._verify_refill = lambda previous: ({}, live_offer, live_offer["pack"])
        client.session = FakeSession(
            get_responses=[FakeResponse({"botProtectionOtpRequired": True})],
            post_responses=[
                FakeResponse(None),
                FakeResponse(None),
                FakeResponse({"status": "ok"}),
            ],
        )
        client.session.cookies["user_id"] = "user-id"

        with redirect_stdout(StringIO()):
            client.book_one_gb()

        self.assertEqual(len(client.session.post_calls), 3)
        generate_url, generate_request = client.session.post_calls[0]
        validate_url, validate_request = client.session.post_calls[1]
        update_url, _ = client.session.post_calls[2]
        self.assertTrue(generate_url.endswith("/v1/generateOtp"))
        self.assertEqual(
            generate_request["json"],
            {"telephoneNumber": "test-phone", "uid": "user-id"},
        )
        self.assertTrue(validate_url.endswith("/v1/validateOtp"))
        self.assertEqual(
            validate_request["json"],
            {
                "telephoneNumber": "test-phone",
                "uid": "user-id",
                "otp": "123456",
            },
        )
        self.assertTrue(update_url.endswith("/v1/offer/updateUnlimited"))

    def test_otp_command_requires_exactly_one_six_digit_code(self):
        client = aldi.AldiTalk(
            "user",
            "password",
            otp_command=["otp-reader"],
            otp_timeout_seconds=1,
        )
        result = Mock(returncode=0, stdout="123456\n", stderr="")

        with patch.object(aldi.subprocess, "run", return_value=result) as run:
            self.assertEqual(client._read_otp(requested_at=1234567890), "123456")

        self.assertEqual(
            run.call_args.kwargs["env"]["ALDITALK_OTP_REQUESTED_AT"], "1234567890"
        )

    def test_watch_stops_instead_of_retrying_an_otp_failure(self):
        client = self.make_client()
        live_offer = offer()
        client.ensure_session = lambda: ({}, live_offer, live_offer["pack"])
        client.book_one_gb = Mock(side_effect=aldi.OtpRequired("OTP failed"))

        with (
            patch.object(aldi.time, "sleep") as sleep,
            self.assertRaisesRegex(SystemExit, "OTP failed"),
            redirect_stdout(StringIO()),
        ):
            aldi.cmd_watch(
                {"watch_interval_seconds": 600, "jitter_fraction": 0.2}, client
            )

        sleep.assert_not_called()

    def test_refill_is_due_at_the_exact_live_threshold(self):
        live_offer = offer(refillThresholdValueUid="1048576")
        packs = [data_pack(allocated=2_000_000, used=951_424)]

        self.assertTrue(aldi.AldiTalk.refill_is_due(live_offer, packs))

    def test_remaining_kb_accepts_portal_decimal_and_exponent_notation(self):
        packs = [
            data_pack(allocated="2.62144E7", used="2.4880202E7"),
        ]

        self.assertEqual(aldi.AldiTalk.remaining_kb(packs), 1_334_198)

    def test_remaining_kb_rejects_fractional_or_non_finite_kib(self):
        for allocated, used in (("NaN", "1"), ("100.5", "1"), ("100", "Infinity")):
            with self.subTest(allocated=allocated, used=used):
                with self.assertRaisesRegex(RuntimeError, "invalid allocated or used"):
                    aldi.AldiTalk.remaining_kb(
                        [data_pack(allocated=allocated, used=used)]
                    )

    def test_refill_verification_rejects_an_unchanged_balance(self):
        client = self.make_client()
        unchanged = offer(pack=[data_pack(allocated=2_000_000, used=1_500_000)])
        client.get_offer_snapshot = lambda: ({}, unchanged, unchanged["pack"])

        with self.assertRaisesRegex(RuntimeError, "did not increase"):
            client._verify_refill(500_000, attempts=1, delay_seconds=0)

    def test_session_expiry_raises_session_dead(self):
        client = self.make_client()
        client.session = FakeSession(get_responses=[FakeResponse({}, status_code=490)])

        with self.assertRaises(aldi.SessionDead):
            client._portal_get_json("/expired")

    def test_portal_get_raises_non_auth_http_errors(self):
        client = self.make_client()
        client.session = FakeSession(get_responses=[FakeResponse({}, status_code=500)])

        with self.assertRaisesRegex(RuntimeError, "HTTP 500"):
            client._portal_get_json("/broken")

    def test_config_dir_override_moves_config_lock_and_profile(self):
        with TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "config.json"
            cfg_path.write_text(json.dumps({"username": "0123", "password": "pw"}))
            cfg_path.chmod(0o600)
            with patch.dict(aldi.os.environ, {"ALDITALK_CONFIG_DIR": tmp}):
                aldi.load_config()
            self.assertEqual(aldi.CONFIG_DIR, Path(tmp).resolve())
            self.assertEqual(aldi.CONFIG_PATH, (Path(tmp) / "config.json").resolve())
            self.assertEqual(aldi.LOCK_PATH, (Path(tmp) / ".watch.lock").resolve())

    def test_config_defaults_to_headed_browser_and_hourly_checks(self):
        with TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            config_path.write_text(
                '{"username":"user","password":"password"}', encoding="utf-8"
            )
            config_path.chmod(0o600)
            with patch.dict(
                aldi.os.environ, {"ALDITALK_CONFIG_DIR": directory}
            ):
                config = aldi.load_config()

        self.assertEqual(config["transport"], "browser")
        self.assertEqual(config["watch_interval_seconds"], 3600)
        self.assertEqual(config["chrome_profile_path"], aldi.DEFAULT_CHROME_PROFILE_PATH)

    def test_config_rejects_a_broad_browser_profile_path(self):
        with TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            config_path.write_text(
                '{"username":"user","password":"password",'
                '"chrome_profile_path":"."}',
                encoding="utf-8",
            )
            config_path.chmod(0o600)
            with (
                patch.dict(aldi.os.environ, {"ALDITALK_CONFIG_DIR": directory}),
                self.assertRaisesRegex(SystemExit, "dedicated subdirectory"),
            ):
                aldi.load_config()

    def test_browser_transport_fetches_json_in_the_page_context(self):
        client = aldi.ChromeAldiTalk("user", "password")
        client._page = FakeBrowserPage(
            {
                "status": 200,
                "url": aldi.PORTAL_BASE + "/live",
                "contentType": "application/json",
                "text": '{"result":"ok"}',
            }
        )

        result = client._portal_get_json("/live", {"contractId": "contract live"})

        self.assertEqual(result, {"result": "ok"})
        argument = client._page.calls[0][1]
        self.assertEqual(argument["method"], "GET")
        self.assertTrue(argument["url"].endswith("contractId=contract+live"))

    def test_browser_transport_treats_auth_failure_as_a_dead_session(self):
        client = aldi.ChromeAldiTalk("user", "password")
        client._page = FakeBrowserPage(
            {
                "status": 490,
                "url": aldi.PORTAL_BASE + "/expired",
                "contentType": "application/json",
                "text": "{}",
            }
        )

        with self.assertRaises(aldi.SessionDead):
            client._portal_get_json("/expired")

    def test_browser_transport_treats_a_failed_read_as_a_dead_session(self):
        client = aldi.ChromeAldiTalk("user", "password")
        client._page = FakeBrowserPage({"error": "TypeError: Failed to fetch"})

        with self.assertRaises(aldi.SessionDead):
            client._portal_get_json("/offers")

    def test_browser_transport_retries_a_get_interrupted_by_navigation(self):
        client = aldi.ChromeAldiTalk("user", "password")
        client._page = FakeBrowserPage(
            [
                RuntimeError("execution context was destroyed"),
                {
                    "status": 200,
                    "url": aldi.PORTAL_BASE + "/live",
                    "contentType": "application/json",
                    "text": '{"result":"ok"}',
                },
            ]
        )

        self.assertEqual(client._portal_get_json("/live"), {"result": "ok"})
        self.assertEqual(len(client._page.calls), 2)
        self.assertEqual(client._page.waits, [("domcontentloaded", 10_000)])

    def test_browser_transport_does_not_retry_an_interrupted_post(self):
        client = aldi.ChromeAldiTalk("user", "password")
        client._page = FakeBrowserPage(
            [
                RuntimeError("execution context was destroyed"),
                {
                    "status": 200,
                    "url": aldi.PORTAL_BASE + "/live",
                    "contentType": "application/json",
                    "text": "{}",
                },
            ]
        )

        with self.assertRaises(aldi.SessionDead):
            client._portal_post_json("/live", {"value": "one"})
        self.assertEqual(len(client._page.calls), 1)

    def test_positive_offer_int_accepts_scientific_notation(self):
        self.assertEqual(
            aldi.AldiTalk._positive_offer_int({"field": "1.048576E6"}, "field"),
            1_048_576,
        )
        self.assertEqual(
            aldi.AldiTalk._positive_offer_int({"field": "2097152"}, "field"),
            2_097_152,
        )
        for bad in (True, False, 0, -1, "-10", "abc", "100.5", "NaN"):
            with self.subTest(bad=bad):
                with self.assertRaises(RuntimeError):
                    aldi.AldiTalk._positive_offer_int({"field": bad}, "field")

    def test_refill_payload_normalizes_scientific_notation(self):
        live_offer = offer(
            onDemandAmountValueUid="2.097152E6",
            refillThresholdValueUid="1.048576E6",
        )
        payload = aldi.AldiTalk._refill_payload(live_offer)
        self.assertEqual(payload["amount"], "2097152")
        self.assertEqual(payload["refillThresholdValue"], "1048576")

    def test_chrome_ensure_session_recovers_on_runtime_error(self):
        client = aldi.ChromeAldiTalk("user", "password")
        client._page = FakeBrowserPage({"status": 200})
        client.contract_id = "test-contract"
        live_offer = offer()

        snapshots = [
            RuntimeError("Portal returned HTTP 503 on /offers."),
            ({}, live_offer, live_offer["pack"]),
        ]

        def fake_snapshot():
            res = snapshots.pop(0)
            if isinstance(res, Exception):
                raise res
            return res

        client.get_offer_snapshot = fake_snapshot
        client.close = Mock()
        client.login = Mock()

        with redirect_stdout(StringIO()):
            payload, selected, packs = client.ensure_session()

        client.close.assert_called_once()
        client.login.assert_called_once()
        self.assertEqual(selected["offerId"], "offer-live")

    def test_chrome_ensure_session_closes_on_double_failure(self):
        client = aldi.ChromeAldiTalk("user", "password")
        client._page = FakeBrowserPage({"status": 200})
        client.contract_id = "test-contract"

        client.get_offer_snapshot = Mock(
            side_effect=RuntimeError("Portal returned HTTP 503 on /offers.")
        )
        client.close = Mock()
        client.login = Mock()

        with (
            redirect_stdout(StringIO()),
            self.assertRaisesRegex(RuntimeError, "HTTP 503"),
        ):
            client.ensure_session()

        self.assertEqual(client.close.call_count, 2)
        client.login.assert_called_once()

    def test_chrome_cleanup_stale_profile_locks(self):
        with TemporaryDirectory() as tmp:
            profile_dir = Path(tmp) / ".chrome-profile"
            profile_dir.mkdir()
            lock = profile_dir / "SingletonLock"
            lock.symlink_to("f5server-12345")
            cookie = profile_dir / "SingletonCookie"
            cookie.symlink_to("123456789")
            socket_file = profile_dir / "SingletonSocket"
            socket_file.symlink_to("/tmp/sock")

            client = aldi.ChromeAldiTalk(
                "user", "password", chrome_profile_path=profile_dir
            )
            client._cleanup_stale_profile_locks()

            self.assertFalse(lock.is_symlink())
            self.assertFalse(cookie.is_symlink())
            self.assertFalse(socket_file.is_symlink())

    def test_cmd_watch_uses_progressive_backoff(self):
        client = self.make_client()
        client.ensure_session = Mock(side_effect=RuntimeError("portal down"))
        cfg = {"watch_interval_seconds": 3600, "jitter_fraction": 0.2}

        sleep_calls = []

        def fake_sleep(dur):
            sleep_calls.append(dur)
            if len(sleep_calls) >= 5:
                raise KeyboardInterrupt

        with (
            patch.object(aldi.time, "sleep", side_effect=fake_sleep),
            redirect_stdout(StringIO()),
        ):
            try:
                aldi.cmd_watch(cfg, client)
            except KeyboardInterrupt:
                pass

        self.assertEqual(sleep_calls, [30, 60, 120, 300, 600])


class AlertsTest(unittest.TestCase):
    ALERTS = {
        "resend_api_key": "env:RESEND_API_KEY",
        "from": "alerts@example.com",
        "to": "user@example.com",
        "on_booking": True,
        "on_failure": True,
        "failure_threshold": 3,
    }

    def make_client(self):
        client = aldi.AldiTalk("user", "password")
        client.contract_id = "contract-navigation"
        return client

    def test_resolve_secret_reads_env_indirection(self):
        with patch.dict(aldi.os.environ, {"RESEND_API_KEY": "re_test123"}):
            self.assertEqual(aldi.resolve_secret("env:RESEND_API_KEY"), "re_test123")
        self.assertIsNone(aldi.resolve_secret("env:MISSING_VAR"))
        self.assertEqual(aldi.resolve_secret("re_literal"), "re_literal")

    def test_send_alert_posts_resend_payload(self):
        sent = {}

        def fake_post(url, **kwargs):
            sent["url"] = url
            sent["kwargs"] = kwargs
            return FakeResponse({"id": "abc"}, status_code=200)

        with (
            patch.dict(aldi.os.environ, {"RESEND_API_KEY": "re_test123"}),
            patch.object(aldi.requests, "post", side_effect=fake_post),
        ):
            ok = aldi.send_alert(self.ALERTS, "Subject", "Body")

        self.assertTrue(ok)
        self.assertEqual(sent["url"], aldi.RESEND_API_URL)
        self.assertEqual(
            sent["kwargs"]["json"]["to"], ["user@example.com"]
        )
        self.assertEqual(sent["kwargs"]["headers"]["Authorization"], "Bearer re_test123")

    def test_send_alert_survives_http_errors(self):
        with (
            patch.dict(aldi.os.environ, {"RESEND_API_KEY": "re_test123"}),
            patch.object(
                aldi.requests, "post", return_value=FakeResponse({}, status_code=422)
            ),
        ):
            self.assertFalse(aldi.send_alert(self.ALERTS, "s", "b"))

    def test_send_alert_without_config_is_a_noop(self):
        self.assertFalse(aldi.send_alert(None, "s", "b"))

    def test_watch_sends_one_booking_alert(self):
        client = self.make_client()
        live_offer = offer()
        client.ensure_session = lambda: ({}, live_offer, live_offer["pack"])
        client.book_one_gb = Mock(return_value=({}, live_offer, live_offer["pack"]))
        cfg = {
            "watch_interval_seconds": 600,
            "jitter_fraction": 0.2,
            "alerts": dict(self.ALERTS, on_failure=False),
        }

        with (
            patch.object(aldi.time, "sleep") as sleep,
            patch.object(aldi, "send_alert") as alert,
            redirect_stdout(StringIO()),
            self.assertRaises(KeyboardInterrupt),
        ):
            sleep.side_effect = KeyboardInterrupt
            aldi.cmd_watch(cfg, client)

        alert.assert_called_once()
        self.assertIn("refill booked", alert.call_args.args[1])

    def test_watch_alerts_once_at_the_failure_threshold(self):
        client = self.make_client()
        client.ensure_session = Mock(side_effect=RuntimeError("boom"))
        cfg = {
            "watch_interval_seconds": 600,
            "jitter_fraction": 0.2,
            "alerts": dict(self.ALERTS, failure_threshold=3),
        }

        with (
            patch.object(aldi.time, "sleep") as sleep,
            patch.object(aldi, "send_alert") as alert,
            redirect_stdout(StringIO()),
        ):
            sleep.side_effect = [None, None, KeyboardInterrupt]
            try:
                aldi.cmd_watch(cfg, client)
            except KeyboardInterrupt:
                pass

        alert.assert_called_once()
        self.assertIn("3 consecutive", alert.call_args.args[1])


if __name__ == "__main__":
    unittest.main()
