"""A local stand-in for ABA PayWay, ACLEDA XPAY, Binance Pay and the Bakong Open API.

Mounted at ``/dev/payment-emulator/`` only when ``PAYMENT_EMULATOR`` is on
(which itself requires DEBUG). With it on, ``config/settings.py`` points all
three providers here, so every flow runs through the real provider code:

* **ABA PayWay** — the purchase endpoint checks the form's HMAC hash, shows a
  hosted "pay" page, sends the pushback to our webhook and answers
  ``check-transaction-2``.
* **Binance Pay** — ``v3/order``, ``v2/order/query``, ``order/close`` and
  ``certificates``, each checking our HMAC-SHA512 request signature; paying
  sends an RSA-signed webhook, exactly as Binance does, with a key the
  emulator makes for itself.
* **Bakong** — ``check_transaction_by_md5`` behind a Bearer token, and a
  "banking app" page that reads the KHQR the way a real app would.
* **ACLEDA XPAY** — ``openSessionV2`` and ``getTxnStatus`` checking the
  uppercase-hex HMAC-SHA512 over login, password and txid/token, the hosted
  ``paymentPage.jsp`` (single-use, like the real one) and the merchant
  callback. Codes are the ones the production gateway returns.

State lives in the cache, so it lasts as long as the dev server does.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from decimal import Decimal

import requests
from django.conf import settings
from django.core.cache import cache
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.urls import path, reverse
from django.views.decorators.csrf import csrf_exempt

from AI.models import Payment
from AI.services.payments.aba_payway import PURCHASE_HASH_FIELDS
from AI.services.payments.acleda_khqr import build_khqr, khqr_md5
from AI.services.payments.binance import trade_no

STATE_SECONDS = 24 * 3600
CERT_SERIAL = "emulator-cert-1"


# -- shared state ---------------------------------------------------------------------------
def _get(kind: str, key: str) -> dict | None:
    return cache.get(f"payemu:{kind}:{key}")


def _put(kind: str, key: str, value: dict) -> dict:
    cache.set(f"payemu:{kind}:{key}", value, STATE_SECONDS)
    return value


def _held(name: str) -> str:
    """The credential this emulated gateway holds for the merchant.

    It matches our own setting, as a real gateway's does when the merchant
    configured it correctly. ``PAYMENT_EMULATOR_CREDENTIALS`` pins it apart,
    so a test can put a wrong key on our side and watch the gateway refuse it.
    """
    pinned = getattr(settings, "PAYMENT_EMULATOR_CREDENTIALS", None) or {}
    return pinned.get(name) or getattr(settings, name)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _page(request, title: str, **context):
    return render(request, "dev/payment_emulator.html", {"title": title, **context})


def _json_body(request) -> dict:
    try:
        body = json.loads(request.body.decode() or "{}")
    except (ValueError, UnicodeDecodeError):
        return {}
    return body if isinstance(body, dict) else {}


# -- KHQR, read the way a banking app reads it -------------------------------------------------
def parse_tlv(payload: str) -> dict[str, str]:
    """Top-level EMVCo tags of a QR payload: ``{"54": "102500", ...}``."""
    tags, i = {}, 0
    while i + 4 <= len(payload):
        tag, length = payload[i:i + 2], int(payload[i + 2:i + 4])
        tags[tag] = payload[i + 4:i + 4 + length]
        i += 4 + length
    return tags


def read_khqr(khqr: str) -> dict:
    tags = parse_tlv(khqr)
    account = parse_tlv(tags.get("29") or tags.get("30") or "")
    extra = parse_tlv(tags.get("62", ""))
    return {
        "account": account.get("00", ""),
        "currency": {"116": "KHR", "840": "USD"}.get(tags.get("53", ""), tags.get("53", "")),
        "amount": tags.get("54", ""),
        "merchant": tags.get("59", ""),
        "bill": extra.get("01", ""),
        "md5": khqr_md5(khqr),
    }


# -- ABA PayWay --------------------------------------------------------------------------------
def _aba_hash(fields: dict, order) -> str:
    payload = "".join(str(fields.get(name) or "") for name in order).encode()
    return base64.b64encode(
        hmac.new(_held("ABA_PAYWAY_API_KEY").encode(), payload, hashlib.sha512).digest()
    ).decode()


@csrf_exempt
def aba_purchase(request):
    """The hosted checkout: the student's browser posts the signed form here."""
    if request.method != "POST":
        return HttpResponse("PayWay purchase expects a POST.", status=405)
    fields = request.POST.dict()
    if fields.get("merchant_id") != _held("ABA_PAYWAY_MERCHANT_ID"):
        return JsonResponse({"status": {"code": "03", "message": "Invalid merchant profile."}}, status=400)
    if not hmac.compare_digest(fields.get("hash", ""), _aba_hash(fields, PURCHASE_HASH_FIELDS)):
        return JsonResponse({"status": {"code": "01", "message": "Wrong hash."}}, status=400)

    tran_id = fields.get("tran_id", "")
    if _get("aba", tran_id) and _get("aba", tran_id).get("posted"):
        # PayWay never takes the same tran_id twice.
        return JsonResponse({"status": {"code": "4", "message": "Duplicated transaction ID."}}, status=400)
    order = _get("aba", tran_id) or {
        "tran_id": tran_id, "status": "PENDING", "apv": "",
        "amount": fields.get("amount", ""), "currency": fields.get("currency", "USD"),
        "pushback": base64.b64decode(fields.get("return_url", "") or b"").decode(),
        "continue": fields.get("continue_success_url", ""), "cancel": fields.get("cancel_url", ""),
        "created": _now_ms(), "posted": True, "payment_gate": fields.get("payment_gate", ""),
    }
    _put("aba", tran_id, order)
    return _page(request, "ABA PayWay", gateway="aba", order=order,
                 action=reverse("payment_emulator:aba_act", args=[tran_id]))


@csrf_exempt
def aba_act(request, tran_id: str):
    order = _get("aba", tran_id)
    if order is None or request.method != "POST":
        raise Http404
    action = request.POST.get("action")
    if action == "cancel":
        return redirect(order["cancel"])
    if order["status"] == "PENDING":
        order["status"] = "APPROVED" if action == "pay" else "DECLINED"
        order["apv"] = f"{secrets.randbelow(10**6):06d}" if action == "pay" else ""
        _put("aba", tran_id, order)
    # PayWay's pushback: unsigned, which is why our side re-checks it.
    try:
        requests.post(order["pushback"], json={"tran_id": tran_id, "apv": order["apv"],
                                               "status": 0 if order["status"] == "APPROVED" else 3,
                                               "return_params": ""}, timeout=15)
    except requests.RequestException:
        pass
    return redirect(order["continue"] if order["status"] == "APPROVED" else order["cancel"])


@csrf_exempt
def aba_check(request):
    body = _json_body(request)
    if body.get("merchant_id") != _held("ABA_PAYWAY_MERCHANT_ID") or not hmac.compare_digest(
        str(body.get("hash", "")), _aba_hash(body, ("req_time", "merchant_id", "tran_id"))
    ):
        return JsonResponse({"status": {"code": 5, "message": "wrong hash", "tran_id": body.get("tran_id")}},
                            status=403)
    order = _get("aba", str(body.get("tran_id", "")))
    if order is None:
        return JsonResponse({"status": {"code": 6, "message": "tran_id not found", "tran_id": body.get("tran_id")}})
    return JsonResponse({
        "data": {
            "payment_status_code": {"APPROVED": 0, "PENDING": 2, "DECLINED": 3}.get(order["status"], 2),
            # A profile can settle in another currency: then the settled pair
            # differs and only original_amount echoes the purchase.
            "total_amount": order.get("settled_amount", order["amount"]), "original_amount": order["amount"],
            "payment_amount": order.get("settled_amount", order["amount"]),
            "payment_currency": order.get("settled_currency", order["currency"]),
            "original_currency": order["currency"],
            "apv": order["apv"], "payment_status": order["status"],
            "transaction_date": time.strftime("%Y-%m-%d %H:%M:%S"),
        },
        "status": {"code": "00", "message": "Success!", "tran_id": order["tran_id"]},
    })


# -- ACLEDA XPAY -------------------------------------------------------------------------------
def _xpay_sign(message: str) -> str:
    return hmac.new(_held("ACLEDA_XPAY_SECRET").encode(), message.encode(), hashlib.sha512).hexdigest().upper()


def _xpay_answer(code: int, details: str, **extra) -> JsonResponse:
    """XPAY wraps every answer in ``result``, as the real gateway does."""
    transaction = extra.pop("xTran", None) or {
        "purchaseAmount": 0.0, "purchaseDate": 0, "quantity": 0, "expiryTime": 1, "confirmDate": 0,
    }
    return JsonResponse({"result": {"code": code, "errorDetails": details, "xTran": transaction, **extra}})


def _xpay_login_ok(body: dict) -> bool:
    return (body.get("loginId") == _held("ACLEDA_XPAY_LOGIN_ID")
            and body.get("password") == _held("ACLEDA_XPAY_PASSWORD"))


@csrf_exempt
def xpay_open_session(request):
    body = _json_body(request)
    transaction = body.get("xpayTransaction") or {}
    txid = str(transaction.get("txid") or "")
    expected = _xpay_sign(f"{_held('ACLEDA_XPAY_MERCHANT_ID')}{_held('ACLEDA_XPAY_LOGIN_ID')}"
                          f"{_held('ACLEDA_XPAY_PASSWORD')}{txid}")
    if (body.get("merchantID") != _held("ACLEDA_XPAY_MERCHANT_ID") or not _xpay_login_ok(body)
            or not hmac.compare_digest(str(body.get("hash", "")), expected)):
        return _xpay_answer(-5547, "Transaction verification failed!")
    if len(txid) > 16 or _get("xpay-txid", txid):
        return _xpay_answer(-3, "Duplicate or invalid transaction id!")
    token = secrets.token_hex(16).upper()
    session = {
        "txid": txid, "token": token, "sessionid": secrets.token_hex(12),
        "amount": str(transaction.get("purchaseAmount") or ""),
        "currency": str(transaction.get("purchaseCurrency") or "USD").upper(),
        "invoice": str(transaction.get("invoiceid") or ""), "status": "AWAITING", "posted": False,
        "confirmDate": 0, "success": "", "error": "",
    }
    _put("xpay", token, session)
    _put("xpay-txid", txid, {"token": token})
    deeplink = f"acledabank://xpay/pay?token={token}" if transaction.get("oprDevice") else ""
    return _xpay_answer(0, "SUCCESS", sessionid=session["sessionid"], deeplinkUrl=deeplink,
                        xTran={"paymentTokenid": token, "txid": txid})


@csrf_exempt
def xpay_status(request):
    body = _json_body(request)
    token = str(body.get("paymentTokenid") or "")
    expected = _xpay_sign(f"{_held('ACLEDA_XPAY_MERCHANT_ID')}{_held('ACLEDA_XPAY_LOGIN_ID')}"
                          f"{_held('ACLEDA_XPAY_PASSWORD')}{token}")
    if not _xpay_login_ok(body) or not hmac.compare_digest(str(body.get("hash", "")), expected):
        return _xpay_answer(-5547, "Transaction verification failed!")
    session = _get("xpay", token)
    if session is None:
        return _xpay_answer(-1, "The transaction you are looking for does not exist!")
    transaction = {"txid": session["txid"], "invoiceid": session["invoice"], "purchaseCurrency": session["currency"],
                   "purchaseAmount": float(session["amount"] or 0), "confirmDate": session["confirmDate"]}
    if session["status"] == "PAID":
        return _xpay_answer(0, "SUCCESS", xTran=transaction, coreRefNum=session.get("core", ""),
                            paymentMethod="KHQR", customerName="TEST STUDENT")
    if session["status"] == "CANCELLED":
        return _xpay_answer(-85, "Transaction has been cancelled!", xTran=transaction)
    # An opened, unpaid session carries a core reference too — which is why
    # the site must never read one as money received.
    return _xpay_answer(-23, "Transaction Awaiting For Payment!", xTran=transaction, coreRefNum="FT-AWAITING")


@csrf_exempt
def xpay_payment_page(request):
    """ACLEDA's hosted page: the browser posts the documented form here."""
    if request.method != "POST":
        return HttpResponse("paymentPage.jsp expects the merchant's POSTed form.", status=405)
    if request.content_type == "multipart/form-data":
        # What the live page answered a multipart post with (29 Sep 2026): it
        # only reads a plain urlencoded form.
        return HttpResponse("<p>The system has been updating, please try again later.</p>")
    fields = request.POST.dict()
    session = _get("xpay", fields.get("paymenttokenid", ""))
    if session is None or fields.get("sessionid") != session["sessionid"] \
            or fields.get("merchantID") != _held("ACLEDA_XPAY_MERCHANT_ID"):
        return HttpResponse("Invalid payment session.", status=400)
    refreshed = False
    if session["posted"] and session["status"] == "AWAITING":
        # Single-use: the real gateway cancels a session posted twice.
        session["status"], refreshed = "CANCELLED", True
    session.update(posted=True, success=fields.get("successUrlToReturn", ""), error=fields.get("errorUrl", ""))
    _put("xpay", session["token"], session)
    # The session's KHQR, in the hidden input ACLEDA's page carries it in.
    khqr = build_khqr(
        bakong_id="epc_xpay@aclb", merchant_name="EPC DEMO MERCHANT", city="Phnom Penh",
        amount=Decimal(session["amount"] or "0"), currency=session["currency"], bill_number=session["txid"],
    )
    return _page(request, "ACLEDA Bank", gateway="xpay", session=session, refreshed=refreshed, khqr=khqr,
                 action=reverse("payment_emulator:xpay_act", args=[session["token"]]), back=session["error"])


def xpay_settle(token: str) -> dict | None:
    """What a payer finishing in their bank app does to the session."""
    session = _get("xpay", token)
    if session is None or session["status"] != "AWAITING":
        return session
    session.update(status="PAID", confirmDate=_now_ms(), core=f"FT{secrets.randbelow(10**10):010d}")
    return _put("xpay", token, session)


@csrf_exempt
def xpay_act(request, token: str):
    session = _get("xpay", token)
    if session is None or request.method != "POST":
        raise Http404
    if request.POST.get("action") == "cancel":
        session["status"] = "CANCELLED"
        _put("xpay", token, session)
        return redirect(session["error"] or "/")
    session = xpay_settle(token)
    # The merchant callback: carries claims the site must not trust — it
    # re-asks getTxnStatus with its own credentials.
    try:
        requests.post(f"{settings.SITE_URL.rstrip('/')}/webhooks/payments/acleda_xpay/",
                      json={"_paymenttokenid": token, "_resultCode": 0, "_paymentresult": "SUCCESS",
                            "_paymenttype": "QR", "_amount": float(session["amount"] or 0),
                            "_transactionid": session["txid"]}, timeout=15)
    except requests.RequestException:
        pass
    return redirect(f"{session['success']}?_paymenttokenid={token}&_resultCode=0&_paymentresult=SUCCESS")


# -- Binance Pay -------------------------------------------------------------------------------
def _binance_key():
    """The emulator's own RSA key, standing in for Binance's webhook key."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    pem = cache.get("payemu:binance:key")
    if pem is None:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode()
        cache.set("payemu:binance:key", pem, None)
    return serialization.load_pem_private_key(pem.encode(), password=None)


def binance_public_pem() -> str:
    from cryptography.hazmat.primitives import serialization

    return _binance_key().public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()


def binance_sign(body: str) -> dict:
    """Headers for a webhook signed the way Binance signs them (SHA256withRSA)."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    timestamp, nonce = str(_now_ms()), secrets.token_hex(16)
    signature = _binance_key().sign(f"{timestamp}\n{nonce}\n{body}\n".encode(), padding.PKCS1v15(), hashes.SHA256())
    return {
        "Content-Type": "application/json",
        "BinancePay-Timestamp": timestamp, "BinancePay-Nonce": nonce,
        "BinancePay-Certificate-SN": CERT_SERIAL,
        "BinancePay-Signature": base64.b64encode(signature).decode(),
    }


def _binance_fail(code: str, message: str):
    return JsonResponse({"status": "FAIL", "code": code, "errorMessage": message})


def _binance_authorised(request) -> bool:
    timestamp = request.headers.get("BinancePay-Timestamp", "")
    nonce = request.headers.get("BinancePay-Nonce", "")
    expected = hmac.new(_held("BINANCE_PAY_API_SECRET").encode(),
                        f"{timestamp}\n{nonce}\n{request.body.decode()}\n".encode(), hashlib.sha512).hexdigest().upper()
    return (request.headers.get("BinancePay-Certificate-SN") == _held("BINANCE_PAY_API_KEY")
            and hmac.compare_digest(request.headers.get("BinancePay-Signature", ""), expected))


def _binance_order_state(order: dict) -> dict:
    if order["status"] == "INITIAL" and order.get("expireTime") and order["expireTime"] < _now_ms():
        order["status"] = "EXPIRED"
        _put("binance", order["merchantTradeNo"], order)
    return order


@csrf_exempt
def binance_api(request, endpoint: str):
    if request.method != "POST":
        return HttpResponse(status=405)
    if not _binance_authorised(request):
        return _binance_fail("400002", "Signature for this request is not valid.")
    body = _json_body(request)

    if endpoint == "certificates":
        return JsonResponse({"status": "SUCCESS", "code": "000000",
                             "data": [{"certSerial": CERT_SERIAL, "certPublic": binance_public_pem()}]})

    trade_no = str(body.get("merchantTradeNo") or "")
    if endpoint == "v3/order":
        missing = [k for k in ("merchantTradeNo", "orderAmount", "currency", "description", "goodsDetails", "env")
                   if not body.get(k)]
        if missing:
            return _binance_fail("400201", f"Missing parameter: {', '.join(missing)}")
        # Stricter than the reference ("special character is prohibited",
        # e.g. backslash, quotes, emoji): anything outside plain ASCII fails,
        # so a name the real gateway might refuse never reaches it.
        for text in [body["description"]] + [g.get("goodsName", "") for g in body["goodsDetails"]]:
            if not str(text).isascii() or any(ch in str(text) for ch in '\\"'):
                return _binance_fail("400201", "goodsName/description contains illegal characters.")
        if body.get("orderExpireTime") and int(body["orderExpireTime"]) > 15 * 24 * 3600 * 1000:
            return _binance_fail("400201", "orderExpireTime is invalid.")
        if not trade_no.isalnum() or len(trade_no) > 32:
            return _binance_fail("400201", "merchantTradeNo is invalid.")
        existing = _get("binance", trade_no)
        if existing and Decimal(str(existing["orderAmount"])) != Decimal(str(body["orderAmount"])):
            return _binance_fail("400201", "merchantTradeNo is duplicated.")
        prepay_id = existing["prepayId"] if existing else str(secrets.randbelow(10**18)).zfill(18)
        order = existing or _put("binance", trade_no, {
            "merchantTradeNo": trade_no, "prepayId": prepay_id, "status": "INITIAL",
            "orderAmount": f"{Decimal(str(body['orderAmount'])):.2f}", "currency": body["currency"],
            "webhookUrl": body.get("webhookUrl", ""), "returnUrl": body.get("returnUrl", ""),
            "cancelUrl": body.get("cancelUrl", ""), "expireTime": body.get("orderExpireTime"),
            "transactionId": "", "createTime": _now_ms(),
        })
        _put("binance-prepay", prepay_id, {"merchantTradeNo": trade_no})
        checkout = request.build_absolute_uri(reverse("payment_emulator:binance_checkout", args=[prepay_id]))
        return JsonResponse({"status": "SUCCESS", "code": "000000", "data": {
            "prepayId": prepay_id, "terminalType": "WEB", "expireTime": order.get("expireTime"),
            "qrcodeLink": "", "qrContent": checkout, "checkoutUrl": checkout,
            "deeplink": "", "universalUrl": checkout,
        }})

    order = _get("binance", trade_no)
    if order is None:
        return _binance_fail("400202", "Order not found.")
    order = _binance_order_state(order)
    if endpoint == "v2/order/query":
        return JsonResponse({"status": "SUCCESS", "code": "000000", "data": {
            "merchantId": 98765, "prepayId": order["prepayId"], "transactionId": order["transactionId"],
            "merchantTradeNo": trade_no, "status": order["status"], "currency": order["currency"],
            "orderAmount": order["orderAmount"], "openUserId": "emulator-payer",
            "transactTime": order.get("transactTime"), "createTime": order["createTime"],
        }})
    if endpoint == "order/close":
        if order["status"] == "PAID":
            return _binance_fail("400203", "Order has been paid and cannot be closed.")
        order["status"] = "CANCELED"
        _put("binance", trade_no, order)
        return JsonResponse({"status": "SUCCESS", "code": "000000", "data": True})
    raise Http404


def send_binance_webhook(order: dict, biz_status: str) -> int | None:
    data = json.dumps({
        "merchantTradeNo": order["merchantTradeNo"], "productType": "Digital", "productName": "Plan",
        "transactTime": order.get("transactTime"), "tradeType": "WEB", "totalFee": order["orderAmount"],
        "currency": order["currency"], "transactionId": order["transactionId"], "openUserId": "emulator-payer",
    }, separators=(",", ":"))
    body = json.dumps({"bizType": "PAY", "bizId": int(order["prepayId"]), "bizIdStr": order["prepayId"],
                       "bizStatus": biz_status, "data": data}, separators=(",", ":"))
    try:
        return requests.post(order["webhookUrl"], data=body, headers=binance_sign(body), timeout=15).status_code
    except requests.RequestException:
        return None


@csrf_exempt
def binance_checkout(request, prepay_id: str):
    """What the Binance app would show after scanning the QR."""
    link = _get("binance-prepay", prepay_id)
    order = _get("binance", link["merchantTradeNo"]) if link else None
    if order is None:
        raise Http404
    order = _binance_order_state(order)
    if request.method == "POST" and order["status"] == "INITIAL":
        action = request.POST.get("action")
        if action == "pay":
            order.update(status="PAID", transactionId=f"M{secrets.randbelow(10**15):015d}", transactTime=_now_ms())
            _put("binance", order["merchantTradeNo"], order)
            send_binance_webhook(order, "PAY_SUCCESS")
            return redirect(order["returnUrl"])
        if action == "close":
            order["status"] = "EXPIRED"
            _put("binance", order["merchantTradeNo"], order)
            send_binance_webhook(order, "PAY_CLOSED")
            return redirect(order["cancelUrl"])
    return _page(request, "Binance Pay", gateway="binance", order=order)


# -- Bakong ------------------------------------------------------------------------------------
@csrf_exempt
def bakong_check(request):
    if request.headers.get("Authorization") != f"Bearer {_held('BAKONG_API_TOKEN')}":
        return JsonResponse({"responseCode": 1, "responseMessage": "Unauthorized", "errorCode": 6, "data": None})
    md5 = str(_json_body(request).get("md5") or "")
    record = _get("bakong", md5)
    if record is None:
        return JsonResponse({"responseCode": 1, "errorCode": 1, "data": None,
                             "responseMessage": "Transaction could not be found. Please check and try again."})
    if record.get("failed"):
        return JsonResponse({"responseCode": 1, "errorCode": 3, "data": None, "responseMessage": "Transaction failed."})
    return JsonResponse({"responseCode": 0, "errorCode": None,
                         "responseMessage": "Getting transaction successfully.", "data": record})


def bakong_pay(khqr: str, *, to_account: str | None = None, failed: bool = False) -> dict:
    """What a banking app does after scanning: pay exactly what the QR says."""
    qr = read_khqr(khqr)
    return _put("bakong", qr["md5"], {
        "hash": secrets.token_hex(32), "fromAccountId": "student@wing",
        "toAccountId": to_account or qr["account"], "currency": qr["currency"],
        "amount": float(qr["amount"] or 0), "description": qr["bill"], "failed": failed,
        "createdDateMs": _now_ms(), "acknowledgedDateMs": _now_ms(),
    })


# -- the payer's side, one page per order ----------------------------------------------------------
@csrf_exempt
def payer(request, method: str, reference: str):
    """"Simulate the payer" from the payment page, for whichever gateway it uses."""
    payment = Payment.objects.filter(reference=reference).first()
    if payment is None:
        raise Http404
    back = f"/payments/{payment.reference}/"
    if method == "binance":
        order = _get("binance", trade_no(payment))
        if order is None:
            raise Http404("Open the payment page first so the Binance order exists.")
        return redirect("payment_emulator:binance_checkout", prepay_id=order["prepayId"])
    newest = payment.attempts.order_by("-created_at", "-id").first()
    if method == "aba_payway":
        tran_id = newest.provider_ref if newest else reference
        order = _get("aba", tran_id)
        return _page(request, "ABA PayWay", gateway="aba-info", order=order, back=back,
                     action=reverse("payment_emulator:aba_act", args=[tran_id]) if order else "")
    if method == "acleda_xpay":
        session = _get("xpay", newest.token) if newest else None
        return _page(request, "ACLEDA Bank", gateway="xpay-info", session=session, back=back,
                     action=reverse("payment_emulator:xpay_act", args=[newest.token]) if session else "")
    khqr = (payment.checkout_payload or {}).get("qr_payload", "")
    if not khqr:
        raise Http404("This order has no KHQR.")
    qr = read_khqr(khqr)
    if request.method == "POST":
        action = request.POST.get("action")
        bakong_pay(khqr, to_account="someone.else@wing" if action == "wrong" else None, failed=action == "fail")
        return redirect(back)
    return _page(request, "Bank app", gateway="khqr", qr=qr, back=back, record=_get("bakong", qr["md5"]))


app_name = "payment_emulator"
urlpatterns = [
    path("aba/api/payment-gateway/v1/payments/purchase", aba_purchase, name="aba_purchase"),
    path("aba/api/payment-gateway/v1/payments/check-transaction-2", aba_check, name="aba_check"),
    path("aba/act/<str:tran_id>/", aba_act, name="aba_act"),
    path("binance/binancepay/openapi/<path:endpoint>", binance_api, name="binance_api"),
    path("binance/checkout/<str:prepay_id>/", binance_checkout, name="binance_checkout"),
    path("bakong/v1/check_transaction_by_md5", bakong_check, name="bakong_check"),
    path("xpay/XPAYConnectorServiceInterfaceImplV2/XPAYConnectorServiceInterfaceImplV2RS/openSessionV2",
         xpay_open_session, name="xpay_open_session"),
    path("xpay/XPAYConnectorServiceInterfaceImplV2/XPAYConnectorServiceInterfaceImplV2RS/getTxnStatus",
         xpay_status, name="xpay_status"),
    path("xpay/paymentPage.jsp", xpay_payment_page, name="xpay_payment_page"),
    path("xpay/act/<str:token>/", xpay_act, name="xpay_act"),
    path("<str:method>/<str:reference>/", payer, name="payer"),
]
