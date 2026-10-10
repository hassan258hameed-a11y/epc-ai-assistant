"""Telegram, round two: the administrator's alert switches, the community group,
and messages that read cleanly — words and bold, never emoji.

Reuses the Bot API stand-in from ``test_telegram_integration``: nothing here
talks to Telegram.
"""
from __future__ import annotations

import re
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.test import override_settings
from django.utils import timezone

from AI.models import (
    Announcement,
    AnnouncementAudience,
    Notification,
    NotificationCategory,
    NotificationLevel,
    OTPPurpose,
    Payment,
    Plan,
    Subscription,
    TelegramDelivery,
    UserAIWallet,
)
from AI.services import (
    notification_service,
    otp_service,
    payment_notice_service,
    settings_service,
    telegram_service,
    token_alert_service,
)
from AI.tests.test_telegram_integration import (
    CHAT_NOT_FOUND,
    TG,
    TelegramTestCase,
    link,
    make_user,
    message,
    private_chat,
)
from AI.tests.helpers import GPT, model_for

GROUP = dict(TG, TELEGRAM_GROUP_ID="-1009876543210", TELEGRAM_GROUP_URL="https://t.me/+epcgroup")

#: Pictographs and emoji: the blocks emoji live in, plus the variation selector.
EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿⬀-⯿️⌚-⏿]")


def migrated(new_id: int) -> dict:
    return {"ok": False, "error_code": 400, "description": "Bad Request: group chat was upgraded to a supergroup chat",
            "parameters": {"migrate_to_chat_id": new_id}}


# ---------------------------------------------------------------------------
# The administrator decides which alerts reach Telegram
# ---------------------------------------------------------------------------
class AlertSwitchTests(TelegramTestCase):
    def setUp(self):
        super().setUp()
        link(self.student, 7001)

    def alert(self, category=NotificationCategory.SYSTEM, title="Support replied"):
        return notification_service.notify(self.student, title, "Body", category=category, telegram=True)

    def test_everything_goes_by_default(self):
        for category in (NotificationCategory.SYSTEM, NotificationCategory.PAYMENT, NotificationCategory.TOKENS,
                         NotificationCategory.EXPIRY, NotificationCategory.REFERRAL):
            self.alert(category)
        self.assertEqual(len(self.texts_to(7001)), 5)

    def test_the_master_switch_keeps_alerts_in_the_app(self):
        settings_service.set_value("telegram_alerts_enabled", False)
        self.alert()
        self.assertEqual(self.texts_to(7001), [])
        self.assertTrue(Notification.objects.filter(user=self.student, title="Support replied").exists())

    def test_one_category_can_be_switched_off(self):
        settings_service.set_value("telegram_send_payment", False)
        self.alert(NotificationCategory.PAYMENT, "Payment confirmed")
        self.alert(NotificationCategory.TOKENS, "Tokens added")
        texts = self.texts_to(7001)
        self.assertEqual(len(texts), 1)
        self.assertIn("Tokens added", texts[0])

    def test_the_students_own_switch_still_applies(self):
        prefs = notification_service.preferences(self.student)
        prefs.telegram_enabled = False
        prefs.save()
        self.alert()
        self.assertEqual(self.texts_to(7001), [])

    def test_the_per_model_low_token_warning_obeys_the_tokens_switch(self):
        wallet = UserAIWallet.objects.create(user=self.student, ai_model=model_for(GPT), balance=50,
                                             allocated_tokens=1000, used_tokens=950)
        settings_service.set_value("telegram_send_tokens", False)
        token_alert_service._telegram_copy(wallet, 5, "https://ai.example.com/app/pricing/")
        self.assertEqual(self.texts_to(7001), [])
        settings_service.set_value("telegram_send_tokens", True)
        token_alert_service._telegram_copy(wallet, 5, "https://ai.example.com/app/pricing/")
        self.assertEqual(len(self.texts_to(7001)), 1)

    def test_sign_in_codes_ignore_the_alert_switches(self):
        """Codes are not alerts: a student who chose Telegram for them still gets them."""
        settings_service.set_value("telegram_alerts_enabled", False)
        otp_service._send_telegram(telegram_service.linked_account(self.student), "123456", OTPPurpose.LOGIN)
        texts = self.texts_to(7001)
        self.assertEqual(len(texts), 1)
        self.assertIn("<code>123456</code>", texts[0])


# ---------------------------------------------------------------------------
# The community group
# ---------------------------------------------------------------------------
@override_settings(**GROUP)
class GroupTests(TelegramTestCase):
    def announce(self, **extra) -> Announcement:
        defaults = {"title": "Exam week", "body": "Revise early.", "level": NotificationLevel.WARNING,
                    "audience": AnnouncementAudience.ALL, "action_label": "See plans",
                    "action_url": "/app/pricing/", "telegram_channel": False}
        defaults.update(extra)
        return Announcement.objects.create(**defaults)

    def test_a_post_goes_to_the_group_and_is_recorded(self):
        delivery = telegram_service.post_to_group("<b>Hello</b>")
        self.assertEqual(delivery.status, "sent")
        self.assertEqual(delivery.target, "group")
        self.assertEqual(self.fake.sent()[-1]["chat_id"], "-1009876543210")

    def test_without_a_group_nothing_is_attempted(self):
        with self.settings(TELEGRAM_GROUP_ID=""):
            delivery = telegram_service.post_to_group("<b>Hello</b>")
        self.assertEqual(delivery.status, "skipped")
        self.assertEqual(self.fake.sent(), [])

    def test_an_announcement_can_go_to_the_group_and_be_taken_back(self):
        announcement = self.announce(telegram_group=True)
        self.assertEqual(notification_service.publish_announcement(announcement), 1)
        post = self.fake.sent()[-1]
        self.assertEqual(post["chat_id"], "-1009876543210")
        self.assertIn("<b>Exam week</b>", post["text"])
        self.assertIn("<i>Important", post["text"])
        announcement.refresh_from_db()
        self.assertEqual((announcement.group_status, announcement.group_message_id),
                         ("sent", str(self.fake.message_id)))

        notification_service.retract_announcement(announcement)
        deleted = self.fake.sent("deleteMessage")[-1]
        self.assertEqual((deleted["chat_id"], deleted["message_id"]), ("-1009876543210", self.fake.message_id))
        announcement.refresh_from_db()
        self.assertEqual(announcement.group_status, "deleted")

    def test_channel_and_group_can_both_be_posted_to(self):
        notification_service.publish_announcement(self.announce(telegram_group=True, telegram_channel=True))
        self.assertEqual({p["chat_id"] for p in self.fake.sent()}, {"@aiassistanthelps", "-1009876543210"})

    def test_a_failed_group_post_is_recorded_and_the_announcement_still_lands(self):
        self.fake.queue("sendMessage", CHAT_NOT_FOUND)
        announcement = self.announce(telegram_group=True)
        self.assertEqual(notification_service.publish_announcement(announcement), 1)
        announcement.refresh_from_db()
        self.assertEqual(announcement.group_status, "failed")
        self.assertIn("chat not found", announcement.group_error)

    def test_adding_the_bot_to_a_group_is_noticed(self):
        status = telegram_service.handle_update({"update_id": 2, "my_chat_member": {
            "chat": {"id": -1005550001, "type": "supergroup", "title": "EPC Students"},
            "new_chat_member": {"status": "member", "user": {"id": 999}},
        }})
        self.assertEqual(status, "group_member")
        found = telegram_service.detected_group()
        self.assertEqual((found["id"], found["title"], found["type"], found["can_post"]),
                         ("-1005550001", "EPC Students", "supergroup", True))

    def test_a_muted_bot_is_reported_as_unable_to_post(self):
        telegram_service.handle_update({"update_id": 3, "my_chat_member": {
            "chat": {"id": -1005550002, "type": "group", "title": "Quiet"},
            "new_chat_member": {"status": "restricted", "can_send_messages": False, "user": {"id": 999}},
        }})
        self.assertFalse(telegram_service.detected_group()["can_post"])

    def test_a_group_that_became_a_supergroup_reports_its_new_id(self):
        self.fake.queue("sendMessage", migrated(-1004444444444))
        delivery = telegram_service.post_to_group("<b>Hello</b>")
        self.assertEqual(delivery.status, "failed")
        found = telegram_service.detected_group()
        self.assertEqual((found["id"], found["status"]), ("-1004444444444", "migrated"))

    def test_group_status_reads_the_bots_membership(self):
        self.fake.queue("getChat", {"ok": True, "result": {"id": -1009876543210, "type": "supergroup",
                                                            "title": "EPC Students"}})
        self.fake.queue("getChatMember", {"ok": True, "result": {"status": "member", "user": {"id": 999}}})
        status = telegram_service.group_status(bot_id=999)
        self.assertTrue(status["can_post"])
        self.assertFalse(status["can_delete"])
        self.assertEqual(status["chat"]["title"], "EPC Students")

    def test_group_status_explains_a_migration(self):
        self.fake.queue("getChat", migrated(-1004444444444))
        status = telegram_service.group_status(bot_id=999)
        self.assertIn("-1004444444444", status["error"])

    def test_nothing_private_is_ever_said_in_a_group(self):
        student_account = link(self.student, 7002)
        group = {"id": -1009876543210, "type": "supergroup", "title": "EPC Students"}
        token = telegram_service.issue_link_token(make_user("joiner")).link_token
        self.assertEqual(telegram_service.handle_update(message(f"/start {token}", group)), "not_private")
        self.assertEqual(telegram_service.handle_update(message("/status", group)), "not_private")
        for text in self.texts_to(-1009876543210):
            self.assertNotIn("balance", text.lower())            # never anyone's account details
        student_account.refresh_from_db()
        self.assertEqual(student_account.telegram_id, "7002")


# ---------------------------------------------------------------------------
# Words, not emoji
# ---------------------------------------------------------------------------
class CleanMessageTests(TelegramTestCase):
    def assertClean(self, text: str):
        self.assertIsNone(EMOJI.search(text), f"emoji in: {text!r}")

    def test_every_message_the_bot_sends_is_emoji_free(self):
        plan = Plan.objects.create(name="Student — Pro", tokens=1000, price=Decimal("5.00"), duration_days=30)
        account = link(self.student, 7100)

        # Bot conversations.
        telegram_service.handle_update(message("/start", private_chat(7200, "newbie")))
        telegram_service.handle_update(message("/start", private_chat(7100)))
        telegram_service.handle_update(message("/status", private_chat(7100)))
        telegram_service.handle_update(message("/unlink", private_chat(7100)))
        telegram_service.handle_update(message("/help", private_chat(7100)))
        telegram_service.handle_update(message("/start expired-token", private_chat(7300, "late")))
        telegram_service.handle_update(message("/start", {"id": -100123, "type": "supergroup", "title": "G"}))
        linker = make_user("linker")
        token = telegram_service.issue_link_token(linker).link_token
        telegram_service.handle_update(message(f"/start {token}", private_chat(7400, "linker")))

        # Alerts.
        notification_service.check_low_balance(self.student, balance=0)
        subscription = Subscription.objects.create(
            user=self.student, plan=plan, starts_at=timezone.now(),
            ends_at=timezone.now() + timedelta(days=3, hours=1),
        )
        notification_service.send_expiry_reminders()
        payment = Payment.objects.create(user=self.student, plan=plan, amount=Decimal("5.00"), method="aba_payway",
                                         charge_amount=Decimal("5.00"), charge_currency="USD", tokens_to_grant=1000)
        notification_service.payment_confirmed(payment)
        payment_notice_service.not_completed(payment, "failed", "declined")
        otp_service._send_telegram(account, "654321", OTPPurpose.LOGIN)
        telegram_service.unlink(account)
        self.assertTrue(subscription.pk)

        texts = [payload["text"] for payload in self.fake.sent()]
        self.assertGreaterEqual(len(texts), 12)
        for text in texts:
            self.assertClean(text)

    def test_announcements_carry_a_level_line_instead_of_a_symbol(self):
        for level, word in ((NotificationLevel.INFO, "Announcement"), (NotificationLevel.SUCCESS, "Good news"),
                            (NotificationLevel.WARNING, "Important"), (NotificationLevel.DANGER, "Urgent")):
            announcement = Announcement(title="T", body="B", level=level)
            text = notification_service.announcement_telegram_text(announcement)
            self.assertClean(text)
            self.assertIn(f"<i>{word} · ", text)

    def test_no_emoji_anywhere_in_the_code_that_writes_telegram_messages(self):
        from pathlib import Path

        from django.conf import settings

        for name in ("telegram_service", "notification_service", "otp_service", "payment_notice_service",
                     "token_alert_service"):
            source = (Path(settings.BASE_DIR) / "AI" / "services" / f"{name}.py").read_text(encoding="utf-8")
            self.assertIsNone(EMOJI.search(source), name)
