"""Telegram: linking, delivery, the bot's commands, announcements and sign-in codes.

No test here talks to Telegram. ``requests.post`` inside the service is
replaced by :class:`FakeTelegram`, which answers each Bot API method the way
the real API does — including its failures: 403 "bot was blocked by the
user", 429 with ``parameters.retry_after``, 400 "chat not found", and a
network error — and records every call so a test can say exactly what would
have been sent, and to whom.
"""
from __future__ import annotations

import json
import re
from unittest.mock import patch

import requests
from django.core import mail
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from AI.models import (
    AccountStatus,
    Announcement,
    AnnouncementAudience,
    Notification,
    NotificationLevel,
    NotificationPreference,
    OTPChannel,
    OTPCode,
    OTPPurpose,
    Role,
    TelegramAccount,
    TelegramDelivery,
    TelegramDeliveryStatus,
    User,
)
from AI.services import notification_service, otp_service, settings_service, support_service, telegram_service
from AI.tests.helpers import GPT, fund, model_for

PASSWORD = "Sup3rSecret!x"
TOKEN = "123456:TEST-TOKEN"

TG = dict(
    TELEGRAM_BOT_TOKEN=TOKEN,
    TELEGRAM_BOT_USERNAME="epc_test_bot",
    TELEGRAM_WEBHOOK_SECRET="s3cret-token",
    TELEGRAM_CHANNEL_ID="@aiassistanthelps",
    TELEGRAM_CHANNEL_URL="https://t.me/aiassistanthelps",
    TELEGRAM_LINK_TTL_MINUTES=15,
    SITE_URL="https://ai.example.com",
)

BLOCKED = {"ok": False, "error_code": 403, "description": "Forbidden: bot was blocked by the user"}
DEACTIVATED = {"ok": False, "error_code": 403, "description": "Forbidden: user is deactivated"}
CHAT_NOT_FOUND = {"ok": False, "error_code": 400, "description": "Bad Request: chat not found"}


def rate_limited(seconds: int) -> dict:
    return {"ok": False, "error_code": 429, "description": f"Too Many Requests: retry after {seconds}",
            "parameters": {"retry_after": seconds}}


class FakeResponse:
    def __init__(self, data: dict):
        self._data = data
        self.status_code = data.get("error_code", 200) if isinstance(data, dict) else 200

    def json(self):
        return self._data


class FakeTelegram:
    """Stands in for ``requests.post`` against the Bot API."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.queued: dict[str, list] = {}
        self.message_id = 100

    def queue(self, method: str, *responses) -> None:
        self.queued.setdefault(method, []).extend(responses)

    def __call__(self, url, json=None, timeout=None, **kwargs):
        method = url.rsplit("/", 1)[-1]
        self.calls.append((method, json or {}))
        pending = self.queued.get(method)
        data = pending.pop(0) if pending else self._default(method, json or {})
        if isinstance(data, Exception):
            raise data
        return FakeResponse(data)

    def _default(self, method: str, payload: dict) -> dict:
        if method == "sendMessage":
            self.message_id += 1
            return {"ok": True, "result": {"message_id": self.message_id,
                                           "chat": {"id": payload.get("chat_id")}, "text": "…"}}
        if method == "getMe":
            return {"ok": True, "result": {"id": 999, "is_bot": True, "first_name": "EPC",
                                           "username": "epc_test_bot"}}
        if method == "getWebhookInfo":
            return {"ok": True, "result": {"url": "", "pending_update_count": 0}}
        if method == "getChat":
            return {"ok": True, "result": {"id": -1001234567890, "type": "channel",
                                           "title": "AI Assistant Helps", "username": "aiassistanthelps"}}
        if method == "getChatMember":
            return {"ok": True, "result": {"status": "administrator", "can_post_messages": True,
                                           "can_delete_messages": True, "user": {"id": 999}}}
        return {"ok": True, "result": True}

    def sent(self, method: str = "sendMessage") -> list[dict]:
        return [payload for name, payload in self.calls if name == method]


def make_user(username: str, **extra) -> User:
    defaults = {"email": f"{username}@test.local", "full_name": username.title(), "is_verified": True,
                "status": AccountStatus.ACTIVE, "role": Role.STUDENT}
    defaults.update(extra)
    return User.objects.create_user(username=username, password=PASSWORD, **defaults)


def private_chat(chat_id: int = 5550001, username: str = "sok") -> dict:
    return {"id": chat_id, "type": "private", "username": username, "first_name": "Sok"}


def message(text: str, chat: dict) -> dict:
    sender = {"id": chat["id"], "is_bot": False, "first_name": "Sok", "username": chat.get("username", "")}
    return {"update_id": 1, "message": {"message_id": 7, "date": 0, "chat": chat, "from": sender, "text": text}}


def link(user, chat_id: int = 5550001, *, username: str = "sok", chat_type: str = "private") -> TelegramAccount:
    """A linked account, as if the student had pressed Start."""
    account = telegram_service.get_or_create_link(user)
    account.telegram_id = str(chat_id)
    account.username = username
    account.chat_type = chat_type
    account.is_linked = True
    account.linked_at = timezone.now()
    account.save()
    return account


@override_settings(**TG)
class TelegramTestCase(TestCase):
    def setUp(self):
        cache.clear()
        settings_service.bootstrap()
        self.fake = FakeTelegram()
        patcher = patch("AI.services.telegram_service.requests.post", side_effect=self.fake)
        self.post = patcher.start()
        self.addCleanup(patcher.stop)
        sleeper = patch("AI.services.telegram_service.time.sleep")
        self.sleep = sleeper.start()
        self.addCleanup(sleeper.stop)
        self.student = make_user("sokha")

    def texts_to(self, chat_id) -> list[str]:
        return [p["text"] for p in self.fake.sent() if str(p["chat_id"]) == str(chat_id)]


# ---------------------------------------------------------------------------
# Linking security
# ---------------------------------------------------------------------------
class LinkingTests(TelegramTestCase):
    def test_opening_settings_never_creates_a_usable_link(self):
        self.client.force_login(self.student)
        self.client.get("/app/settings/", HTTP_X_APP_PANEL="1")
        account = TelegramAccount.objects.get(user=self.student)
        self.assertIsNone(account.link_token_expires_at)
        self.assertEqual(telegram_service.deep_link(account), "")
        # The token that does exist cannot link anything.
        status = telegram_service.handle_update(message(f"/start {account.link_token}", private_chat()))
        self.assertEqual(status, "invalid")
        self.assertFalse(TelegramAccount.objects.get(pk=account.pk).is_linked)

    def test_a_fresh_link_works_once_and_is_retired(self):
        account = telegram_service.issue_link_token(self.student)
        token = account.link_token
        self.assertTrue(telegram_service.link_is_valid(account))
        self.assertEqual(telegram_service.deep_link(account), f"https://t.me/epc_test_bot?start={token}")

        self.assertEqual(telegram_service.handle_update(message(f"/start {token}", private_chat(111))), "linked")
        account.refresh_from_db()
        self.assertTrue(account.is_linked)
        self.assertEqual(account.telegram_id, "111")
        self.assertEqual(account.chat_type, "private")
        self.assertEqual(account.username, "sok")
        # Rotated and unusable: a leaked copy of the link cannot re-point the codes.
        self.assertNotEqual(account.link_token, token)
        self.assertIsNone(account.link_token_expires_at)
        self.assertIn("Linked successfully", self.texts_to(111)[-1])

        # Somebody else presses the same link later.
        self.assertEqual(telegram_service.handle_update(message(f"/start {token}", private_chat(222, "thief"))),
                         "invalid")
        account.refresh_from_db()
        self.assertEqual(account.telegram_id, "111")
        self.assertIn("can't be used", self.texts_to(222)[-1])

    def test_an_expired_link_is_refused_with_a_friendly_message(self):
        account = telegram_service.issue_link_token(self.student)
        account.link_token_expires_at = timezone.now() - timezone.timedelta(seconds=1)
        account.save(update_fields=["link_token_expires_at"])
        status = telegram_service.handle_update(message(f"/start {account.link_token}", private_chat(333)))
        self.assertEqual(status, "expired")
        self.assertFalse(TelegramAccount.objects.get(pk=account.pk).is_linked)
        self.assertIn("expired", self.texts_to(333)[-1])
        self.assertIn("15 minutes", self.texts_to(333)[-1])

    def test_links_expire_after_the_configured_minutes(self):
        before = timezone.now()
        account = telegram_service.issue_link_token(self.student)
        minutes = (account.link_token_expires_at - before).total_seconds() / 60
        self.assertAlmostEqual(minutes, 15, delta=0.1)

    def test_a_group_or_channel_can_never_be_linked(self):
        account = telegram_service.issue_link_token(self.student)
        for chat in ({"id": -100555, "type": "supergroup", "title": "Class 12B"},
                     {"id": -444, "type": "group", "title": "Friends"}):
            with self.subTest(chat=chat["type"]):
                status = telegram_service.handle_update(message(f"/start@epc_test_bot {account.link_token}", chat))
                self.assertEqual(status, "not_private")
        account.refresh_from_db()
        self.assertFalse(account.is_linked)
        # The token survives, so the student can still use it from their own chat.
        self.assertTrue(telegram_service.link_is_valid(account))

    def test_linking_moves_a_chat_off_any_other_account(self):
        other = make_user("dara")
        link(other, 777)
        account = telegram_service.issue_link_token(self.student)
        telegram_service.handle_update(message(f"/start {account.link_token}", private_chat(777)))
        self.assertFalse(TelegramAccount.objects.get(user=other).is_linked)
        self.assertEqual(TelegramAccount.objects.get(user=self.student).telegram_id, "777")

    def test_start_without_a_token_explains_how_to_connect(self):
        self.assertEqual(telegram_service.handle_update(message("/start", private_chat(900))), "greeted")
        self.assertIn("Connect Telegram", self.texts_to(900)[-1])


# ---------------------------------------------------------------------------
# Codes only ever go to a linked, private, reachable chat
# ---------------------------------------------------------------------------
class OTPRoutingTests(TelegramTestCase):
    def code_in(self, text: str) -> str:
        return re.search(r"<code>(\d+)</code>", text).group(1)

    def test_a_private_linked_chat_gets_the_code_and_the_log_never_holds_it(self):
        settings_service.set_value("otp_send_everywhere", False)
        link(self.student, 4242)
        otp = otp_service.issue(self.student, OTPPurpose.LOGIN, channel=OTPChannel.TELEGRAM)
        self.assertEqual(otp.channel, OTPChannel.TELEGRAM)
        code = self.code_in(self.texts_to(4242)[-1])
        otp_service.verify(self.student, OTPPurpose.LOGIN, code)
        self.assertEqual(len(mail.outbox), 0)

        delivery = TelegramDelivery.objects.get(kind="otp")
        self.assertEqual(delivery.status, TelegramDeliveryStatus.SENT)
        self.assertEqual(delivery.chat_id, "4242")
        for field in ("chat_id", "target", "kind", "status", "error", "telegram_message_id"):
            self.assertNotIn(code, str(getattr(delivery, field)))

    def test_a_group_chat_never_receives_a_code(self):
        link(self.student, -100999, chat_type="supergroup")
        otp = otp_service.issue(self.student, OTPPurpose.LOGIN, channel=OTPChannel.TELEGRAM)
        self.assertEqual(otp.channel, OTPChannel.EMAIL)
        self.assertEqual(self.fake.sent(), [])
        self.assertEqual(len(mail.outbox), 1)
        self.assertNotIn(OTPChannel.TELEGRAM, otp_service.available_channels(self.student))

    def test_a_legacy_link_with_a_negative_id_is_not_treated_as_private(self):
        link(self.student, -4001, chat_type="")
        self.assertIsNone(otp_service.telegram_target(self.student))
        link(self.student, 4001, chat_type="")
        self.assertIsNotNone(otp_service.telegram_target(self.student))

    def test_a_blocked_chat_falls_back_to_email(self):
        account = link(self.student, 4343)
        account.blocked_at = timezone.now()
        account.save(update_fields=["blocked_at"])
        otp = otp_service.issue(self.student, OTPPurpose.LOGIN, channel=OTPChannel.TELEGRAM)
        self.assertEqual(otp.channel, OTPChannel.EMAIL)
        self.assertEqual(self.fake.sent(), [])
        self.assertEqual(len(mail.outbox), 1)

    def test_a_403_marks_the_account_blocked_and_the_code_goes_by_email(self):
        link(self.student, 4444)
        self.fake.queue("sendMessage", BLOCKED)
        otp = otp_service.issue(self.student, OTPPurpose.LOGIN, channel=OTPChannel.TELEGRAM)
        self.assertEqual(otp.channel, OTPChannel.EMAIL)
        self.assertEqual(len(mail.outbox), 1)
        account = TelegramAccount.objects.get(user=self.student)
        self.assertIsNotNone(account.blocked_at)
        self.assertIn("blocked", account.last_error)
        self.assertEqual(TelegramDelivery.objects.get().status, TelegramDeliveryStatus.BLOCKED)
        # And the next code does not even try Telegram.
        otp_service.issue(self.student, OTPPurpose.LOGIN, channel=OTPChannel.TELEGRAM)
        self.assertEqual(len(self.fake.sent()), 1)

    def test_no_email_and_no_reachable_chat_refuses_rather_than_misdirects(self):
        lonely = make_user("lonely", email="")
        link(lonely, -500, chat_type="group")
        with self.assertRaises(otp_service.OTPError):
            otp_service.issue(lonely, OTPPurpose.LOGIN, channel=OTPChannel.TELEGRAM)
        self.assertEqual(self.fake.sent(), [])


# ---------------------------------------------------------------------------
# The delivery wrapper
# ---------------------------------------------------------------------------
class DeliveryTests(TelegramTestCase):
    def test_notify_user_keeps_its_signature_and_takes_a_kind(self):
        link(self.student, 6001)
        ok = telegram_service.notify_user(self.student, "<b>Paid</b>",
                                          buttons=[("Receipt", "https://ai.example.com/r/1/")], kind="payment")
        self.assertTrue(ok)
        payload = self.fake.sent()[-1]
        self.assertEqual(payload["reply_markup"]["inline_keyboard"][0][0]["url"], "https://ai.example.com/r/1/")
        delivery = TelegramDelivery.objects.get()
        self.assertEqual((delivery.kind, delivery.status, delivery.user), ("payment", "sent", self.student))
        self.assertIsNotNone(TelegramAccount.objects.get(user=self.student).last_delivery_at)
        # The old call shape still works.
        self.assertTrue(telegram_service.notify_user(self.student, "hello"))

    def test_notify_user_never_raises(self):
        link(self.student, 6002)
        self.fake.queue("sendMessage", requests.ConnectionError(f"https://api.telegram.org/bot{TOKEN}/sendMessage"))
        self.assertFalse(telegram_service.notify_user(self.student, "x"))
        delivery = TelegramDelivery.objects.get()
        self.assertEqual(delivery.status, TelegramDeliveryStatus.FAILED)
        self.assertNotIn(TOKEN, delivery.error, "the bot token must never be logged")

    def test_a_deactivated_account_is_marked_blocked(self):
        link(self.student, 6003)
        self.fake.queue("sendMessage", DEACTIVATED)
        self.assertFalse(telegram_service.notify_user(self.student, "x"))
        self.assertIsNotNone(TelegramAccount.objects.get(user=self.student).blocked_at)
        # Blocked accounts are skipped, not retried.
        self.assertFalse(telegram_service.notify_user(self.student, "y"))
        self.assertEqual(len(self.fake.sent()), 1)
        self.assertEqual(TelegramDelivery.objects.filter(status="skipped").count(), 1)

    def test_a_short_rate_limit_is_waited_out_once(self):
        link(self.student, 6004)
        self.fake.queue("sendMessage", rate_limited(2))
        self.assertTrue(telegram_service.notify_user(self.student, "x"))
        self.sleep.assert_called_once_with(2)
        self.assertEqual(len(self.fake.sent()), 2)

    def test_a_long_rate_limit_is_a_failure_not_a_stall(self):
        link(self.student, 6005)
        self.fake.queue("sendMessage", rate_limited(40))
        self.assertFalse(telegram_service.notify_user(self.student, "x"))
        self.sleep.assert_not_called()
        self.assertEqual(TelegramDelivery.objects.get().status, TelegramDeliveryStatus.FAILED)

    def test_a_group_link_gets_nothing_private(self):
        link(self.student, -100777, chat_type="supergroup")
        self.assertFalse(telegram_service.notify_user(self.student, "balance: 5"))
        self.assertEqual(self.fake.sent(), [])

    def test_local_site_urls_do_not_become_buttons(self):
        link(self.student, 6006)
        telegram_service.notify_user(self.student, "x", buttons=[("Open", "http://127.0.0.1:8000/chat/")])
        self.assertNotIn("reply_markup", self.fake.sent()[-1])

    def test_ticket_replies_are_pushed_to_telegram(self):
        link(self.student, 6007)
        ticket = support_service.create_ticket(self.student, data={
            "category": "other", "subject": "Wrong answer", "description": "It said 2+2=5",
        })
        admin = User.objects.create_superuser(username="root", email="root@test.local", password=PASSWORD)
        support_service.add_reply(ticket, admin, "Thanks — fixed.", from_staff=True)
        texts = self.texts_to(6007)
        self.assertTrue(any(f"Reply on ticket {ticket.reference}" in text for text in texts))

    def test_the_students_switch_turns_pushes_off(self):
        link(self.student, 6008)
        NotificationPreference.objects.update_or_create(user=self.student, defaults={"telegram_enabled": False})
        notification_service.notify(self.student, "Hi", "There", telegram=True)
        self.assertEqual(self.fake.sent(), [])


# ---------------------------------------------------------------------------
# Bot commands and membership updates
# ---------------------------------------------------------------------------
class BotCommandTests(TelegramTestCase):
    def test_blocking_and_unblocking_via_my_chat_member(self):
        link(self.student, 7001)

        def member(status):
            return {"update_id": 2, "my_chat_member": {
                "chat": private_chat(7001), "from": {"id": 7001},
                "old_chat_member": {"status": "member"}, "new_chat_member": {"status": status, "user": {"id": 999}},
            }}

        self.assertEqual(telegram_service.handle_update(member("kicked")), "blocked")
        self.assertIsNotNone(TelegramAccount.objects.get(user=self.student).blocked_at)
        self.assertIsNone(otp_service.telegram_target(self.student))
        self.assertEqual(telegram_service.handle_update(member("member")), "unblocked")
        self.assertIsNone(TelegramAccount.objects.get(user=self.student).blocked_at)

    def test_status_lists_each_models_balance_and_links_to_the_chat(self):
        fund(self.student, 10_000)
        link(self.student, 7002)
        self.assertEqual(telegram_service.handle_update(message("/status", private_chat(7002))), "status")
        payload = self.fake.sent()[-1]
        self.assertIn(model_for(GPT).name, payload["text"])
        self.assertIn("10,000", payload["text"])
        urls = [row[0]["url"] for row in payload["reply_markup"]["inline_keyboard"]]
        self.assertIn("https://ai.example.com/chat/", urls)
        self.assertNotIn("dashboard", " ".join(urls))

    def test_status_is_private_only(self):
        link(self.student, 7003)
        group = {"id": -100321, "type": "supergroup", "title": "Class"}
        self.assertEqual(telegram_service.handle_update(message("/status", group)), "not_private")
        self.assertNotIn("tokens", self.texts_to(-100321)[-1])

    def test_status_for_an_unlinked_chat(self):
        self.assertEqual(telegram_service.handle_update(message("/status", private_chat(7004))), "unlinked")

    def test_unlink_asks_first_then_disconnects(self):
        link(self.student, 7005)
        self.assertEqual(telegram_service.handle_update(message("/unlink", private_chat(7005))), "unlink_confirm")
        self.assertTrue(TelegramAccount.objects.get(user=self.student).is_linked)
        self.assertIn("/unlink confirm", self.texts_to(7005)[-1])

        self.assertEqual(telegram_service.handle_update(message("/unlink confirm", private_chat(7005))),
                         "unlinked_now")
        account = TelegramAccount.objects.get(user=self.student)
        self.assertFalse(account.is_linked)
        self.assertIsNone(account.telegram_id)
        self.assertIn("Disconnected", self.texts_to(7005)[-1])

    def test_unlinking_the_only_place_codes_go_turns_sign_in_codes_off(self):
        lonely = make_user("lonely", email="", login_otp_enabled=True)
        link(lonely, 7006)
        telegram_service.handle_update(message("/unlink confirm", private_chat(7006)))
        lonely.refresh_from_db()
        self.assertFalse(lonely.login_otp_enabled)

    def test_help_lists_the_commands(self):
        telegram_service.handle_update(message("/help", private_chat(7007)))
        text = self.texts_to(7007)[-1]
        for command in ("/start", "/status", "/help", "/unlink"):
            self.assertIn(command, text)

    def test_a_channel_promotion_is_remembered_for_the_console(self):
        telegram_service.handle_update({"update_id": 3, "my_chat_member": {
            "chat": {"id": -1009876, "type": "channel", "title": "AI Assistant Helps"},
            "from": {"id": 1}, "old_chat_member": {"status": "left"},
            "new_chat_member": {"status": "administrator", "can_post_messages": True},
        }})
        detected = telegram_service.detected_channel()
        self.assertEqual(detected["id"], "-1009876")
        self.assertTrue(detected["can_post"])

    def test_webhook_and_polling_ask_for_membership_updates(self):
        telegram_service.set_webhook("https://ai.example.com/webhooks/telegram/")
        telegram_service.get_updates(offset=5, timeout=1)
        hook = self.fake.sent("setWebhook")[-1]
        poll = self.fake.sent("getUpdates")[-1]
        for payload in (hook, poll):
            self.assertIn("my_chat_member", payload["allowed_updates"])
            self.assertIn("message", payload["allowed_updates"])
        self.assertEqual(hook["secret_token"], "s3cret-token")


# ---------------------------------------------------------------------------
# Announcements: channel post, private fan-out, retraction
# ---------------------------------------------------------------------------
class AnnouncementTelegramTests(TelegramTestCase):
    def announce(self, **extra) -> Announcement:
        defaults = {"title": "Exam week <tips>", "body": "Sleep & revise.", "level": NotificationLevel.INFO,
                    "audience": AnnouncementAudience.ALL, "action_label": "See plans",
                    "action_url": "/app/pricing/"}
        defaults.update(extra)
        return Announcement.objects.create(**defaults)

    def test_posting_to_the_channel_is_recorded_on_the_announcement(self):
        announcement = self.announce(telegram_channel=True)
        count = notification_service.publish_announcement(announcement)
        self.assertEqual(count, 1)  # the in-app fan-out still happened
        post = self.fake.sent()[-1]
        self.assertEqual(post["chat_id"], "@aiassistanthelps")
        self.assertIn("<b>Exam week &lt;tips&gt;</b>", post["text"])
        self.assertIn("Sleep &amp; revise.", post["text"])
        self.assertEqual(post["reply_markup"]["inline_keyboard"][0][0],
                         {"text": "See plans", "url": "https://ai.example.com/app/pricing/"})
        announcement.refresh_from_db()
        self.assertEqual(announcement.channel_status, "sent")
        self.assertEqual(announcement.channel_message_id, str(self.fake.message_id))
        self.assertEqual(TelegramDelivery.objects.get(target="channel").kind, "announcement")

    def test_a_failed_channel_post_is_recorded_and_the_announcement_still_lands(self):
        self.fake.queue("sendMessage", CHAT_NOT_FOUND)
        announcement = self.announce(telegram_channel=True)
        self.assertEqual(notification_service.publish_announcement(announcement), 1)
        announcement.refresh_from_db()
        self.assertEqual(announcement.channel_status, "failed")
        self.assertIn("chat not found", announcement.channel_error)
        self.assertTrue(Notification.objects.filter(announcement=announcement, user=self.student).exists())

    def test_no_channel_post_when_it_was_not_asked_for(self):
        notification_service.publish_announcement(self.announce(telegram_channel=False))
        self.assertEqual(self.fake.sent(), [])

    def test_private_fan_out_reaches_only_those_who_can_and_want_it(self):
        wanted = self.student
        link(wanted, 8001)
        quiet = make_user("quiet")
        link(quiet, 8002)
        NotificationPreference.objects.update_or_create(user=quiet, defaults={"telegram_enabled": False})
        no_news = make_user("nonews")
        link(no_news, 8003)
        NotificationPreference.objects.update_or_create(user=no_news, defaults={"announcements": False})
        blocked = make_user("blocked")
        account = link(blocked, 8004)
        account.blocked_at = timezone.now()
        account.save(update_fields=["blocked_at"])
        grouped = make_user("grouped")
        link(grouped, -1008005, chat_type="supergroup")
        make_user("unlinked")

        announcement = self.announce(telegram_channel=False, telegram_private=True)
        with patch("AI.services.notification_service._spawn", side_effect=lambda fn, *args: fn(*args)), \
                patch("AI.services.notification_service.time.sleep") as pace, \
                self.captureOnCommitCallbacks(execute=True):
            notification_service.publish_announcement(announcement)
        self.assertEqual(announcement.telegram_queued, 1)
        self.assertEqual([p["chat_id"] for p in self.fake.sent()], ["8001"])
        self.assertIn("Exam week", self.fake.sent()[0]["text"])
        self.assertEqual(pace.call_count, 1)
        self.assertEqual(TelegramDelivery.objects.get(kind="announcement").user, wanted)

    def test_the_fan_out_rechecks_each_student_at_send_time(self):
        link(self.student, 8101)
        announcement = self.announce(telegram_private=True)
        # Queued, then the student blocks the bot before their turn comes.
        TelegramAccount.objects.filter(user=self.student).update(blocked_at=timezone.now())
        counts = notification_service.send_private_announcement(announcement.pk, [self.student.pk])
        self.assertEqual(counts["skipped"], 1)
        self.assertEqual(self.fake.sent(), [])

    def test_retracting_deletes_the_channel_post(self):
        announcement = self.announce(telegram_channel=True)
        notification_service.publish_announcement(announcement)
        announcement.refresh_from_db()
        notification_service.retract_announcement(announcement)
        delete = self.fake.sent("deleteMessage")[-1]
        self.assertEqual(delete, {"chat_id": "@aiassistanthelps", "message_id": int(announcement.channel_message_id)})
        announcement.refresh_from_db()
        self.assertEqual(announcement.channel_status, "deleted")
        self.assertIsNotNone(announcement.retracted_at)

    def test_a_post_that_cannot_be_deleted_does_not_stop_the_retraction(self):
        announcement = self.announce(telegram_channel=True)
        notification_service.publish_announcement(announcement)
        self.fake.queue("deleteMessage", {"ok": False, "error_code": 400,
                                          "description": "Bad Request: message can't be deleted"})
        removed = notification_service.retract_announcement(announcement)
        self.assertEqual(removed, 1)
        announcement.refresh_from_db()
        self.assertEqual(announcement.channel_status, "sent")
        self.assertIn("can't be deleted", announcement.channel_error)
        self.assertFalse(Notification.objects.filter(announcement=announcement).exists())


class AnnouncementPageTests(TelegramTestCase):
    def setUp(self):
        super().setUp()
        self.admin = User.objects.create_superuser(username="root", email="root@test.local", password=PASSWORD)
        self.client.force_login(self.admin)
        self.url = reverse("console:notifications")

    def test_the_compose_form_offers_both_switches(self):
        page = self.client.get(self.url)
        self.assertContains(page, 'name="telegram_channel"')
        self.assertContains(page, 'name="telegram_private"')
        self.assertContains(page, "Post to the Telegram channel")
        self.assertContains(page, "Also send to students' Telegram chats")

    @override_settings(TELEGRAM_CHANNEL_ID="")
    def test_the_channel_switch_is_disabled_without_a_channel(self):
        page = self.client.get(self.url).content.decode()
        switch = re.search(r'<input[^>]*name="telegram_channel"[^>]*>', page, re.S).group(0)
        self.assertIn("disabled", switch)
        self.assertIn("Telegram setup", page)
        # Even if the box is forced on, nothing is posted.
        self.client.post(self.url, {"title": "T", "body": "B", "level": "info", "audience": "all",
                                    "telegram_channel": "on"})
        self.assertFalse(Announcement.objects.get().telegram_channel)
        self.assertEqual(self.fake.sent(), [])

    def test_publishing_with_the_channel_switch_posts_and_shows_the_status(self):
        self.client.post(self.url, {"title": "Maintenance", "body": "Sunday 02:00", "level": "warning",
                                    "audience": "all", "telegram_channel": "on"})
        announcement = Announcement.objects.get()
        self.assertTrue(announcement.telegram_channel)
        self.assertEqual(announcement.channel_status, "sent")
        page = self.client.get(self.url)
        self.assertIn("Posted to the Telegram channel", page.context["toast"])
        self.assertContains(page, "TELEGRAM &middot; SENT")

    def test_a_failed_post_shows_its_error_on_the_row(self):
        self.fake.queue("sendMessage", {"ok": False, "error_code": 403,
                                        "description": "Forbidden: bot is not a member of the channel chat"})
        self.client.post(self.url, {"title": "X", "body": "Y", "level": "info", "audience": "all",
                                    "telegram_channel": "on"})
        page = self.client.get(self.url)
        self.assertContains(page, "TELEGRAM &middot; FAILED")
        self.assertContains(page, "bot is not a member of the channel chat")

    def test_retract_from_the_page_reports_the_channel_deletion(self):
        self.client.post(self.url, {"title": "X", "body": "Y", "level": "info", "audience": "all",
                                    "telegram_channel": "on"})
        announcement = Announcement.objects.get()
        self.client.post(reverse("console:announcement_action", args=[announcement.pk]), {"action": "retract"})
        page = self.client.get(self.url)
        self.assertIn("Telegram channel post was deleted", page.context["toast"])


# ---------------------------------------------------------------------------
# Sign-in codes (login OTP)
# ---------------------------------------------------------------------------
class LoginCodeTests(TelegramTestCase):
    def setUp(self):
        super().setUp()
        self.student.login_otp_enabled = True
        self.student.save(update_fields=["login_otp_enabled"])

    def sign_in(self, identifier="sokha"):
        return self.client.post("/login/", {"identifier": identifier, "password": PASSWORD})

    def signed_in(self) -> bool:
        return "_auth_user_id" in self.client.session

    def email_code(self) -> str:
        return re.search(r"\b(\d{6})\b", mail.outbox[-1].body).group(1)

    def test_password_alone_is_not_enough(self):
        response = self.sign_in()
        self.assertRedirects(response, "/otp/", fetch_redirect_response=False)
        self.assertFalse(self.signed_in())
        self.assertEqual(OTPCode.objects.get().purpose, OTPPurpose.LOGIN)
        self.assertEqual(len(mail.outbox), 1)  # no Telegram linked: by e-mail
        page = self.client.get("/otp/")
        self.assertContains(page, "Enter your sign-in code")

    def test_a_wrong_code_is_refused_and_the_right_one_signs_in(self):
        self.sign_in()
        code = self.email_code()
        wrong = "000000" if code != "000000" else "111111"
        self.client.post("/otp/", {"code": wrong})
        self.assertFalse(self.signed_in())

        response = self.client.post("/otp/", {"code": code})
        self.assertRedirects(response, reverse("student:chat"), fetch_redirect_response=False)
        self.assertTrue(self.signed_in())
        self.student.refresh_from_db()
        # The single-session slot belongs to this session.
        self.assertEqual(self.student.active_session_key, self.client.session.session_key)

    def test_a_linked_private_chat_gets_the_code_instead_of_email(self):
        settings_service.set_value("otp_send_everywhere", False)
        link(self.student, 9001)
        self.sign_in()
        self.assertEqual(len(mail.outbox), 0)
        code = re.search(r"<code>(\d+)</code>", self.texts_to(9001)[-1]).group(1)
        self.assertIn("Someone just entered your password", self.texts_to(9001)[-1])
        page = self.client.get("/otp/")
        self.assertContains(page, "your Telegram chat with our bot")
        self.assertContains(page, "Send it by e-mail instead")
        self.client.post("/otp/", {"code": code})
        self.assertTrue(self.signed_in())

    def test_send_by_email_instead(self):
        settings_service.set_value("otp_send_everywhere", False)
        link(self.student, 9002)
        self.sign_in()
        telegram_code = re.search(r"<code>(\d+)</code>", self.texts_to(9002)[-1]).group(1)
        self.client.post("/otp/resend/", {"channel": "email"})
        self.assertEqual(len(mail.outbox), 1)
        # The Telegram code is void now; only the newest code works.
        self.client.post("/otp/", {"code": telegram_code})
        self.assertFalse(self.signed_in())
        self.client.post("/otp/", {"code": self.email_code()})
        self.assertTrue(self.signed_in())

    def test_a_blocked_chat_sends_the_code_by_email(self):
        account = link(self.student, 9003)
        account.blocked_at = timezone.now()
        account.save(update_fields=["blocked_at"])
        self.sign_in()
        self.assertEqual(self.fake.sent(), [])
        self.assertEqual(len(mail.outbox), 1)
        self.assertContains(self.client.get("/otp/"), "You blocked our Telegram bot")

    def test_accounts_without_the_setting_sign_in_as_before(self):
        plain = make_user("plain")
        response = self.sign_in("plain")
        self.assertRedirects(response, reverse("student:chat"), fetch_redirect_response=False)
        self.assertTrue(self.signed_in())
        self.assertFalse(OTPCode.objects.filter(user=plain).exists())

    def test_administrators_are_asked_only_when_they_opted_in(self):
        admin = User.objects.create_superuser(username="boss", email="boss@test.local", password=PASSWORD)
        response = self.client.post("/admin/login/", {"identifier": "boss", "password": PASSWORD})
        self.assertTrue(self.signed_in())
        self.client.logout()

        admin.login_otp_enabled = True
        admin.save(update_fields=["login_otp_enabled"])
        response = self.client.post("/admin/login/", {"identifier": "boss", "password": PASSWORD})
        self.assertRedirects(response, "/otp/", fetch_redirect_response=False)
        self.assertFalse(self.signed_in())
        response = self.client.post("/otp/", {"code": self.email_code()})
        self.assertRedirects(response, reverse("console:dashboard"), fetch_redirect_response=False)
        self.assertTrue(self.signed_in())

    def test_a_password_reset_page_never_offers_a_telegram_link(self):
        self.client.post("/forgot-password/", {"identifier": "sokha"})
        page = self.client.get("/otp/").content.decode()
        self.assertNotIn("t.me/epc_test_bot?start=", page)
        self.assertIsNone(TelegramAccount.objects.filter(user=self.student).values_list(
            "link_token_expires_at", flat=True).first())


# ---------------------------------------------------------------------------
# One code to Telegram and e-mail together (the default)
# ---------------------------------------------------------------------------
class CodesEverywhereTests(TelegramTestCase):
    def setUp(self):
        super().setUp()
        self.student.login_otp_enabled = True
        self.student.save(update_fields=["login_otp_enabled"])

    sign_in = LoginCodeTests.sign_in
    signed_in = LoginCodeTests.signed_in
    email_code = LoginCodeTests.email_code

    def telegram_code(self, chat_id) -> str:
        return re.search(r"<code>(\d+)</code>", self.texts_to(chat_id)[-1]).group(1)

    def test_one_code_reaches_telegram_and_email(self):
        link(self.student, 9101)
        self.sign_in()
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(self.telegram_code(9101), self.email_code())
        otp = OTPCode.objects.get()
        self.assertEqual(otp.channel, OTPChannel.BOTH)
        self.assertIn("@sok", otp.destination)
        self.assertIn("@test.local", otp.destination)

        page = self.client.get("/otp/")
        self.assertContains(page, "Use whichever arrives first")
        self.assertContains(page, "Send a new code")
        self.assertNotContains(page, "instead")

    def test_either_copy_signs_in(self):
        link(self.student, 9102)
        self.sign_in()
        self.client.post("/otp/", {"code": self.telegram_code(9102)})
        self.assertTrue(self.signed_in())

    def test_resend_goes_to_both_and_voids_the_old_code(self):
        link(self.student, 9103)
        self.sign_in()
        old = self.email_code()
        self.client.post("/otp/resend/", {"channel": "email"})
        self.assertEqual(len(mail.outbox), 2)
        self.assertEqual(len(self.texts_to(9103)), 2)
        self.assertEqual(self.telegram_code(9103), self.email_code())
        if old != self.email_code():
            self.client.post("/otp/", {"code": old})
            self.assertFalse(self.signed_in())
        self.client.post("/otp/", {"code": self.email_code()})
        self.assertTrue(self.signed_in())

    def test_a_blocked_bot_still_leaves_the_email_copy(self):
        link(self.student, 9104)
        self.fake.queue("sendMessage", BLOCKED)
        self.sign_in()
        otp = OTPCode.objects.get()
        self.assertEqual(otp.channel, OTPChannel.EMAIL)
        self.assertNotIn("@sok", otp.destination)
        self.client.post("/otp/", {"code": self.email_code()})
        self.assertTrue(self.signed_in())

    def test_a_mail_failure_still_leaves_the_telegram_copy(self):
        link(self.student, 9105)
        with patch("AI.services.otp_service._send_email", side_effect=OSError("smtp down")):
            self.sign_in()
        otp = OTPCode.objects.get()
        self.assertEqual(otp.channel, OTPChannel.TELEGRAM)
        self.client.post("/otp/", {"code": self.telegram_code(9105)})
        self.assertTrue(self.signed_in())

    def test_an_unreachable_telegram_costs_seconds_not_the_code(self):
        link(self.student, 9109)
        self.fake.queue("sendMessage", requests.ConnectTimeout("stalled"))
        self.sign_in()
        sends = [c for c in self.post.call_args_list if c.args[0].endswith("/sendMessage")]
        self.assertEqual(sends[-1].kwargs["timeout"], otp_service.OTP_TELEGRAM_TIMEOUT)
        self.assertEqual(OTPCode.objects.get().channel, OTPChannel.EMAIL)
        self.client.post("/otp/", {"code": self.email_code()})
        self.assertTrue(self.signed_in())

    def test_nothing_delivered_voids_the_code(self):
        link(self.student, 9106)
        self.fake.queue("sendMessage", BLOCKED)
        with patch("AI.services.otp_service._send_email", side_effect=OSError("smtp down")):
            with self.assertRaises(otp_service.OTPError):
                otp_service.issue(self.student, OTPPurpose.LOGIN)
        self.assertTrue(OTPCode.objects.get().is_expired)

    def test_without_telegram_it_is_email_only(self):
        self.sign_in()
        self.assertEqual(OTPCode.objects.get().channel, OTPChannel.EMAIL)
        self.assertEqual(self.fake.sent(), [])

    def test_a_reset_code_goes_to_both(self):
        link(self.student, 9107)
        self.client.post("/forgot-password/", {"identifier": "sokha"})
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(self.telegram_code(9107), self.email_code())
        self.assertIn("reset your password", self.texts_to(9107)[-1])

    def test_a_signup_code_stays_on_email(self):
        link(self.student, 9108)
        otp = otp_service.issue(self.student, OTPPurpose.SIGNUP, channel=OTPChannel.EMAIL)
        self.assertEqual(otp.channel, OTPChannel.EMAIL)
        self.assertEqual(self.fake.sent(), [])


# ---------------------------------------------------------------------------
# A brand-new student who wants the verification code on Telegram
# ---------------------------------------------------------------------------
class NewStudentTelegramCodeTests(TelegramTestCase):
    def test_start_the_bot_then_send_the_code_there(self):
        # Through the real sign-up form: the account is *pending* until the
        # code is entered, and linking must still be allowed then.
        self.client.post("/signup/", {"full_name": "New Bie", "email": "newbie@test.local",
                                      "username": "newbie", "password": PASSWORD, "accept_terms": "on"})
        newbie = User.objects.get(username="newbie")
        self.assertEqual(newbie.status, AccountStatus.PENDING)
        page = self.client.get("/otp/").content.decode()
        token = re.search(r"t\.me/epc_test_bot\?start=([\w-]+)", page).group(1)
        self.assertIn("I've pressed Start", page)
        # For networks that block t.me: the installed app directly, or a phone.
        self.assertIn(f"tg://resolve?domain=epc_test_bot&amp;start={token}", page)
        self.assertIn('aria-label="QR code that opens our Telegram bot"', page)

        # The student presses Start in Telegram, and comes back.
        self.assertEqual(telegram_service.handle_update(message(f"/start {token}", private_chat(7700))), "linked")
        self.assertIn("Go back to the sign-up page", self.texts_to(7700)[-1])
        self.assertNotIn("disabled", self.texts_to(7700)[-1])
        page = self.client.get("/otp/").content.decode()
        self.assertIn("Send the code to Telegram instead", page)
        self.assertNotIn("Start our Telegram bot", page)

        self.client.post("/otp/resend/", {"channel": "telegram"})
        code = re.search(r"<code>(\d+)</code>", self.texts_to(7700)[-1]).group(1)
        self.client.post("/otp/", {"code": code})
        newbie.refresh_from_db()
        self.assertTrue(newbie.is_verified)
        self.assertEqual(newbie.status, AccountStatus.ACTIVE)
        self.assertIn("_auth_user_id", self.client.session)

    def test_a_disabled_account_still_cannot_link(self):
        user = make_user("gone", status=AccountStatus.DISABLED)
        account = telegram_service.issue_link_token(user)
        self.assertEqual(telegram_service.handle_update(
            message(f"/start {account.link_token}", private_chat(7701))), "disabled")
        self.assertIn("disabled", self.texts_to(7701)[-1])
        self.assertFalse(TelegramAccount.objects.get(user=user).is_linked)


# ---------------------------------------------------------------------------
# The administrator requires a sign-in code from every student
# ---------------------------------------------------------------------------
class RequiredForEveryoneTests(TelegramTestCase):
    def setUp(self):
        super().setUp()
        settings_service.set_value("login_otp_students_required", True)

    def sign_in(self, identifier="sokha"):
        return self.client.post("/login/", {"identifier": identifier, "password": PASSWORD})

    def signed_in(self) -> bool:
        return "_auth_user_id" in self.client.session

    def test_off_by_default(self):
        settings_service.set_value("login_otp_students_required", False)
        self.sign_in()
        self.assertTrue(self.signed_in())

    def test_a_student_who_never_opted_in_needs_a_code(self):
        self.assertFalse(self.student.login_otp_enabled)
        response = self.sign_in()
        self.assertRedirects(response, "/otp/", fetch_redirect_response=False)
        self.assertFalse(self.signed_in())
        self.assertContains(self.client.get("/otp/"), "Every sign-in needs a one-time code")
        code = re.search(r"\b(\d{6})\b", mail.outbox[-1].body).group(1)
        self.client.post("/otp/", {"code": code})
        self.assertTrue(self.signed_in())

    def test_administrators_still_choose_for_themselves(self):
        User.objects.create_superuser(username="boss", email="boss@test.local", password=PASSWORD)
        self.client.post("/admin/login/", {"identifier": "boss", "password": PASSWORD})
        self.assertTrue(self.signed_in())

    def test_a_student_with_nowhere_to_send_a_code_is_not_locked_out(self):
        make_user("nomail", email="")
        self.sign_in("nomail")
        self.assertTrue(self.signed_in())

    def test_the_students_own_switch_is_locked_on(self):
        self.client.force_login(self.student)
        page = self.client.get("/app/settings/", HTTP_X_APP_PANEL="1").content.decode()
        box = re.search(r'<input[^>]*name="login_otp_enabled"[^>]*>', page, re.S).group(0)
        self.assertIn("checked", box)
        self.assertIn("disabled", box)
        self.assertIn("Required for every student", page)

        response = self.client.post("/app/settings/", {"action": "security"}, HTTP_X_APP_PANEL="1")
        self.assertContains(response, "required for every student")
        self.student.refresh_from_db()
        self.assertFalse(self.student.login_otp_enabled)  # the rule, not the student's flag
        self.client.logout()
        self.assertRedirects(self.sign_in(), "/otp/", fetch_redirect_response=False)

    def test_the_console_shows_and_counts_it(self):
        boss = User.objects.create_superuser(username="boss", email="boss@test.local", password=PASSWORD)
        self.client.force_login(boss)
        page = self.client.get(reverse("console:settings"))
        self.assertContains(page, "Sign-in code for every student")
        self.assertContains(page, "Send codes to Telegram and e-mail")
        page = self.client.get(reverse("console:telegram"))
        students = User.objects.filter(role=Role.STUDENT, is_superuser=False).count()
        self.assertEqual(page.context["counts"]["login_otp"], students)


# ---------------------------------------------------------------------------
# Settings → Telegram and Security
# ---------------------------------------------------------------------------
class SettingsCardTests(TelegramTestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.student)

    def act(self, action: str, **data):
        return self.client.post("/app/settings/", {"action": action, **data}, HTTP_X_APP_PANEL="1")

    def test_the_channel_link_carries_the_channels_real_title(self):
        telegram_service.channel_status()  # what the console's check does
        page = self.client.get("/app/settings/", HTTP_X_APP_PANEL="1")
        self.assertContains(page, "Join our channel — AI Assistant Helps")
        help_page = self.client.get("/app/help/", HTTP_X_APP_PANEL="1")
        self.assertContains(help_page, "AI Assistant Helps")

        # Point the site at another channel: the old title must not follow.
        with override_settings(TELEGRAM_CHANNEL_ID="-1009999"):
            page = self.client.get("/app/settings/", HTTP_X_APP_PANEL="1")
            self.assertNotContains(page, "AI Assistant Helps")
            self.assertContains(page, "Join our channel</b>")

    def test_a_detected_chat_only_names_the_configured_channel(self):
        telegram_service.handle_update({"update_id": 1, "my_chat_member": {
            "chat": {"id": -1005555, "type": "channel", "title": "Someone else's channel"},
            "new_chat_member": {"status": "administrator", "can_post_messages": True}}})
        self.assertEqual(telegram_service.channel_title(), "")
        telegram_service.handle_update({"update_id": 2, "my_chat_member": {
            "chat": {"id": -1001234567890, "type": "channel", "title": "Renamed Helps",
                     "username": "aiassistanthelps"},
            "new_chat_member": {"status": "administrator", "can_post_messages": True}}})
        self.assertEqual(telegram_service.channel_title(), "Renamed Helps")

    def test_connect_shows_a_live_single_use_link_and_qr(self):
        page = self.client.get("/app/settings/", HTTP_X_APP_PANEL="1")
        self.assertContains(page, "Connect Telegram")
        # Never checked yet, so no name rather than a made-up one.
        self.assertContains(page, "Join our channel</b>")

        page = self.act("telegram_connect")
        account = TelegramAccount.objects.get(user=self.student)
        self.assertTrue(telegram_service.link_is_valid(account))
        self.assertContains(page, f"https://t.me/epc_test_bot?start={account.link_token}")
        self.assertContains(page, "<svg")
        self.assertContains(page, "I've pressed Start")
        self.assertContains(page, "Works once")

    def test_refresh_reports_whether_start_was_pressed(self):
        account = telegram_service.issue_link_token(self.student)
        self.assertContains(self.act("telegram_check"), "Not connected yet")
        telegram_service.handle_update(message(f"/start {account.link_token}", private_chat(1201)))
        page = self.act("telegram_check")
        self.assertContains(page, "Connected to Telegram as @sok")
        self.assertContains(page, "Send a test message")
        self.assertContains(page, "Connected since")

    def test_a_test_message_goes_to_the_linked_chat(self):
        link(self.student, 1202)
        self.assertContains(self.act("telegram_test"), "Test message sent")
        self.assertIn("Test message", self.texts_to(1202)[-1])
        self.assertEqual(TelegramDelivery.objects.get().kind, "test")

    def test_blocked_state_is_explained(self):
        account = link(self.student, 1203)
        account.blocked_at = timezone.now()
        account.save(update_fields=["blocked_at"])
        page = self.client.get("/app/settings/", HTTP_X_APP_PANEL="1")
        self.assertContains(page, "You blocked the bot")
        self.assertContains(page, "https://t.me/epc_test_bot")

    def test_disconnect(self):
        link(self.student, 1204)
        page = self.act("telegram_unlink")
        self.assertContains(page, "Telegram has been disconnected")
        self.assertFalse(TelegramAccount.objects.get(user=self.student).is_linked)
        self.assertIn("Disconnected", self.texts_to(1204)[-1])

    def test_the_two_telegram_switches(self):
        link(self.student, 1205)
        self.act("telegram_prefs", announcements="on")
        prefs = NotificationPreference.objects.get(user=self.student)
        self.assertFalse(prefs.telegram_enabled)
        self.assertTrue(prefs.announcements)

    def test_sign_in_codes_need_somewhere_to_go(self):
        self.student.email = ""
        self.student.save(update_fields=["email"])
        page = self.act("security", login_otp_enabled="on")
        self.assertContains(page, "Sign-in codes need somewhere to go")
        self.student.refresh_from_db()
        self.assertFalse(self.student.login_otp_enabled)

        link(self.student, 1206)
        self.assertContains(self.act("security", login_otp_enabled="on"), "Sign-in codes are on")
        self.student.refresh_from_db()
        self.assertTrue(self.student.login_otp_enabled)
        self.assertContains(self.act("security"), "Sign-in codes are off")

    def test_the_settings_page_has_no_duplicate_ids(self):
        from AI.tests.test_ui_regressions import duplicate_ids

        telegram_service.issue_link_token(self.student)
        self.assertEqual(duplicate_ids(self.client.get("/app/settings/").content.decode()), [])


# ---------------------------------------------------------------------------
# Console → Telegram
# ---------------------------------------------------------------------------
class ConsolePageTests(TelegramTestCase):
    def setUp(self):
        super().setUp()
        self.admin = User.objects.create_superuser(username="root", email="root@test.local", password=PASSWORD)
        self.client.force_login(self.admin)
        self.url = reverse("console:telegram")
        self.action_url = reverse("console:telegram_action")

    def test_the_page_shows_live_status(self):
        link(self.student, 1301)
        telegram_service.notify_user(self.student, "hello", kind="alert")
        page = self.client.get(self.url)
        self.assertEqual(page.status_code, 200)
        for text in ("@epc_test_bot", "AI Assistant Helps", "Can post", "Setup guide", "BotFather",
                     "TELEGRAM_WEBHOOK_SECRET", "Recent deliveries", "Sokha"):
            self.assertContains(page, text)
        self.assertNotContains(page, TOKEN)
        self.assertNotContains(page, "s3cret-token")
        self.assertEqual(page.context["counts"]["linked"], 1)

    def test_the_nav_links_to_it(self):
        self.assertContains(self.client.get(reverse("console:dashboard")), f'href="{self.url}"')

    @override_settings(TELEGRAM_BOT_TOKEN="", TELEGRAM_CHANNEL_ID="")
    def test_the_page_renders_without_any_configuration(self):
        page = self.client.get(self.url)
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Not configured")
        self.assertEqual(self.fake.calls, [])

    def test_delivery_filters(self):
        TelegramDelivery.objects.create(chat_id="1", kind="otp", status="sent")
        TelegramDelivery.objects.create(chat_id="2", kind="alert", status="failed", error="Bad Request: boom")
        page = self.client.get(self.url, {"status": "failed"})
        self.assertEqual([row.kind for row in page.context["page"].object_list], ["alert"])
        page = self.client.get(self.url, {"kind": "otp"})
        self.assertEqual([row.status for row in page.context["page"].object_list], ["sent"])

    def test_register_webhook(self):
        self.client.post(self.action_url, {"action": "register_webhook"})
        hook = self.fake.sent("setWebhook")[-1]
        self.assertEqual(hook["url"], "https://ai.example.com/webhooks/telegram/")
        self.assertEqual(hook["secret_token"], "s3cret-token")
        self.assertIn("my_chat_member", hook["allowed_updates"])

    @override_settings(SITE_URL="http://127.0.0.1:8000")
    def test_register_webhook_refuses_plain_http(self):
        response = self.client.post(self.action_url, {"action": "register_webhook"}, follow=True)
        self.assertEqual(self.fake.sent("setWebhook"), [])
        self.assertContains(response, "telegram_bot --poll")

    def test_test_post_to_the_channel(self):
        self.client.post(self.action_url, {"action": "test_channel"})
        self.assertEqual(self.fake.sent()[-1]["chat_id"], "@aiassistanthelps")
        self.assertEqual(TelegramDelivery.objects.get().target, "channel")

    def test_test_to_my_own_chat_needs_my_own_link(self):
        response = self.client.post(self.action_url, {"action": "test_me"}, follow=True)
        self.assertContains(response, "Connect your own Telegram")
        link(self.admin, 1302)
        self.client.post(self.action_url, {"action": "test_me"})
        self.assertIn("Test from the", self.texts_to(1302)[-1])

    def test_check_channel_permissions(self):
        self.fake.queue("getChatMember", {"ok": True, "result": {"status": "administrator",
                                                                  "can_post_messages": False}})
        response = self.client.post(self.action_url, {"action": "check"}, follow=True)
        self.assertContains(response, "cannot post")

    def test_connect_my_own_telegram(self):
        self.client.post(self.action_url, {"action": "connect_me"})
        account = TelegramAccount.objects.get(user=self.admin)
        self.assertTrue(telegram_service.link_is_valid(account))
        self.assertContains(self.client.get(self.url), account.link_token)

    def test_students_cannot_open_it(self):
        self.client.force_login(self.student)
        self.assertNotEqual(self.client.get(self.url).status_code, 200)
        self.client.post(self.action_url, {"action": "test_channel"})
        self.assertEqual(self.fake.sent(), [])


# ---------------------------------------------------------------------------
# The webhook
# ---------------------------------------------------------------------------
class WebhookSecretTests(TelegramTestCase):
    url = "/webhooks/telegram/"

    def hit(self, secret=None):
        extra = {"HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN": secret} if secret is not None else {}
        body = json.dumps(message("/help", private_chat(1401)))
        return self.client.post(self.url, data=body, content_type="application/json", **extra)

    @override_settings(DEBUG=False, TELEGRAM_WEBHOOK_SECRET="")
    def test_production_refuses_every_call_without_a_configured_secret(self):
        self.assertEqual(self.hit().status_code, 403)
        self.assertEqual(self.hit("anything").status_code, 403)
        self.assertEqual(self.fake.calls, [])

    @override_settings(DEBUG=False)
    def test_production_requires_the_matching_secret(self):
        self.assertEqual(self.hit().status_code, 403)
        self.assertEqual(self.hit("wrong").status_code, 403)
        self.assertEqual(self.hit("s3cret-token").status_code, 200)
        self.assertEqual(len(self.fake.sent()), 1)

    @override_settings(DEBUG=True, TELEGRAM_WEBHOOK_SECRET="")
    def test_a_debug_machine_may_run_without_one(self):
        self.assertEqual(self.hit().status_code, 200)

    @override_settings(DEBUG=False)
    def test_a_forged_link_update_is_refused(self):
        account = telegram_service.issue_link_token(self.student)
        body = json.dumps(message(f"/start {account.link_token}", private_chat(1402)))
        response = self.client.post(self.url, data=body, content_type="application/json",
                                    HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN="guess")
        self.assertEqual(response.status_code, 403)
        self.assertFalse(TelegramAccount.objects.get(pk=account.pk).is_linked)


# ---------------------------------------------------------------------------
# Payments, plan grants and per-model warnings reach the private chat
# ---------------------------------------------------------------------------
class AccountEventPushTests(TelegramTestCase):
    def setUp(self):
        super().setUp()
        from decimal import Decimal

        from AI.models import Plan

        link(self.student, 7001)
        self.plan = Plan.objects.create(name="Student", tokens=100_000, price=Decimal("10.00"), duration_days=30,
                                        ai_model=model_for(GPT))

    def test_a_confirmed_payment_is_pushed_privately_with_the_charged_amount(self):
        from AI.services import payment_service

        payment = payment_service.create_payment(self.student, self.plan, "acleda_khqr")
        with self.captureOnCommitCallbacks(execute=True):
            payment_service.settle(payment, source="test")
        texts = self.texts_to(7001)
        self.assertTrue(any(payment.reference in text and "៛" in text for text in texts), texts)
        # Never to the public channel.
        self.assertEqual(self.texts_to("@aiassistanthelps"), [])

    def test_an_expired_order_is_pushed_privately(self):
        from AI.services import payment_service

        payment = payment_service.create_payment(self.student, self.plan, "binance")
        with self.captureOnCommitCallbacks(execute=True):
            payment_service.mark_expired(payment, "No payment arrived.")
        self.assertTrue(any("Payment window closed" in text for text in self.texts_to(7001)))
        self.assertEqual(self.texts_to("@aiassistanthelps"), [])

    def test_an_admin_granting_a_plan_is_pushed(self):
        from AI.tests.helpers import fund_pool

        fund_pool(GPT)
        admin = User.objects.create_superuser(username="root", email="root@test.local", password=PASSWORD)
        self.client.force_login(admin)
        self.client.post(f"/admin/users/{self.student.pk}/grant-plan/", {"plan_id": self.plan.pk})
        self.assertTrue(any("Student plan added" in text for text in self.texts_to(7001)))

    def test_a_per_model_low_token_warning_is_also_pushed(self):
        from AI.models import UserAIWallet
        from AI.services import token_alert_service

        fund(self.student, 1_000)
        wallet = UserAIWallet.objects.select_related("ai_model", "user").get(user=self.student)
        token_alert_service.send_warning(wallet, 10, force=True)
        self.assertTrue(any("tokens running low" in text for text in self.texts_to(7001)))

    def test_no_low_token_push_when_the_student_turned_telegram_alerts_off(self):
        from AI.models import UserAIWallet
        from AI.services import token_alert_service

        NotificationPreference.objects.update_or_create(user=self.student, defaults={"telegram_enabled": False})
        fund(self.student, 1_000)
        wallet = UserAIWallet.objects.select_related("ai_model", "user").get(user=self.student)
        token_alert_service.send_warning(wallet, 10, force=True)
        self.assertFalse(any("tokens running low" in text for text in self.texts_to(7001)))
