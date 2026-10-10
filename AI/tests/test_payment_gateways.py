"""ABA PayWay, ACLEDA KHQR (Bakong) and Binance Pay, end to end.

Every outgoing HTTP call the providers make is routed into the local gateway
emulator (``AI/dev/payment_emulator.py``) through the test client, and the
emulator's own callbacks come back into the real webhook URL the same way. So
these tests run the real provider code — request signing, the hosted form's
hash, RSA webhook verification, KHQR building, status queries — against a
gateway that checks all of it, without touching the network.
"""
from __future__ import annotations

import base64
import json
from decimal import Decimal
from datetime import timedelta
from unittest.mock import patch
from urllib.parse import urlencode, urlsplit

from django.core.cache import cache
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from AI.dev import payment_emulator as emulator
from AI.models import AccountStatus, Payment, PaymentEvent, PaymentStatus, Plan, User
from AI.services import payment_service, settings_service, token_service
from AI.services.payments import PaymentError, get_provider
from AI.services.payments import acleda_xpay as xpay_module
from AI.services.payments.acleda_khqr import build_khqr, crc16, khqr_md5
from AI.services.payments.base import VerificationResult

PASSWORD = "Sup3rSecret!x"
EMU = "http://testserver/dev/payment-emulator"
GATEWAYS = dict(
    SITE_URL="http://testserver",
    ROOT_URLCONF="AI.tests.urls_with_emulator",
    ABA_PAYWAY_MERCHANT_ID="ec-test", ABA_PAYWAY_API_KEY="aba-test-key",
    ABA_PAYWAY_BASE_URL=f"{EMU}/aba", ABA_PAYWAY_CURRENCY="KHR", ABA_PAYWAY_PAYMENT_OPTION="",
    ACLEDA_KHQR_BAKONG_ID="epc_test@aclb", ACLEDA_KHQR_ACCOUNT_NAME="EPC AI ASSISTANT",
    ACLEDA_KHQR_CITY="Phnom Penh", ACLEDA_KHQR_MERCHANT_ID="", ACLEDA_KHQR_ACCOUNT_NUMBER="",
    ACLEDA_KHQR_CURRENCY="KHR",
    BAKONG_API_TOKEN="bakong-test-token", BAKONG_API_BASE_URL=f"{EMU}/bakong",
    BINANCE_PAY_API_KEY="bn-test-key", BINANCE_PAY_API_SECRET="bn-test-secret",
    BINANCE_PAY_BASE_URL=f"{EMU}/binance", BINANCE_PAY_CURRENCY="USDT",
    # What the emulated gateways hold, so a test can change ours and be refused.
    PAYMENT_EMULATOR_CREDENTIALS={
        "ABA_PAYWAY_MERCHANT_ID": "ec-test", "ABA_PAYWAY_API_KEY": "aba-test-key",
        "BINANCE_PAY_API_KEY": "bn-test-key", "BINANCE_PAY_API_SECRET": "bn-test-secret",
        "BAKONG_API_TOKEN": "bakong-test-token",
    },
)


class _Response:
    def __init__(self, response):
        self.status_code = response.status_code
        self.content = response.content
        self.text = response.content.decode("utf-8", "replace")

    def json(self):
        return json.loads(self.content.decode() or "null")


class Network:
    """``requests.post`` for the test: into the site (and emulator) in-process."""

    def __init__(self):
        self.client = Client()
        self.calls: list[str] = []

    def post(self, url, json=None, data=None, headers=None, timeout=None):
        headers = dict(headers or {})
        if json is None and isinstance(data, dict):
            # requests posts a dict as a plain urlencoded form.
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            data = urlencode(data)
        content_type = headers.pop("Content-Type", "application/json")
        body = _json_dumps(json) if json is not None else (data or "")
        extra = {f"HTTP_{name.upper().replace('-', '_')}": value for name, value in headers.items()}
        path = urlsplit(url).path
        self.calls.append(path)
        return _Response(self.client.post(path, data=body, content_type=content_type, **extra))


def _json_dumps(value):
    return json.dumps(value, separators=(",", ":"))


@override_settings(**GATEWAYS)
class GatewayTestCase(TestCase):
    plan_price = Decimal("10.00")

    def setUp(self):
        cache.clear()
        settings_service.bootstrap()
        self.user = User.objects.create_user(
            username="payer", email="payer@test.local", password=PASSWORD,
            is_verified=True, status=AccountStatus.ACTIVE,
        )
        self.plan = Plan.objects.create(name="Student", tokens=100_000, price=self.plan_price, duration_days=30)
        self.network = Network()
        patcher = patch("requests.post", side_effect=self.network.post)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client.force_login(self.user)

    # -- helpers -------------------------------------------------------------------------
    def checkout(self, method: str) -> Payment:
        response = self.client.post(f"/checkout/{self.plan.slug}/", {"method": method})
        self.assertEqual(response.status_code, 302)
        # pk breaks the tie: two orders can share a timestamp on Windows' ~15 ms clock.
        return Payment.objects.filter(user=self.user).latest("created_at", "pk")

    def detail(self, payment: Payment):
        response = self.client.get(f"/payments/{payment.reference}/")
        self.assertEqual(response.status_code, 200)
        return response

    def status(self, payment: Payment) -> dict:
        response = self.client.get(f"/payments/{payment.reference}/status/")
        self.assertEqual(response.status_code, 200)
        return response.json()

    def assertPaid(self, payment: Payment):
        payment.refresh_from_db()
        self.assertEqual(payment.status, PaymentStatus.PAID)
        self.assertTrue(payment.tokens_credited)
        self.assertEqual(token_service.balance_of(self.user), self.plan.tokens)

    def assertNothingCredited(self, payment: Payment, status=PaymentStatus.PENDING):
        payment.refresh_from_db()
        self.assertEqual(payment.status, status)
        self.assertFalse(payment.tokens_credited)
        self.assertEqual(token_service.balance_of(self.user), 0)


# ---------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------
class ChargeQuoteTests(GatewayTestCase):
    def test_riel_is_quoted_at_the_admin_rate_rounded_up_to_100(self):
        with patch.object(payment_service, "khr_rate", return_value=Decimal("4100")):
            self.assertEqual(payment_service.quote_charge("aba_payway", Decimal("25.00")),
                             (Decimal("102500"), "KHR", Decimal("4100")))
            charge, currency, _ = payment_service.quote_charge("acleda_khqr", Decimal("9.99"))
        self.assertEqual((charge, currency), (Decimal("41000"), "KHR"))   # 40,959 rounds up

    def test_binance_charges_usdt_one_to_one(self):
        self.assertEqual(payment_service.quote_charge("binance", Decimal("25")), (Decimal("25.00"), "USDT", None))

    def test_the_order_keeps_usd_for_reports_and_the_charge_separately(self):
        payment = self.checkout("aba_payway")
        self.assertEqual(payment.amount, Decimal("10.00"))
        self.assertEqual(payment.currency, "USD")
        self.assertEqual(payment.charge_currency, "KHR")
        self.assertEqual(payment.charge_amount, Decimal("10.00") * payment.exchange_rate)
        self.assertTrue(payment.pay_display.startswith("៛"))

    def test_checkout_page_offers_all_three_methods_with_their_prices(self):
        response = self.client.get(f"/checkout/{self.plan.slug}/")
        self.assertEqual(response.status_code, 200)
        keys = [option["key"] for option in response.context["options"]]
        self.assertEqual(keys, ["aba_payway", "acleda_khqr", "binance"])
        body = response.content.decode()
        self.assertIn("10.00 USDT", body)
        self.assertIn("៛", body)

    def test_pressing_continue_twice_reuses_the_open_order(self):
        first = self.checkout("binance")
        second = self.checkout("binance")
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(Payment.objects.filter(user=self.user).count(), 1)


# ---------------------------------------------------------------------------
# ABA PayWay
# ---------------------------------------------------------------------------
class ABAPayWayFlowTests(GatewayTestCase):
    def press_pay(self, payment: Payment):
        """The student presses Pay: a fresh, signed hand-off for one attempt."""
        response = self.client.post(f"/payments/{payment.reference}/pay/")
        self.assertEqual(response.status_code, 200)
        return response.context["handoff"]

    def open_gateway(self, payment: Payment, **tamper):
        session = self.detail(payment).context["session"]
        self.assertEqual(session["kind"], "handoff")
        handoff = self.press_pay(payment)
        fields = dict(handoff.fields, **tamper)
        return self.client.post(urlsplit(handoff.action).path, fields), fields

    def act(self, tran_id: str, action: str = "pay"):
        return self.client.post(f"/dev/payment-emulator/aba/act/{tran_id}/", {"action": action})

    def test_full_flow_pay_pushback_verify_and_activate(self):
        payment = self.checkout("aba_payway")
        response, fields = self.open_gateway(payment)
        self.assertEqual(response.status_code, 200)          # the hosted page accepted our hash
        self.assertEqual(fields["currency"], "KHR")
        self.assertEqual(fields["amount"], str(int(payment.charge_amount)))
        self.assertEqual(base64.b64decode(fields["return_url"]).decode(),
                         "http://testserver/webhooks/payments/aba_payway/")
        self.assertNothingCredited(payment)                  # opening the gateway is not paying

        response = self.act(fields["tran_id"])
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].endswith(f"/payments/{payment.reference}/return/"))
        # The pushback arrived and was confirmed with Check Transaction before settling.
        self.assertIn("/webhooks/payments/aba_payway/", self.network.calls)
        self.assertIn("/dev/payment-emulator/aba/api/payment-gateway/v1/payments/check-transaction-2", self.network.calls)
        self.assertPaid(payment)

        # The browser's return lands on the receipt.
        response = self.client.get(f"/payments/{payment.reference}/return/")
        self.assertRedirects(response, f"/payments/{payment.reference}/receipt/", fetch_redirect_response=False)

    def test_a_tampered_amount_is_refused_by_the_gateway(self):
        payment = self.checkout("aba_payway")
        response, _ = self.open_gateway(payment, amount="100")
        self.assertEqual(response.status_code, 400)
        self.assertIn("Wrong hash", response.content.decode())

    def test_a_forged_pushback_without_payment_changes_nothing(self):
        payment = self.checkout("aba_payway")
        _, fields = self.open_gateway(payment)
        response = Client().post("/webhooks/payments/aba_payway/", data=_json_dumps(
            {"tran_id": fields["tran_id"], "status": 0, "apv": "FAKE"}), content_type="application/json")
        self.assertEqual(response.status_code, 200)          # acknowledged…
        self.assertNothingCredited(payment)                  # …but PayWay says it is not paid

    def test_the_browser_return_alone_never_settles(self):
        payment = self.checkout("aba_payway")
        self.open_gateway(payment)
        response = self.client.get(f"/payments/{payment.reference}/return/")
        self.assertRedirects(response, f"/payments/{payment.reference}/", fetch_redirect_response=False)
        self.assertNothingCredited(payment)

    def test_a_declined_payment_is_marked_failed(self):
        payment = self.checkout("aba_payway")
        _, fields = self.open_gateway(payment)
        self.act(fields["tran_id"], "decline")
        self.assertNothingCredited(payment, status=PaymentStatus.FAILED)
        page = self.detail(payment)
        self.assertEqual(page.context["outcome"][0], "The payment did not go through")
        self.assertContains(page, "Try again")

    def test_an_amount_mismatch_is_held_for_review(self):
        payment = self.checkout("aba_payway")
        _, fields = self.open_gateway(payment)
        order = emulator._get("aba", fields["tran_id"])
        order["amount"] = "100"
        emulator._put("aba", fields["tran_id"], order)
        self.act(fields["tran_id"])
        self.assertNothingCredited(payment)
        self.assertTrue(PaymentEvent.objects.filter(payment=payment, event_type="amount_mismatch").exists())

    def test_replayed_pushbacks_credit_once(self):
        payment = self.checkout("aba_payway")
        _, fields = self.open_gateway(payment)
        self.act(fields["tran_id"])
        for _ in range(3):
            Client().post("/webhooks/payments/aba_payway/", data=_json_dumps({"tran_id": fields["tran_id"]}),
                          content_type="application/json")
        self.assertPaid(payment)

    # -- one press of Pay, one tran_id -------------------------------------------------------
    def test_each_press_of_pay_is_a_new_tran_id_that_payway_accepts(self):
        payment = self.checkout("aba_payway")
        first_page, first = self.open_gateway(payment)
        second_page, second = self.open_gateway(payment)
        self.assertEqual((first_page.status_code, second_page.status_code), (200, 200))
        self.assertNotEqual(first["tran_id"], second["tran_id"])
        for tran_id in (first["tran_id"], second["tran_id"]):
            self.assertLessEqual(len(tran_id), 20)
        self.assertEqual(payment.attempts.count(), 2)
        # Re-posting an old form is what PayWay refuses; the site never does it.
        again = self.client.post(urlsplit(f"{EMU}/aba/api/payment-gateway/v1/payments/purchase").path, first)
        self.assertEqual(again.status_code, 400)
        self.assertIn("Duplicated", again.content.decode())

    def test_an_older_attempt_paid_in_another_tab_still_counts(self):
        payment = self.checkout("aba_payway")
        _, first = self.open_gateway(payment)
        self.open_gateway(payment)                            # a second press, never paid
        order = emulator._get("aba", first["tran_id"])
        order["status"], order["apv"] = "APPROVED", "123456"    # paid, but the pushback was lost
        emulator._put("aba", first["tran_id"], order)
        self.assertTrue(self.status(payment)["paid"])
        self.assertPaid(payment)

    def test_the_hosted_page_is_requested_and_shipping_is_explicit(self):
        payment = self.checkout("aba_payway")
        response, fields = self.open_gateway(payment)
        self.assertEqual(response.status_code, 200)          # hash still valid: payment_gate is unsigned
        self.assertEqual(fields["payment_gate"], "0")
        self.assertEqual(fields["shipping"], "0")
        self.assertEqual(emulator._get("aba", fields["tran_id"])["payment_gate"], "0")

    def test_a_riel_payment_settled_in_dollars_still_matches(self):
        payment = self.checkout("aba_payway")
        _, fields = self.open_gateway(payment)
        order = emulator._get("aba", fields["tran_id"])
        order["settled_currency"], order["settled_amount"] = "USD", "10.00"
        emulator._put("aba", fields["tran_id"], order)
        self.act(fields["tran_id"])
        self.assertPaid(payment)

    def test_aba_can_charge_in_dollars(self):
        with self.settings(ABA_PAYWAY_CURRENCY="USD"):
            payment = self.checkout("aba_payway")
            response, fields = self.open_gateway(payment)
            self.assertEqual(response.status_code, 200)            # PayWay accepted the dollar form
            self.assertEqual((fields["currency"], fields["amount"], fields["shipping"]), ("USD", "10.00", "0.00"))
            self.assertEqual(payment.pay_display, "$10.00")
            self.act(fields["tran_id"])
        self.assertPaid(payment)

    def test_the_riel_note_shows_only_when_a_method_charges_riel(self):
        self.assertContains(self.client.get(f"/checkout/{self.plan.slug}/"), "Riel prices use")
        with self.settings(ABA_PAYWAY_CURRENCY="USD", ACLEDA_KHQR_CURRENCY="USD"):
            self.assertNotContains(self.client.get(f"/checkout/{self.plan.slug}/"), "Riel prices use")

    def test_an_order_with_a_stored_form_moves_to_fresh_attempts(self):
        payment = self.checkout("aba_payway")
        Payment.objects.filter(pk=payment.pk).update(checkout_payload={
            "kind": "redirect", "redirect_url": f"{EMU}/aba/api/payment-gateway/v1/payments/purchase",
            "fields": {"tran_id": payment.reference},
        })
        payment.refresh_from_db()
        self.assertEqual(self.detail(payment).context["session"]["kind"], "handoff")

    def test_the_bank_can_send_the_browser_back_without_a_session(self):
        payment = self.checkout("aba_payway")
        _, fields = self.open_gateway(payment)
        order = emulator._get("aba", fields["tran_id"])
        order["status"], order["apv"] = "APPROVED", "654321"
        emulator._put("aba", fields["tran_id"], order)
        stranger = Client(enforce_csrf_checks=True)            # a cross-site POST: no cookie, no token
        response = stranger.post(f"/payments/{payment.reference}/return/")
        self.assertRedirects(response, f"/payments/{payment.reference}/receipt/", fetch_redirect_response=False)
        self.assertPaid(payment)

    def test_pay_is_refused_for_a_closed_order(self):
        payment = self.checkout("aba_payway")
        payment_service.mark_expired(payment, "test")
        response = self.client.post(f"/payments/{payment.reference}/pay/")
        self.assertRedirects(response, f"/payments/{payment.reference}/", fetch_redirect_response=False)
        self.assertEqual(payment.attempts.count(), 0)

    def test_pay_needs_a_post_and_the_owner(self):
        payment = self.checkout("aba_payway")
        self.assertEqual(self.client.get(f"/payments/{payment.reference}/pay/").status_code, 405)
        other = User.objects.create_user(username="other", email="o@test.local", password=PASSWORD,
                                         is_verified=True, status=AccountStatus.ACTIVE)
        intruder = Client()
        intruder.force_login(other)
        self.assertEqual(intruder.post(f"/payments/{payment.reference}/pay/").status_code, 404)
        self.assertEqual(payment.attempts.count(), 0)

    def test_pushback_for_an_unknown_order_is_acknowledged(self):
        response = Client().post("/webhooks/payments/aba_payway/", data=_json_dumps({"tran_id": "EPC-000000-NOPE"}),
                                 content_type="application/json")
        self.assertEqual(response.status_code, 200)

    def test_unconfigured_aba_rejects_callbacks(self):
        with self.settings(ABA_PAYWAY_MERCHANT_ID="", ABA_PAYWAY_API_KEY=""):
            response = Client().post("/webhooks/payments/aba_payway/", data=_json_dumps({"tran_id": "X"}),
                                     content_type="application/json")
        self.assertEqual(response.status_code, 400)


# ---------------------------------------------------------------------------
# ACLEDA XPAY
# ---------------------------------------------------------------------------
XPAY = dict(
    ACLEDA_XPAY_BASE_URL=f"{EMU}/xpay", ACLEDA_XPAY_MERCHANT_ID="xp-merchant=",
    ACLEDA_XPAY_LOGIN_ID="XPTEST", ACLEDA_XPAY_PASSWORD="XPTEST", ACLEDA_XPAY_SECRET="xp-secret",
    ACLEDA_XPAY_MERCHANT_NAME="XPTEST", ACLEDA_XPAY_CURRENCY="USD",
    PAYMENT_EMULATOR_CREDENTIALS={
        **GATEWAYS["PAYMENT_EMULATOR_CREDENTIALS"],
        "ACLEDA_XPAY_MERCHANT_ID": "xp-merchant=", "ACLEDA_XPAY_LOGIN_ID": "XPTEST",
        "ACLEDA_XPAY_PASSWORD": "XPTEST", "ACLEDA_XPAY_SECRET": "xp-secret",
    },
)
PHONE = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) Mobile/15E148"


@override_settings(**XPAY)
class ACLEDAXPayFlowTests(GatewayTestCase):
    def press_pay(self, payment: Payment, qr: bool = False, **headers):
        """Pay goes to ACLEDA's own page; ``qr`` asks for its KHQR on ours instead."""
        return self.client.post(f"/payments/{payment.reference}/pay/", {"show": "qr"} if qr else {}, **headers)

    def show_qr(self, payment: Payment) -> dict:
        """The server opens a session, reads ACLEDA's KHQR and shows it on our page."""
        response = self.press_pay(payment, qr=True)
        self.assertRedirects(response, f"/payments/{payment.reference}/", fetch_redirect_response=False)
        session = self.detail(payment).context["session"]
        self.assertEqual(session["kind"], "qr")
        return session

    def token(self, payment: Payment) -> str:
        return payment.attempts.order_by("-created_at", "-id").first().token

    def open_hosted(self, payment: Payment):
        """ACLEDA's own page: a fresh session, posted by the browser as a plain form."""
        response = self.press_pay(payment)
        self.assertEqual(response.status_code, 200)
        handoff = response.context["handoff"]
        page = self.client.post(urlsplit(handoff.action).path, urlencode(handoff.fields),
                                content_type="application/x-www-form-urlencoded")
        return handoff, page

    def pay(self, token: str):
        return self.client.post(f"/dev/payment-emulator/xpay/act/{token}/", {"action": "pay"})

    def test_acleda_is_one_choice_at_checkout(self):
        keys = [option["key"] for option in self.client.get(f"/checkout/{self.plan.slug}/").context["options"]]
        self.assertIn("acleda_xpay", keys)
        self.assertNotIn("acleda_khqr", keys)                  # one bank, one entry

    def test_pay_takes_the_student_to_acledas_own_page_like_aba(self):
        payment = self.checkout("acleda_xpay")
        page = self.detail(payment)
        self.assertContains(page, "with ACLEDA Bank")                       # the main button
        self.assertContains(page, "Or show ACLEDA's KHQR on this page")      # the alternative
        response = self.press_pay(payment)
        self.assertEqual(urlsplit(response.context["handoff"].action).path, "/dev/payment-emulator/xpay/paymentPage.jsp")
        self.assertNotRegex(response.content.decode(), r'id="handoffForm"[^>]*data-manual')   # submits itself
        self.assertNotIn("/dev/payment-emulator/xpay/paymentPage.jsp", self.network.calls)  # the browser posts it

    def test_the_khqr_is_acledas_own_and_shown_on_our_page(self):
        payment = self.checkout("acleda_xpay")
        self.assertEqual(payment.charge_currency, "USD")
        self.assertEqual(self.detail(payment).context["session"]["kind"], "handoff")
        session = self.show_qr(payment)
        khqr = session["qr_payload"]
        self.assertTrue(xpay_module.khqr_is_intact(khqr))
        qr = emulator.read_khqr(khqr)
        self.assertEqual((qr["amount"], qr["currency"]), ("10.00", "USD"))
        self.assertIn("/dev/payment-emulator/xpay/paymentPage.jsp", self.network.calls)   # read server-side
        page = self.detail(payment)
        self.assertContains(page, "<svg")                       # the code is drawn
        self.assertContains(page, "Use ACLEDA Bank")            # and the bank's page is one tap away
        self.assertNothingCredited(payment)                     # an open session is not a payment

    def test_scanning_the_code_activates_the_plan(self):
        payment = self.checkout("acleda_xpay")
        self.show_qr(payment)
        self.assertFalse(self.status(payment)["paid"])         # -23 "Awaiting" with a core ref is not paid
        emulator.xpay_settle(self.token(payment))               # paid in any bank app
        self.assertTrue(self.status(payment)["paid"])
        self.assertPaid(payment)
        payment.refresh_from_db()
        self.assertTrue(payment.provider_txn_id.startswith("FT"))

    def test_acledas_own_page_gets_a_plain_form_and_a_fresh_session(self):
        payment = self.checkout("acleda_xpay")
        self.show_qr(payment)
        first = self.token(payment)
        handoff, page = self.open_hosted(payment)
        self.assertEqual(handoff.enctype, "application/x-www-form-urlencoded")
        self.assertNotEqual(handoff.fields["paymenttokenid"], first)
        self.assertEqual(handoff.fields["successUrlToReturn"], f"http://testserver/payments/{payment.reference}/return/")
        self.assertLessEqual(len(handoff.fields["transactionID"]), 16)
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Scan the KHQR and pay")

        response = self.pay(handoff.fields["paymenttokenid"])
        self.assertIn(f"/payments/{payment.reference}/return/?_paymenttokenid=", response["Location"])
        self.assertIn("/webhooks/payments/acleda_xpay/", self.network.calls)
        self.assertPaid(payment)
        back = self.client.get(response["Location"])
        self.assertRedirects(back, f"/payments/{payment.reference}/receipt/", fetch_redirect_response=False)

    def test_the_hand_off_page_posts_a_plain_form(self):
        payment = self.checkout("acleda_xpay")
        response = self.press_pay(payment)
        self.assertRegex(response.content.decode(),
                         r'id="handoffForm" enctype="application/x-www-form-urlencoded"')
        self.assertEqual(response["Cache-Control"].split(",")[0].strip(), "max-age=0")

    def test_a_multipart_post_gets_acledas_updating_page(self):
        """What the live page answered when the site first posted multipart."""
        payment = self.checkout("acleda_xpay")
        handoff = self.press_pay(payment).context["handoff"]
        page = self.client.post(urlsplit(handoff.action).path, handoff.fields)   # test client: multipart
        self.assertContains(page, "The system has been updating")

    def test_an_older_code_paid_after_a_newer_one_still_counts(self):
        payment = self.checkout("acleda_xpay")
        self.show_qr(payment)
        first = self.token(payment)
        self.open_hosted(payment)                               # a second session, never paid
        emulator.xpay_settle(first)                             # the student scanned the first code
        self.assertTrue(self.status(payment)["paid"])
        self.assertPaid(payment)

    def test_when_the_code_cannot_be_read_the_student_goes_to_acledas_page(self):
        payment = self.checkout("acleda_xpay")
        with patch.object(xpay_module.ACLEDAXPayProvider, "fetch_khqr", return_value=""):
            response = self.press_pay(payment, qr=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(urlsplit(response.context["handoff"].action).path, "/dev/payment-emulator/xpay/paymentPage.jsp")
        self.assertEqual(payment.attempts.count(), 2)          # the fetch spent one; the page gets a fresh one

    def test_a_session_posted_twice_is_cancelled_but_the_order_lives(self):
        payment = self.checkout("acleda_xpay")
        handoff, _ = self.open_hosted(payment)
        again = self.client.post(urlsplit(handoff.action).path, urlencode(handoff.fields),
                                 content_type="application/x-www-form-urlencoded")
        self.assertContains(again, "-85")                       # what the real gateway does
        self.assertFalse(self.status(payment)["paid"])
        payment.refresh_from_db()
        self.assertEqual(payment.status, PaymentStatus.PENDING)   # a dead session is not a dead order
        self.show_qr(payment)
        emulator.xpay_settle(self.token(payment))
        self.assertTrue(self.status(payment)["paid"])

    def test_no_one_tap_app_button_is_offered(self):
        payment = self.checkout("acleda_xpay")
        for agent in (PHONE, "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"):
            self.press_pay(payment, HTTP_USER_AGENT=agent)
            page = self.detail(payment)
            self.assertNotContains(page, "Open ACLEDA mobile")
            self.assertNotContains(page, "acledabank://")

    def test_a_forged_callback_changes_nothing(self):
        payment = self.checkout("acleda_xpay")
        self.show_qr(payment)
        response = Client().post("/webhooks/payments/acleda_xpay/", data=_json_dumps({
            "_paymenttokenid": self.token(payment), "_resultCode": 0, "_paymentresult": "SUCCESS",
        }), content_type="application/json")
        self.assertEqual(response.status_code, 200)
        self.assertNothingCredited(payment)                     # ACLEDA itself says: awaiting payment

    def test_a_callback_for_an_unknown_token_is_acknowledged(self):
        response = Client().post("/webhooks/payments/acleda_xpay/", data=_json_dumps({"_paymenttokenid": "NOPE"}),
                                 content_type="application/json")
        self.assertEqual(response.status_code, 200)

    def test_an_amount_mismatch_is_held_for_review(self):
        payment = self.checkout("acleda_xpay")
        self.show_qr(payment)
        session = emulator._get("xpay", self.token(payment))
        session["amount"] = "1.00"
        emulator._put("xpay", session["token"], session)
        emulator.xpay_settle(session["token"])
        self.status(payment)
        self.assertNothingCredited(payment)
        self.assertTrue(PaymentEvent.objects.filter(payment=payment, event_type="amount_mismatch").exists())

    def test_the_background_sweep_credits_a_student_who_closed_the_page(self):
        payment = self.checkout("acleda_xpay")
        self.show_qr(payment)
        emulator.xpay_settle(self.token(payment))               # paid; no callback, no return, page closed
        self.assertEqual(payment_service.sweep_pending(), 1)
        self.assertPaid(payment)

    def test_signatures_are_uppercase_hex_hmac_sha512(self):
        digest = xpay_module.sign("MIDLOGINPASSTXID", "secret")
        self.assertEqual(len(digest), 128)
        self.assertEqual(digest, digest.upper())

    def test_a_truncated_or_corrupt_khqr_is_never_shown(self):
        good = build_khqr(bakong_id="x@aclb", merchant_name="M", city="PP", amount=Decimal("1.00"),
                          currency="USD", bill_number="B1")
        self.assertTrue(xpay_module.khqr_is_intact(good))
        self.assertFalse(xpay_module.khqr_is_intact(good[:-6]))
        self.assertFalse(xpay_module.khqr_is_intact(good[:-1] + ("0" if good[-1] != "0" else "1")))

    def test_txids_fit_and_never_collide(self):
        payment = self.checkout("acleda_xpay")
        ids = {xpay_module.txid_for(payment, n) for n in range(1, 21)}
        self.assertEqual(len(ids), 20)
        self.assertTrue(all(len(txid) <= 16 for txid in ids))

    def test_health_check_tells_good_credentials_from_bad(self):
        ok, message = get_provider("acleda_xpay").health_check()
        self.assertTrue(ok, message)
        with self.settings(ACLEDA_XPAY_SECRET="wrong"):
            ok, message = get_provider("acleda_xpay").health_check()
        self.assertFalse(ok)
        self.assertIn("rejected", message)

    def test_without_credentials_acleda_falls_back_to_the_khqr(self):
        with self.settings(ACLEDA_XPAY_SECRET=""):
            keys = [o["key"] for o in self.client.get(f"/checkout/{self.plan.slug}/").context["options"]]
        self.assertNotIn("acleda_xpay", keys)
        self.assertIn("acleda_khqr", keys)


class LiveCredentialIsolationTests(TestCase):
    def test_the_test_runner_never_sees_live_payment_keys(self):
        from django.conf import settings

        for name in ("ABA_PAYWAY_MERCHANT_ID", "ABA_PAYWAY_API_KEY", "ACLEDA_XPAY_SECRET", "ACLEDA_XPAY_PASSWORD",
                     "ACLEDA_XPAY_MERCHANT_ID", "BAKONG_API_TOKEN", "BINANCE_PAY_API_SECRET"):
            self.assertEqual(getattr(settings, name), "", name)
        self.assertIn("sandbox", settings.ABA_PAYWAY_BASE_URL)


# ---------------------------------------------------------------------------
# Binance Pay
# ---------------------------------------------------------------------------
class BinancePayFlowTests(GatewayTestCase):
    def open_order(self) -> tuple[Payment, dict]:
        payment = self.checkout("binance")
        session = self.detail(payment).context["session"]
        self.assertEqual(session["kind"], "qr")
        payment.refresh_from_db()
        self.assertTrue(payment.provider_order_id)           # the prepayId
        return payment, emulator._get("binance", payment.reference.replace("-", ""))

    def signed_webhook(self, order: dict, biz_status: str = "PAY_SUCCESS", **overrides) -> tuple[str, dict]:
        data = _json_dumps({"merchantTradeNo": order["merchantTradeNo"], "totalFee": order["orderAmount"],
                            "currency": order["currency"], "transactionId": "M1", **overrides})
        body = _json_dumps({"bizType": "PAY", "bizIdStr": order["prepayId"], "bizStatus": biz_status, "data": data})
        return body, emulator.binance_sign(body)

    def post_webhook(self, body: str, headers: dict):
        extra = {f"HTTP_{k.upper().replace('-', '_')}": v for k, v in headers.items() if k != "Content-Type"}
        return Client().post("/webhooks/payments/binance/", data=body, content_type="application/json", **extra)

    def test_full_flow_scan_pay_signed_webhook_and_activate(self):
        payment, order = self.open_order()
        self.assertEqual(order["currency"], "USDT")
        self.assertEqual(order["orderAmount"], "10.00")
        self.assertEqual(order["webhookUrl"], "http://testserver/webhooks/payments/binance/")
        self.assertNothingCredited(payment)

        response = self.client.post(f"/dev/payment-emulator/binance/checkout/{order['prepayId']}/", {"action": "pay"})
        self.assertEqual(response.status_code, 302)
        self.assertIn("/dev/payment-emulator/binance/binancepay/openapi/certificates", self.network.calls)
        self.assertPaid(payment)

    def test_webhook_ack_is_exactly_what_binance_expects(self):
        payment, order = self.open_order()
        body, headers = self.signed_webhook(order)
        response = self.post_webhook(body, headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b'{"returnCode":"SUCCESS","returnMessage":null}')
        self.assertPaid(payment)

    def test_a_tampered_webhook_is_rejected(self):
        payment, order = self.open_order()
        body, headers = self.signed_webhook(order)
        response = self.post_webhook(body.replace("10.00", "0.01"), headers)
        self.assertEqual(response.status_code, 400)
        self.assertNothingCredited(payment)

    def test_a_webhook_signed_with_another_key_is_rejected(self):
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding, rsa

        payment, order = self.open_order()
        body, headers = self.signed_webhook(order)
        forger = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        payload = f"{headers['BinancePay-Timestamp']}\n{headers['BinancePay-Nonce']}\n{body}\n".encode()
        headers["BinancePay-Signature"] = base64.b64encode(
            forger.sign(payload, padding.PKCS1v15(), hashes.SHA256())).decode()
        self.assertEqual(self.post_webhook(body, headers).status_code, 400)
        self.assertNothingCredited(payment)

    def test_an_unsigned_webhook_is_rejected(self):
        payment, order = self.open_order()
        body, _ = self.signed_webhook(order)
        self.assertEqual(self.post_webhook(body, {}).status_code, 400)
        self.assertNothingCredited(payment)

    def test_a_closed_order_expires(self):
        payment, order = self.open_order()
        self.client.post(f"/dev/payment-emulator/binance/checkout/{order['prepayId']}/", {"action": "close"})
        self.assertNothingCredited(payment, status=PaymentStatus.EXPIRED)
        self.assertEqual(self.detail(payment).context["outcome"][0], "This order expired")

    def test_replayed_webhooks_credit_once(self):
        payment, order = self.open_order()
        body, headers = self.signed_webhook(order)
        for _ in range(3):
            self.post_webhook(body, headers)
        self.assertPaid(payment)

    def test_the_status_query_confirms_without_a_webhook(self):
        payment, order = self.open_order()
        order.update(status="PAID", transactionId="M42")
        emulator._put("binance", order["merchantTradeNo"], order)
        self.assertTrue(self.status(payment)["paid"])
        self.assertPaid(payment)

    def test_a_wrong_api_secret_is_refused_by_binance(self):
        payment = payment_service.create_payment(self.user, self.plan, "binance")
        with self.settings(BINANCE_PAY_API_SECRET="not-the-secret"):
            with self.assertRaises(PaymentError):
                get_provider("binance").create_checkout(payment)

    def test_plan_names_with_dashes_and_emoji_reach_binance_as_plain_text(self):
        """Binance prohibits special characters in goodsName; the real plans use "—"."""
        Plan.objects.filter(pk=self.plan.pk).update(name="GPT-5 Mini — Standard \U0001F680")
        payment, order = self.open_order()                    # the emulator refuses non-ASCII names
        self.assertTrue(order["prepayId"])
        self.assertEqual(payment.checkout_payload["kind"], "qr")

    def test_no_expiry_timestamp_is_sent(self):
        """v3 reads orderExpireTime as a duration; Binance's one-hour default is used instead."""
        _, order = self.open_order()
        self.assertIsNone(order["expireTime"])

    def test_the_site_closes_the_binance_order_when_its_window_ends(self):
        payment, order = self.open_order()
        Payment.objects.filter(pk=payment.pk).update(expires_at=timezone.now() - timedelta(minutes=10))
        with self.captureOnCommitCallbacks(execute=True):     # the close runs once the expiry commits
            payment_service.expire_stale()
        self.assertNothingCredited(payment, status=PaymentStatus.EXPIRED)
        self.assertEqual(emulator._get("binance", order["merchantTradeNo"])["status"], "CANCELED")

    def test_both_spellings_of_the_closed_notification_expire_the_order(self):
        for spelling in ("PAY_CLOSED", "PAY_CLOSE"):
            with self.subTest(spelling=spelling):
                payment, order = self.open_order()
                order["status"] = "EXPIRED"                   # what the query then confirms
                emulator._put("binance", order["merchantTradeNo"], order)
                body, headers = self.signed_webhook(order, biz_status=spelling)
                self.assertEqual(self.post_webhook(body, headers).status_code, 200)
                self.assertNothingCredited(payment, status=PaymentStatus.EXPIRED)

    def test_the_background_sweep_credits_a_payment_whose_webhook_was_lost(self):
        payment, order = self.open_order()
        order.update(status="PAID", transactionId="M77")      # paid, but no webhook ever arrived
        emulator._put("binance", order["merchantTradeNo"], order)
        self.assertEqual(payment_service.sweep_pending(), 1)
        self.assertPaid(payment)


# ---------------------------------------------------------------------------
# ACLEDA KHQR on Bakong
# ---------------------------------------------------------------------------
class KHQRPayloadTests(TestCase):
    def test_crc_is_ccitt_false(self):
        self.assertEqual(crc16("123456789"), "29B1")

    def test_payload_carries_the_order(self):
        khqr = build_khqr(bakong_id="epc_test@aclb", merchant_name="EPC AI ASSISTANT", city="Phnom Penh",
                          amount=Decimal("41000"), currency="KHR", bill_number="EPC-260927-AB12CD34",
                          created_ms=1_700_000_000_000, expires_ms=1_700_000_900_000)
        tags = emulator.parse_tlv(khqr)
        self.assertEqual(tags["00"], "01")
        self.assertEqual(tags["01"], "12")                   # dynamic: carries an amount
        self.assertEqual(emulator.parse_tlv(tags["29"])["00"], "epc_test@aclb")
        self.assertEqual(tags["53"], "116")                  # KHR
        self.assertEqual(tags["54"], "41000")
        self.assertEqual(tags["58"], "KH")
        self.assertEqual(emulator.parse_tlv(tags["62"])["01"], "EPC-260927-AB12CD34")
        self.assertEqual(emulator.parse_tlv(tags["99"])["01"], "1700000900000")
        self.assertEqual(khqr[-4:], crc16(khqr[:-4]))

    def test_merchant_accounts_use_tag_30(self):
        khqr = build_khqr(bakong_id="shop@aclb", merchant_name="Shop", city="Phnom Penh", amount=Decimal("5.00"),
                          currency="USD", bill_number="R1", merchant_id="123456")
        tags = emulator.parse_tlv(khqr)
        self.assertNotIn("29", tags)
        self.assertEqual(emulator.parse_tlv(tags["30"])["01"], "123456")
        self.assertEqual(tags["53"], "840")
        self.assertEqual(tags["54"], "5.00")


class ACLEDAKHQRFlowTests(GatewayTestCase):
    def open_qr(self) -> tuple[Payment, str]:
        payment = self.checkout("acleda_khqr")
        response = self.detail(payment)
        session = response.context["session"]
        self.assertEqual(session["kind"], "qr")
        self.assertTrue(response.context["qr_svg"])
        payment.refresh_from_db()
        self.assertEqual(payment.provider_order_id, khqr_md5(session["qr_payload"]))
        return payment, session["qr_payload"]

    def test_full_flow_scan_poll_and_activate(self):
        payment, khqr = self.open_qr()
        self.assertFalse(self.status(payment)["paid"])       # not paid yet: Bakong has no record
        self.assertNothingCredited(payment)

        self.client.post(f"/dev/payment-emulator/acleda_khqr/{payment.reference}/", {"action": "pay"})
        data = self.status(payment)
        self.assertTrue(data["paid"])
        self.assertTrue(data["final"])
        self.assertPaid(payment)

    def test_money_sent_to_another_account_does_not_count(self):
        payment, _ = self.open_qr()
        self.client.post(f"/dev/payment-emulator/acleda_khqr/{payment.reference}/", {"action": "wrong"})
        self.assertFalse(self.status(payment)["paid"])
        self.assertNothingCredited(payment)

    def test_a_failed_transfer_fails_the_order(self):
        payment, _ = self.open_qr()
        self.client.post(f"/dev/payment-emulator/acleda_khqr/{payment.reference}/", {"action": "fail"})
        self.assertTrue(self.status(payment)["final"])
        self.assertNothingCredited(payment, status=PaymentStatus.FAILED)

    def test_a_rejected_bakong_token_never_settles(self):
        payment, khqr = self.open_qr()
        emulator.bakong_pay(khqr)
        with self.settings(BAKONG_API_TOKEN="expired-token"):
            with self.assertRaises(PaymentError) as caught:
                get_provider("acleda_khqr").verify(payment)
        self.assertEqual(caught.exception.code, "bakong_token")
        self.assertNothingCredited(payment)

    def test_background_sweep_credits_a_student_who_closed_the_page(self):
        payment, khqr = self.open_qr()
        emulator.bakong_pay(khqr)
        self.assertEqual(payment_service.sweep_pending(), 1)
        self.assertPaid(payment)

    def test_without_a_token_the_qr_still_shows_for_manual_confirmation(self):
        with self.settings(BAKONG_API_TOKEN=""):
            payment = self.checkout("acleda_khqr")
            response = self.detail(payment)
            self.assertEqual(response.context["session"]["kind"], "qr")
            self.assertTrue(response.context["manual_review"])
            self.assertFalse(response.context["automatic"])


# ---------------------------------------------------------------------------
# Order lifecycle shared by every method
# ---------------------------------------------------------------------------
class OrderLifecycleTests(GatewayTestCase):
    def test_an_unpaid_order_expires_after_its_window(self):
        payment = self.checkout("acleda_khqr")
        Payment.objects.filter(pk=payment.pk).update(expires_at=timezone.now() - timedelta(minutes=10))
        data = self.status(payment)
        self.assertEqual(data["status"], "expired")
        self.assertTrue(data["final"])
        self.assertNothingCredited(payment, status=PaymentStatus.EXPIRED)

    def test_a_payment_in_the_last_moment_is_credited_not_expired(self):
        payment = self.checkout("acleda_khqr")
        emulator.bakong_pay(payment.checkout_payload["qr_payload"])
        Payment.objects.filter(pk=payment.pk).update(expires_at=timezone.now() - timedelta(minutes=10))
        self.assertEqual(payment_service.expire_stale(), 0)
        self.assertPaid(payment)

    def test_expire_stale_closes_forgotten_orders(self):
        payment = self.checkout("binance")
        Payment.objects.filter(pk=payment.pk).update(expires_at=timezone.now() - timedelta(minutes=10))
        self.assertEqual(payment_service.expire_stale(), 1)
        self.assertNothingCredited(payment, status=PaymentStatus.EXPIRED)

    def test_one_gateway_transaction_cannot_pay_two_orders(self):
        first = payment_service.create_payment(self.user, self.plan, "binance")
        second = payment_service.create_payment(self.user, self.plan, "binance")
        paid = VerificationResult(status="paid", provider_txn_id="M-SAME", amount="10.00", currency="USDT")
        payment_service.apply_result(first, paid, source="test")
        payment_service.apply_result(second, paid, source="test")
        second.refresh_from_db()
        self.assertEqual(second.status, PaymentStatus.PENDING)
        self.assertTrue(PaymentEvent.objects.filter(payment=second, event_type="duplicate_transaction").exists())
        self.assertEqual(token_service.balance_of(self.user), self.plan.tokens)

    def test_wrong_currency_is_a_mismatch(self):
        payment = payment_service.create_payment(self.user, self.plan, "binance")
        payment_service.apply_result(
            payment, VerificationResult(status="paid", amount="10.00", currency="BUSD"), source="test")
        self.assertNothingCredited(payment)

    def test_the_payment_page_shows_the_countdown_and_polls(self):
        payment = self.checkout("acleda_khqr")
        response = self.detail(payment)
        self.assertGreater(response.context["seconds_left"], 0)
        self.assertContains(response, 'id="timer"')
        self.assertContains(response, f"/payments/{payment.reference}/status/")

    def test_expired_order_can_be_retried_with_a_new_one(self):
        payment = self.checkout("binance")
        Payment.objects.filter(pk=payment.pk).update(expires_at=timezone.now() - timedelta(minutes=10))
        payment_service.expire_stale()
        again = self.checkout("binance")
        self.assertNotEqual(again.pk, payment.pk)


# ---------------------------------------------------------------------------
# Admin: gateway page and connection tests
# ---------------------------------------------------------------------------
class GatewayConsoleTests(GatewayTestCase):
    def setUp(self):
        super().setUp()
        self.admin = User.objects.create_superuser(username="root", email="root@test.local", password=PASSWORD)
        self.client.force_login(self.admin)

    def test_health_checks_pass_with_good_credentials(self):
        for key in ("aba_payway", "acleda_khqr", "binance"):
            ok, message = get_provider(key).health_check()
            self.assertTrue(ok, f"{key}: {message}")

    def test_health_checks_catch_bad_credentials(self):
        with self.settings(BAKONG_API_TOKEN="expired"):
            ok, message = get_provider("acleda_khqr").health_check()
        self.assertFalse(ok)
        self.assertIn("token", message)
        with self.settings(BINANCE_PAY_API_SECRET="wrong"):
            ok, message = get_provider("binance").health_check()
        self.assertFalse(ok)
        with self.settings(ABA_PAYWAY_API_KEY="wrong"):
            ok, message = get_provider("aba_payway").health_check()
        self.assertFalse(ok)
        self.assertIn("rejected", message)

    def test_gateways_page_lists_callback_urls_and_never_secrets(self):
        response = self.client.get("/admin/payment-gateways/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "http://testserver/webhooks/payments/aba_payway/")
        self.assertContains(response, "http://testserver/webhooks/payments/binance/")
        self.assertContains(response, "BAKONG_API_TOKEN")
        for secret in ("aba-test-key", "bn-test-secret", "bakong-test-token"):
            self.assertNotContains(response, secret)

    def test_connection_test_button_records_the_result(self):
        response = self.client.post("/admin/payment-gateways/binance/test/")
        self.assertEqual(response.status_code, 302)
        page = self.client.get("/admin/payment-gateways/")
        self.assertContains(page, "Binance accepted the API key")

    def test_admin_payment_detail_shows_the_charge_and_flags(self):
        payment = payment_service.create_payment(self.user, self.plan, "aba_payway")
        payment_service.apply_result(payment, VerificationResult(status="paid", amount="1", currency="KHR"), source="t")
        response = self.client.get(f"/admin/payments/{payment.reference}/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, payment.pay_display)
        self.assertContains(response, "Amount mismatch")

    def test_students_cannot_reach_the_gateway_page(self):
        self.client.force_login(self.user)
        response = self.client.get("/admin/payment-gateways/")
        self.assertNotEqual(response.status_code, 200)
