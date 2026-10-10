"""Browser notifications (Web Push): subscriptions, delivery, the worker, the card.

Nothing here touches the network: ``pywebpush.webpush`` is replaced by a mock
that records what would have been sent, or raises what a push service would
answer. Background sends are run inline so their effects can be asserted.
"""
from __future__ import annotations

import base64
import json
import os
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from django.conf import settings
from django.core.management import call_command
from django.test import Client, TestCase, override_settings

from AI.management.commands.generate_vapid_keys import generate
from AI.models import (
    AccountStatus,
    Announcement,
    AnnouncementAudience,
    Message,
    MessageRole,
    NotificationCategory,
    NotificationPreference,
    PushSubscription,
    User,
)
from AI.services import chat_service, notification_service, push_service, settings_service
from pywebpush import WebPushException

PASSWORD = "Sup3rSecret!x"
PUBLIC_KEY, PRIVATE_KEY = generate()
KEYS = {"VAPID_PUBLIC_KEY": PUBLIC_KEY, "VAPID_PRIVATE_KEY": PRIVATE_KEY, "VAPID_SUBJECT": "mailto:ops@test.local"}
NO_KEYS = {"VAPID_PUBLIC_KEY": "", "VAPID_PRIVATE_KEY": ""}
STATES = ("checking", "unsupported", "needs-install", "not-configured", "blocked", "off", "on")


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def browser_subscription(token: str = "device-1", host: str = "fcm.googleapis.com") -> dict:
    """What ``PushSubscription.toJSON()`` gives in a real browser."""
    point = ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint,
    )
    return {
        "endpoint": f"https://{host}/fcm/send/{token}-secretpart",
        "expirationTime": None,
        "keys": {"p256dh": b64url(point), "auth": b64url(os.urandom(16))},
    }


def push_error(status: int) -> WebPushException:
    return WebPushException(f"Push failed: {status}", response=Mock(status_code=status, text=""))


def inline_spawn(target, *args):
    target(*args)


class PushFixture(TestCase):
    def setUp(self):
        settings_service.bootstrap()
        self.student = self.make_student("ann")
        self.other = self.make_student("ben")
        self.admin = User.objects.create_superuser(username="root", email="root@test.local", password=PASSWORD)

    @staticmethod
    def make_student(username: str) -> User:
        return User.objects.create_user(
            username=username, email=f"{username}@test.local", password=PASSWORD,
            is_verified=True, status=AccountStatus.ACTIVE,
        )

    def device(self, user=None, token="device-1") -> PushSubscription:
        return push_service.subscribe(user or self.student, browser_subscription(token), "Test browser")

    def prefs(self, user=None) -> NotificationPreference:
        return notification_service.preferences(user or self.student)


# ---------------------------------------------------------------------------
# Subscriptions
# ---------------------------------------------------------------------------
class SubscribeTests(PushFixture):
    def test_a_browser_subscription_is_stored_and_turns_push_on(self):
        NotificationPreference.objects.update_or_create(user=self.student, defaults={"push_enabled": False})
        data = browser_subscription()
        row = push_service.subscribe(self.student, data, "Chrome on Android")
        self.assertEqual(row.endpoint, data["endpoint"])
        self.assertEqual(row.p256dh, data["keys"]["p256dh"])
        self.assertEqual(row.auth, data["keys"]["auth"])
        self.assertEqual(row.user_agent, "Chrome on Android")
        self.assertTrue(self.prefs().push_enabled, "turning a device on is an explicit yes")

    def test_subscribing_again_updates_rather_than_duplicates(self):
        data = browser_subscription()
        push_service.subscribe(self.student, data)
        PushSubscription.objects.update(failure_count=3)
        data["keys"] = browser_subscription()["keys"]
        push_service.subscribe(self.student, data)
        self.assertEqual(PushSubscription.objects.count(), 1)
        row = PushSubscription.objects.get()
        self.assertEqual(row.p256dh, data["keys"]["p256dh"])
        self.assertEqual(row.failure_count, 0)

    def test_a_shared_browser_follows_whoever_signed_in_last(self):
        data = browser_subscription()
        push_service.subscribe(self.student, data)
        push_service.subscribe(self.other, data)
        row = PushSubscription.objects.get()
        self.assertEqual(row.user, self.other)
        self.assertEqual(push_service.device_count(self.student), 0)

    def test_the_old_endpoint_of_a_renewed_subscription_is_forgotten(self):
        old = self.device(token="old")
        push_service.subscribe(self.student, browser_subscription("new"), old_endpoint=old.endpoint)
        self.assertEqual(list(PushSubscription.objects.values_list("endpoint", flat=True)),
                         [browser_subscription("new")["endpoint"]])

    def test_another_students_endpoint_is_not_removed_as_an_old_one(self):
        theirs = self.device(self.other, token="theirs")
        push_service.subscribe(self.student, browser_subscription("mine"), old_endpoint=theirs.endpoint)
        self.assertTrue(PushSubscription.objects.filter(pk=theirs.pk, user=self.other).exists())

    def test_unusable_subscriptions_are_refused(self):
        good = browser_subscription()
        cases = {
            "not a dict": "https://example.com",
            "no endpoint": {"keys": good["keys"]},
            "plain http": {**good, "endpoint": "http://push.example.com/x"},
            "not a url": {**good, "endpoint": "javascript:alert(1)"},
            "no keys": {"endpoint": good["endpoint"]},
            "short p256dh": {**good, "keys": {**good["keys"], "p256dh": b64url(b"\x04" + b"a" * 10)}},
            "compressed p256dh": {**good, "keys": {**good["keys"], "p256dh": b64url(b"\x02" + b"a" * 64)}},
            "bad auth": {**good, "keys": {**good["keys"], "auth": b64url(b"a" * 8)}},
            "garbage auth": {**good, "keys": {**good["keys"], "auth": "%%%%"}},
            "too long": {**good, "endpoint": "https://push.example.com/" + "a" * 600},
        }
        for label, data in cases.items():
            with self.subTest(label):
                with self.assertRaises(push_service.PushError):
                    push_service.subscribe(self.student, data)
        self.assertEqual(PushSubscription.objects.count(), 0)

    def test_unsubscribe_only_touches_the_students_own_device(self):
        mine = self.device(token="mine")
        theirs = self.device(self.other, token="theirs")
        self.assertEqual(push_service.unsubscribe(self.student, theirs.endpoint), 0)
        self.assertEqual(push_service.unsubscribe(self.student, mine.endpoint), 1)
        self.assertEqual(list(PushSubscription.objects.values_list("pk", flat=True)), [theirs.pk])
        self.assertEqual(push_service.unsubscribe(self.student, ""), 0)

    def test_an_account_keeps_a_bounded_number_of_devices(self):
        with patch.object(push_service, "MAX_DEVICES", 3):
            for index in range(5):
                self.device(token=f"d{index}")
        self.assertEqual(push_service.device_count(self.student), 3)


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------
@override_settings(**KEYS)
class SendTests(PushFixture):
    def setUp(self):
        super().setUp()
        patcher = patch("AI.services.push_service.webpush")
        self.webpush = patcher.start()
        self.addCleanup(patcher.stop)

    def sent_payloads(self) -> list[dict]:
        return [json.loads(call.kwargs["data"]) for call in self.webpush.call_args_list]

    def test_each_device_gets_an_encrypted_signed_message(self):
        first, second = self.device(token="a"), self.device(token="b")
        count = push_service.send(self.student, "Payment confirmed", "Plan activated.", url="/app/billing/", tag="pay-1")
        self.assertEqual(count, 2)
        self.assertEqual(self.webpush.call_count, 2)
        call = self.webpush.call_args_list[0].kwargs
        self.assertEqual(call["subscription_info"]["endpoint"], first.endpoint)
        self.assertEqual(call["subscription_info"]["keys"], {"p256dh": first.p256dh, "auth": first.auth})
        self.assertEqual(call["vapid_claims"], {"sub": "mailto:ops@test.local"})
        self.assertEqual(call["ttl"], push_service.TTL_SECONDS)
        self.assertEqual(call["headers"]["Urgency"], "normal")
        self.assertEqual(call["headers"]["Topic"], "pay-1")
        self.assertIsNotNone(call["vapid_private_key"])
        self.assertEqual(self.sent_payloads()[0],
                         {"title": "Payment confirmed", "body": "Plan activated.", "url": "/app/billing/", "tag": "pay-1"})
        # pywebpush writes "aud" into the claims it is given; one dict shared
        # between devices would sign the second for the first's push service.
        claims = [c.kwargs["vapid_claims"] for c in self.webpush.call_args_list]
        self.assertIsNot(claims[0], claims[1])
        second.refresh_from_db()
        self.assertIsNotNone(second.last_success_at)

    def test_urls_that_are_not_a_path_or_http_are_replaced(self):
        self.device()
        push_service.send(self.student, "Hi", url="javascript:alert(1)")
        push_service.send(self.student, "Hi", url="//evil.example/")
        self.assertEqual([p["url"] for p in self.sent_payloads()], ["/chat/", "/chat/"])

    @override_settings(**NO_KEYS)
    def test_nothing_is_sent_without_keys(self):
        self.device()
        self.assertFalse(push_service.is_configured())
        self.assertEqual(push_service.send(self.student, "Hi"), 0)
        self.webpush.assert_not_called()

    def test_the_account_switch_is_respected(self):
        self.device()
        NotificationPreference.objects.filter(user=self.student).update(push_enabled=False)
        self.assertEqual(push_service.send(self.student, "Hi"), 0)
        self.webpush.assert_not_called()

    def test_category_switches_are_the_same_as_in_app(self):
        self.device()
        NotificationPreference.objects.filter(user=self.student).update(
            payment_alerts=False, low_token_alerts=False, expiry_reminders=False, announcements=False,
        )
        for category in (NotificationCategory.PAYMENT, NotificationCategory.TOKENS,
                         NotificationCategory.EXPIRY, NotificationCategory.ANNOUNCEMENT):
            with self.subTest(category=category):
                self.assertEqual(push_service.send(self.student, "Hi", category=category), 0)
        self.webpush.assert_not_called()
        self.assertEqual(push_service.send(self.student, "Reply", category=NotificationCategory.SYSTEM), 1)
        self.assertEqual(push_service.send(self.student, "Plain"), 1)

    def test_a_gone_subscription_is_deleted(self):
        for status in (404, 410):
            with self.subTest(status=status):
                row = self.device(token=f"gone-{status}")
                self.webpush.side_effect = push_error(status)
                self.assertEqual(push_service.send(self.student, "Hi"), 0)
                self.assertFalse(PushSubscription.objects.filter(pk=row.pk).exists())

    def test_repeated_failures_prune_a_device_and_success_resets_the_count(self):
        row = self.device()
        self.webpush.side_effect = push_error(500)
        for attempt in range(1, push_service.MAX_FAILURES):
            push_service.send(self.student, "Hi")
            row.refresh_from_db()
            self.assertEqual(row.failure_count, attempt)
        self.webpush.side_effect = None
        push_service.send(self.student, "Hi")
        row.refresh_from_db()
        self.assertEqual(row.failure_count, 0)

        self.webpush.side_effect = requests.ConnectionError("down")
        for _ in range(push_service.MAX_FAILURES):
            self.assertEqual(push_service.send(self.student, "Hi"), 0)
        self.assertFalse(PushSubscription.objects.filter(pk=row.pk).exists())

    def test_failures_never_raise_and_never_log_the_endpoint(self):
        row = self.device()
        self.webpush.side_effect = push_error(500)
        with self.assertLogs("AI.push", level="WARNING") as logs:
            self.assertEqual(push_service.send(self.student, "Hi"), 0)
        output = "\n".join(logs.output)
        self.assertIn("fcm.googleapis.com", output)
        self.assertNotIn("secretpart", output)
        self.assertNotIn(row.auth, output)

        with patch.object(push_service, "_preferences", side_effect=RuntimeError("db down")),                 self.assertLogs("AI.push", level="ERROR"):
            self.assertEqual(push_service.send(self.student, "Hi"), 0)

    def test_a_test_ignores_the_switches_but_needs_a_device(self):
        self.assertEqual(push_service.send_test(self.student), 0)
        self.device()
        NotificationPreference.objects.filter(user=self.student).update(push_enabled=False)
        self.assertEqual(push_service.send_test(self.student), 1)
        self.assertEqual(self.sent_payloads()[0]["tag"], "push-test")

    def test_a_bad_private_key_means_not_configured(self):
        with override_settings(VAPID_PRIVATE_KEY="not-a-key"), self.assertLogs("AI.push", level="ERROR"):
            self.assertFalse(push_service.is_configured())
            self.assertEqual(push_service.public_key(), "")

    def test_the_public_key_is_derived_from_the_private_one(self):
        self.assertEqual(push_service.public_key(), PUBLIC_KEY)
        with override_settings(VAPID_PUBLIC_KEY="mismatch"), self.assertLogs("AI.push", level="WARNING"):
            self.assertEqual(push_service.public_key(), PUBLIC_KEY)

    def test_queue_sends_after_commit_off_the_request_thread(self):
        self.device()
        with patch("AI.services.push_service._spawn") as spawn:
            with self.captureOnCommitCallbacks() as callbacks:
                self.assertEqual(push_service.queue(self.student, "Hi", "There"), 1)
                spawn.assert_not_called()  # not before the transaction commits
            for callback in callbacks:
                callback()
        spawn.assert_called_once()
        target, *args = spawn.call_args.args
        target(*args)
        self.assertEqual(self.sent_payloads()[0]["title"], "Hi")

    def test_queue_starts_no_thread_for_a_student_without_devices(self):
        with patch("AI.services.push_service._spawn") as spawn, self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(push_service.queue(self.student, "Hi"), 0)
        spawn.assert_not_called()


@override_settings(**KEYS)
class AnswerReadyTests(PushFixture):
    def setUp(self):
        super().setUp()
        patcher = patch("AI.services.push_service.webpush")
        self.webpush = patcher.start()
        self.addCleanup(patcher.stop)
        self.device()
        self.chat = chat_service.create_conversation(self.student, "Photosynthesis basics")
        self.answer = Message.objects.create(
            conversation=self.chat, role=MessageRole.ASSISTANT,
            content="## Overview\n**Photosynthesis** turns [light](https://x.test) into sugar.\n```py\nprint(1)\n```",
        )

    def test_the_student_hears_the_answer_is_ready(self):
        self.assertEqual(push_service.answer_ready(self.student, self.chat, self.answer), 1)
        payload = json.loads(self.webpush.call_args.kwargs["data"])
        self.assertEqual(payload["title"], "Your answer is ready")
        self.assertEqual(payload["url"], f"/chat/{self.chat.pk}/")
        self.assertEqual(payload["tag"], f"answer-{self.chat.pk}")
        self.assertTrue(payload["body"].startswith("Photosynthesis basics: "), payload["body"])
        self.assertIn("Photosynthesis turns light into sugar.", payload["body"])
        for mark in ("**", "##", "](", "```", "print(1)"):
            self.assertNotIn(mark, payload["body"])

    def test_the_answers_switch_is_respected(self):
        NotificationPreference.objects.filter(user=self.student).update(push_answers=False)
        self.assertEqual(push_service.answer_ready(self.student, self.chat, self.answer), 0)
        NotificationPreference.objects.filter(user=self.student).update(push_answers=True, push_enabled=False)
        self.assertEqual(push_service.answer_ready(self.student, self.chat, self.answer), 0)
        self.webpush.assert_not_called()

    def test_an_empty_answer_or_missing_chat_is_harmless(self):
        self.assertEqual(push_service.answer_ready(self.student, None, self.answer), 0)
        self.assertEqual(push_service.answer_ready(self.student, self.chat, SimpleNamespace(content="")), 1)
        self.assertIn("tap to read it", json.loads(self.webpush.call_args.kwargs["data"])["body"])
        self.assertEqual(push_service.answer_ready(self.student, self.chat, "Plain text answer"), 1)


# ---------------------------------------------------------------------------
# The notification service
# ---------------------------------------------------------------------------
@override_settings(**KEYS)
class NotifyTests(PushFixture):
    def setUp(self):
        super().setUp()
        patcher = patch("AI.services.push_service.webpush")
        self.webpush = patcher.start()
        self.addCleanup(patcher.stop)

    def notify(self, **kwargs):
        with patch("AI.services.push_service._spawn", side_effect=inline_spawn), \
                self.captureOnCommitCallbacks(execute=True):
            return notification_service.notify(self.student, "Payment confirmed", "Plan activated.", **kwargs)

    def test_an_in_app_notification_is_pushed_too(self):
        self.device()
        notification = self.notify(category=NotificationCategory.PAYMENT, action_url="/payments/R1/receipt/")
        payload = json.loads(self.webpush.call_args.kwargs["data"])
        self.assertEqual(payload["title"], "Payment confirmed")
        self.assertEqual(payload["body"], "Plan activated.")
        self.assertEqual(payload["url"], "/payments/R1/receipt/")
        self.assertEqual(payload["tag"], f"n-{notification.pk}")

    def test_without_an_action_it_opens_the_inbox(self):
        self.device()
        self.notify()
        self.assertEqual(json.loads(self.webpush.call_args.kwargs["data"])["url"], "/app/notifications/")

    def test_a_switched_off_category_is_not_pushed_but_still_lands_in_app(self):
        self.device()
        NotificationPreference.objects.filter(user=self.student).update(payment_alerts=False)
        notification = self.notify(category=NotificationCategory.PAYMENT)
        self.assertTrue(notification.pk)
        self.webpush.assert_not_called()

    def test_a_push_failure_never_breaks_notify(self):
        self.device()
        self.webpush.side_effect = RuntimeError("boom")
        notification = self.notify()
        self.assertTrue(notification.pk)
        with patch("AI.services.push_service.queue", side_effect=RuntimeError("boom")),                 self.assertLogs("AI.notifications", level="ERROR"):
            self.assertTrue(notification_service.notify(self.student, "Still fine").pk)


@override_settings(**KEYS)
class AnnouncementPushTests(PushFixture):
    def setUp(self):
        super().setUp()
        patcher = patch("AI.services.push_service.webpush")
        self.webpush = patcher.start()
        self.addCleanup(patcher.stop)
        self.pace = patch("AI.services.push_service.time.sleep")
        self.pace.start()
        self.addCleanup(self.pace.stop)

    def publish(self, **extra):
        announcement = Announcement.objects.create(
            title="Exam week hours", body="The assistant is free all week.", created_by=self.admin,
            audience=AnnouncementAudience.ALL, **extra,
        )
        with patch("AI.services.push_service._spawn", side_effect=inline_spawn), \
                patch("AI.services.notification_service._spawn"), \
                self.captureOnCommitCallbacks(execute=True):
            notification_service.publish_announcement(announcement)
        return announcement

    def endpoints(self) -> set[str]:
        return {call.kwargs["subscription_info"]["endpoint"] for call in self.webpush.call_args_list}

    def test_the_audience_is_pushed_in_the_background_with_its_switches(self):
        mine = self.device(token="ann")
        theirs = self.device(self.other, token="ben")
        quiet = self.make_student("cat")
        self.device(quiet, token="cat")
        NotificationPreference.objects.filter(user=quiet).update(announcements=False)
        muted = self.make_student("dan")
        self.device(muted, token="dan")
        NotificationPreference.objects.filter(user=muted).update(push_enabled=False)

        announcement = self.publish(action_url="/app/pricing/", action_label="See plans")
        self.assertEqual(announcement.push_queued, 2)
        self.assertEqual(self.endpoints(), {mine.endpoint, theirs.endpoint})
        payload = json.loads(self.webpush.call_args.kwargs["data"])
        self.assertEqual(payload["title"], "Exam week hours")
        self.assertEqual(payload["url"], "/app/pricing/")
        self.assertEqual(payload["tag"], f"announcement-{announcement.pk}")

    def test_nobody_with_a_device_means_nothing_is_queued(self):
        announcement = self.publish()
        self.assertEqual(announcement.push_queued, 0)
        self.webpush.assert_not_called()

    def test_a_retracted_announcement_stops_part_way(self):
        users = [self.make_student(f"s{n}") for n in range(55)]
        for user in users:
            self.device(user, token=user.username)
        announcement = Announcement.objects.create(
            title="Oops", body="Sent by mistake.", created_by=self.admin, audience=AnnouncementAudience.ALL,
        )
        ids = push_service.announcement_recipients(announcement)
        Announcement.objects.filter(pk=announcement.pk).update(retracted_at=announcement.created_at)
        counts = push_service.send_announcement(announcement.pk, ids)
        self.assertEqual(counts["users"], 50)


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------
@override_settings(**KEYS)
class ViewTests(PushFixture):
    AJAX = {"HTTP_X_REQUESTED_WITH": "XMLHttpRequest", "HTTP_ACCEPT": "application/json"}

    def post(self, client, url, data=None, **headers):
        return client.post(url, data=json.dumps(data or {}), content_type="application/json", **self.AJAX, **headers)

    def test_the_service_worker_is_served_from_the_root(self):
        response = Client().get("/sw.js")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response["Content-Type"].startswith("application/javascript"))
        self.assertEqual(response["Service-Worker-Allowed"], "/")
        self.assertEqual(response["Cache-Control"], "no-cache")
        body = response.content.decode()
        for needle in ("showNotification", "notificationclick", "openWindow", "pushsubscriptionchange",
                       "/api/push/subscribe/", "brand/icon-192.png", "brand/badge-96.png"):
            self.assertIn(needle, body)
        self.assertNotIn("addEventListener('fetch'", body, "the worker must not intercept page loads")
        self.assertNotIn("{{", body)

    def test_the_manifest_describes_an_installable_app(self):
        response = Client().get("/manifest.webmanifest")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response["Content-Type"].startswith("application/manifest+json"))
        data = json.loads(response.content)
        self.assertEqual(data["name"], settings.BRAND["full_name"])
        self.assertEqual(data["short_name"], settings.BRAND["name"])
        self.assertEqual(data["start_url"], "/chat/")
        self.assertEqual(data["display"], "standalone")
        self.assertTrue(data["theme_color"].startswith("#"))
        sizes = {(icon["sizes"], icon["purpose"]) for icon in data["icons"]}
        self.assertTrue({("192x192", "any"), ("512x512", "any"), ("512x512", "maskable")} <= sizes)
        for icon in data["icons"]:
            path = Path(settings.BASE_DIR) / "static" / icon["src"].replace(settings.STATIC_URL, "", 1)
            self.assertTrue(path.is_file(), icon["src"])

    def test_the_icons_are_the_sizes_they_claim(self):
        from PIL import Image

        brand = Path(settings.BASE_DIR) / "static" / "brand"
        for name, size in (("icon-192.png", 192), ("icon-512.png", 512), ("icon-maskable-192.png", 192),
                           ("icon-maskable-512.png", 512), ("badge-96.png", 96)):
            with self.subTest(name), Image.open(brand / name) as image:
                self.assertEqual(image.size, (size, size))
        with Image.open(brand / "icon-maskable-512.png") as image:
            self.assertEqual(image.convert("RGBA").getpixel((0, 0))[3], 255, "a maskable icon is full-bleed")

    def test_the_api_is_for_signed_in_students(self):
        anonymous = Client()
        self.assertEqual(anonymous.get("/api/push/key/", **self.AJAX).status_code, 401)
        self.assertEqual(self.post(anonymous, "/api/push/subscribe/", browser_subscription()).status_code, 401)
        admin = Client()
        admin.force_login(self.admin)
        self.assertEqual(admin.get("/api/push/key/").status_code, 302)
        self.assertEqual(PushSubscription.objects.count(), 0)

    def test_the_key_endpoint_gives_the_worker_what_it_needs(self):
        client = Client()
        client.force_login(self.student)
        data = client.get("/api/push/key/", **self.AJAX).json()
        self.assertTrue(data["configured"])
        self.assertEqual(data["public_key"], PUBLIC_KEY)
        self.assertTrue(data["csrf"])
        with override_settings(**NO_KEYS):
            data = client.get("/api/push/key/", **self.AJAX).json()
        self.assertEqual((data["configured"], data["public_key"]), (False, ""))

    def test_posts_need_a_csrf_token(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.student)
        data = {"subscription": browser_subscription()}
        self.assertEqual(self.post(client, "/api/push/subscribe/", data).status_code, 403)
        self.assertEqual(self.post(client, "/api/push/test/").status_code, 403)
        self.assertEqual(self.post(client, "/api/push/unsubscribe/").status_code, 403)
        token = client.get("/api/push/key/", **self.AJAX).json()["csrf"]
        response = self.post(client, "/api/push/subscribe/", data, HTTP_X_CSRFTOKEN=token)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(PushSubscription.objects.get().user, self.student)

    def test_subscribe_unsubscribe_round_trip(self):
        client = Client()
        client.force_login(self.student)
        data = browser_subscription()
        response = self.post(client, "/api/push/subscribe/", {"subscription": data},
                             HTTP_USER_AGENT="Mozilla/5.0 (Android) Chrome/140")
        self.assertEqual(response.json(), {"ok": True, "subscribed": True, "devices": 1})
        self.assertIn("Android", PushSubscription.objects.get().user_agent)
        response = self.post(client, "/api/push/unsubscribe/", {"endpoint": data["endpoint"]})
        self.assertEqual(response.json(), {"ok": True, "removed": 1, "devices": 0})
        self.assertEqual(client.get("/api/push/subscribe/").status_code, 405)

    def test_a_bad_subscription_is_a_400(self):
        client = Client()
        client.force_login(self.student)
        response = self.post(client, "/api/push/subscribe/", {"subscription": {"endpoint": "http://x/"}})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "invalid_subscription")
        response = client.post("/api/push/subscribe/", data="not json", content_type="application/json", **self.AJAX)
        self.assertEqual(response.status_code, 400)

    @override_settings(**NO_KEYS)
    def test_without_keys_the_api_says_so(self):
        client = Client()
        client.force_login(self.student)
        response = self.post(client, "/api/push/subscribe/", {"subscription": browser_subscription()})
        self.assertEqual((response.status_code, response.json()["code"]), (503, "not_configured"))
        self.assertEqual(self.post(client, "/api/push/test/").status_code, 503)

    def test_the_test_button(self):
        client = Client()
        client.force_login(self.student)
        response = self.post(client, "/api/push/test/")
        self.assertEqual((response.status_code, response.json()["code"]), (409, "no_devices"))
        self.device()
        with patch("AI.services.push_service.webpush") as webpush:
            response = self.post(client, "/api/push/test/")
            self.assertEqual(response.json(), {"ok": True, "sent": 1})
            webpush.side_effect = push_error(500)
            self.assertEqual(self.post(client, "/api/push/test/").status_code, 502)

    def test_the_test_button_is_rate_limited(self):
        client = Client()
        client.force_login(self.student)
        statuses = [self.post(client, "/api/push/test/").status_code for _ in range(6)]
        self.assertEqual(statuses[-1], 429)


# ---------------------------------------------------------------------------
# generate_vapid_keys
# ---------------------------------------------------------------------------
class GenerateKeysTests(TestCase):
    def run_command(self, *args) -> dict[str, str]:
        out = StringIO()
        call_command("generate_vapid_keys", *args, stdout=out, stderr=StringIO())
        lines = [line for line in out.getvalue().splitlines() if "=" in line and not line.startswith("#")]
        return dict(line.split("=", 1) for line in lines)

    def test_the_output_is_a_pair_pywebpush_accepts(self):
        from py_vapid import Vapid
        from py_vapid.utils import b64urlencode

        values = self.run_command()
        self.assertEqual(set(values), {"VAPID_PUBLIC_KEY", "VAPID_PRIVATE_KEY", "VAPID_SUBJECT"})
        vapid = Vapid.from_string(private_key=values["VAPID_PRIVATE_KEY"])
        derived = b64urlencode(vapid.public_key.public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint,
        ))
        self.assertEqual(derived, values["VAPID_PUBLIC_KEY"])
        raw = base64.urlsafe_b64decode(values["VAPID_PUBLIC_KEY"] + "==")
        self.assertEqual((len(raw), raw[:1]), (65, b"\x04"))
        self.assertNotIn("=", values["VAPID_PUBLIC_KEY"] + values["VAPID_PRIVATE_KEY"])
        self.assertTrue(values["VAPID_SUBJECT"].startswith("mailto:"))
        # And the service agrees, signing with it.
        with override_settings(VAPID_PRIVATE_KEY=values["VAPID_PRIVATE_KEY"], VAPID_PUBLIC_KEY=values["VAPID_PUBLIC_KEY"]):
            self.assertTrue(push_service.is_configured())
            self.assertEqual(push_service.public_key(), values["VAPID_PUBLIC_KEY"])
            headers = push_service._vapid().sign({"sub": "mailto:a@b.c", "aud": "https://fcm.googleapis.com"})
            self.assertIn("vapid", headers["Authorization"].lower())

    def test_each_run_is_a_new_pair_and_the_subject_can_be_set(self):
        first, second = self.run_command(), self.run_command("--subject", "ops@example.com")
        self.assertNotEqual(first["VAPID_PRIVATE_KEY"], second["VAPID_PRIVATE_KEY"])
        self.assertEqual(second["VAPID_SUBJECT"], "mailto:ops@example.com")


# ---------------------------------------------------------------------------
# The Settings card
# ---------------------------------------------------------------------------
class SettingsCardTests(PushFixture):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.student)

    def card(self, html: str) -> str:
        start = html.index('<section class="panel-section" id="push">')
        return html[start:html.index("</section>", start)]

    @override_settings(**NO_KEYS)
    def test_without_keys_the_card_says_it_is_not_set_up(self):
        html = self.client.get("/app/settings/").content.decode()
        card = self.card(html)
        self.assertIn('data-push-state="not-configured"', card)
        self.assertIn('data-push-configured="0"', card)
        self.assertIn("generate_vapid_keys", card)

    @override_settings(**KEYS)
    def test_with_keys_the_browser_decides_and_every_state_has_words(self):
        html = self.client.get("/app/settings/", HTTP_X_APP_PANEL="1").content.decode()
        card = self.card(html)
        self.assertIn('data-push-state="checking"', card)
        self.assertIn(f'data-push-key="{PUBLIC_KEY}"', card)
        for state in STATES:
            with self.subTest(state=state):
                self.assertIn(f'data-push-show="{state}"', card)
                self.assertIn(f'.ps-card:not([data-push-state="{state}"]) [data-push-show="{state}"]', html)
        for action in ("enable", "disable", "test", "refresh"):
            self.assertIn(f'data-push-action="{action}"', card)
        for words in ("Turn on", "Turn off", "Send a test", "Add to Home Screen", "iOS 16.4",
                      "set <b>Notifications</b> to <b>Allow</b>"):
            self.assertIn(words, card)

    def test_what_arrives_follows_the_switches(self):
        NotificationPreference.objects.update_or_create(user=self.student, defaults={"payment_alerts": False})
        card = self.card(self.client.get("/app/settings/", HTTP_X_APP_PANEL="1").content.decode())
        for label in ("Answers ready", "Payment updates", "Support replies", "Low tokens", "Plan reminders",
                      "Announcements"):
            self.assertIn(label, card)
        payment = card[card.rindex("<li", 0, card.index("Payment updates")):card.index("Payment updates")]
        self.assertIn("is-off", payment)

    def test_the_answers_switch_saves(self):
        response = self.client.post("/app/settings/", {"action": "push_prefs"}, HTTP_X_APP_PANEL="1")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.prefs().push_answers)
        self.assertNotIn('name="push_answers" checked', response.content.decode())
        self.client.post("/app/settings/", {"action": "push_prefs", "push_answers": "on"}, HTTP_X_APP_PANEL="1")
        self.assertTrue(self.prefs().push_answers)
        # The other switches are left alone.
        self.assertTrue(self.prefs().payment_alerts)

    def test_the_shell_loads_the_client_and_links_the_manifest(self):
        html = self.client.get("/chat/").content.decode()
        self.assertIn('<link rel="manifest" href="/manifest.webmanifest">', html)
        self.assertIn('name="apple-mobile-web-app-capable"', html)
        self.assertRegex(html, r'<script src="/static/js/push\.js" defer></script>')

    def test_the_client_script_is_wired_by_delegation(self):
        source = (Path(settings.BASE_DIR) / "static" / "js" / "push.js").read_text(encoding="utf-8")
        for needle in ("register(SW_URL, { scope: '/' })", "data-push-action", "MutationObserver", "panelHost",
                       "window.pushRefresh", "requestPermission", "userVisibleOnly: true", "navigator.standalone",
                       "'needs-install'", "'not-configured'", "'blocked'", "X-CSRFToken"):
            self.assertIn(needle, source)
        self.assertNotRegex(source, r"\bform\.(action|submit|method)\b")
