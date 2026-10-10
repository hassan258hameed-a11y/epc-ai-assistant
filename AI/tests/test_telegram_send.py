"""Console → Telegram → Send a message: the channel, the group, one student, everyone.

No test talks to Telegram: ``requests.post`` is the FakeTelegram from the
integration tests, which records every Bot API call.
"""
from __future__ import annotations

import re
from unittest.mock import patch

from django.test import override_settings
from django.utils import timezone

from AI.models import (
    AccountStatus,
    Notification,
    NotificationPreference,
    Role,
    StaffRole,
    TelegramMessage,
    TelegramMessageStatus,
    User,
)
from AI.services import telegram_message_service
from AI.tests.test_telegram_integration import BLOCKED, PASSWORD, TG, TelegramTestCase, link, make_user

URL = "/admin/telegram/send/"
GROUP = {**TG, "TELEGRAM_GROUP_ID": "-1005550001", "TELEGRAM_GROUP_URL": "https://t.me/+group"}


def inline_spawn(target, *args):
    target(*args)


def staff(username: str, role: str) -> User:
    return User.objects.create_user(
        username=username, email=f"{username}@staff.test", password=PASSWORD, full_name=username.title(),
        role=Role.ADMIN, staff_role=role, status=AccountStatus.ACTIVE, is_verified=True,
    )


@override_settings(**GROUP)
class SendPageTests(TelegramTestCase):
    def setUp(self):
        super().setUp()
        self.owner = User.objects.create_superuser(username="boss", email="boss@test.local", password=PASSWORD)
        self.client.force_login(self.owner)
        patcher = patch("AI.services.telegram_message_service.time.sleep")
        patcher.start()
        self.addCleanup(patcher.stop)

    def send(self, **data):
        payload = {"title": "", "body": "Hello students", "button_label": "", "button_url": ""}
        payload.update(data)
        return self.client.post(URL, payload)

    # -- the page -------------------------------------------------------------
    def test_the_page_offers_every_destination_with_a_preview(self):
        link(self.student, 7001)
        page = self.client.get(URL)
        self.assertEqual(page.status_code, 200)
        html = page.content.decode()
        for value in ("channel", "group", "student", "students"):
            self.assertIn(f'name="target" value="{value}"', html)
        self.assertIn("Sokha", html)  # the connected student is in the picker
        self.assertIn("Preview", html)
        self.assertIn("Send on Telegram", html)  # in the sidebar

    @override_settings(TELEGRAM_BOT_TOKEN="")
    def test_without_a_bot_it_points_to_setup(self):
        self.assertContains(self.client.get(URL), "The Telegram bot isn't set up yet")

    @override_settings(TELEGRAM_GROUP_ID="")
    def test_a_missing_group_is_greyed_out_not_hidden(self):
        html = self.client.get(URL).content.decode()
        tile = re.search(r'<input type="radio" name="target" value="group"[^>]*>', html).group(0)
        self.assertIn("disabled", tile)

    # -- channel and group ------------------------------------------------------
    def test_a_channel_post_with_a_button_and_its_deletion(self):
        self.send(target="channel", title="New guides", body="Open the app <b>now</b>",
                  button_label="Open", button_url="https://example.com/guides")
        post = [p for p in self.fake.sent() if p["chat_id"] == "@aiassistanthelps"][-1]
        self.assertIn("<b>New guides</b>", post["text"])
        self.assertIn("&lt;b&gt;now&lt;/b&gt;", post["text"])  # what was typed is text, not markup
        self.assertNotIn("From the", post["text"])  # the team sign-off is for private chats
        self.assertIn("example.com/guides", str(post.get("reply_markup")))

        message = TelegramMessage.objects.get()
        self.assertEqual(message.status, TelegramMessageStatus.SENT)
        self.assertTrue(message.telegram_message_id)
        self.assertContains(self.client.get(URL), "Delivered")

        self.client.post(f"{URL}{message.pk}/delete/")
        message.refresh_from_db()
        self.assertEqual(message.status, TelegramMessageStatus.DELETED)
        self.assertIn("deleteMessage", [method for method, _ in self.fake.calls])

    def test_a_post_older_than_48_hours_cannot_be_deleted(self):
        self.send(target="channel")
        message = TelegramMessage.objects.get()
        TelegramMessage.objects.filter(pk=message.pk).update(created_at=timezone.now() - timezone.timedelta(hours=49))
        self.client.post(f"{URL}{message.pk}/delete/")
        message.refresh_from_db()
        self.assertEqual(message.status, TelegramMessageStatus.SENT)

    def test_a_group_post(self):
        self.send(target="group", body="Study session at 7")
        self.assertIn("Study session at 7", self.texts_to("-1005550001")[-1])
        self.assertEqual(TelegramMessage.objects.get().status, TelegramMessageStatus.SENT)

    # -- one student ------------------------------------------------------------
    def test_one_student_gets_it_privately_and_in_the_app(self):
        link(self.student, 7002)
        self.send(target="student", student=self.student.pk, title="About your exam", body="Good luck!",
                  in_app="on")
        text = self.texts_to(7002)[-1]
        self.assertIn("<b>About your exam</b>", text)
        self.assertIn("From the", text)
        self.assertEqual(TelegramMessage.objects.get().status, TelegramMessageStatus.SENT)
        self.assertTrue(Notification.objects.filter(user=self.student, title="About your exam").exists())

    def test_a_student_who_turned_telegram_off_is_not_messaged(self):
        link(self.student, 7003)
        NotificationPreference.objects.update_or_create(user=self.student, defaults={"telegram_enabled": False})
        self.send(target="student", student=self.student.pk, in_app="on")
        self.assertEqual(self.texts_to(7003), [])
        message = TelegramMessage.objects.get()
        self.assertEqual(message.status, TelegramMessageStatus.FAILED)
        self.assertIn("turned Telegram messages off", message.error)
        # …but the in-app copy still arrives.
        self.assertTrue(Notification.objects.filter(user=self.student).exists())

    def test_a_blocked_bot_is_reported(self):
        link(self.student, 7004)
        self.fake.queue("sendMessage", BLOCKED)
        self.send(target="student", student=self.student.pk)
        self.assertEqual(TelegramMessage.objects.get().status, TelegramMessageStatus.FAILED)

    def test_staff_accounts_cannot_be_messaged_from_here(self):
        other = staff("other.admin", StaffRole.SUPPORT)
        link(other, 7005)
        self.send(target="student", student=other.pk)
        self.assertFalse(TelegramMessage.objects.exists())
        self.assertEqual(self.texts_to(7005), [])

    # -- everyone ---------------------------------------------------------------
    def test_all_connected_students_in_the_background(self):
        link(self.student, 7010)
        second = make_user("dara")
        link(second, 7011)
        quiet = make_user("quiet")
        link(quiet, 7012)
        NotificationPreference.objects.update_or_create(user=quiet, defaults={"telegram_enabled": False})
        admin_chat = staff("chat.admin", StaffRole.SUPPORT)
        link(admin_chat, 7013)
        make_user("unlinked")

        with patch("AI.services.notification_service._spawn", side_effect=inline_spawn), \
                self.captureOnCommitCallbacks(execute=True):
            self.send(target="students", body="Exams start Monday")

        self.assertIn("Exams start Monday", self.texts_to(7010)[-1])
        self.assertIn("Exams start Monday", self.texts_to(7011)[-1])
        self.assertEqual(self.texts_to(7012), [])  # switched Telegram off
        self.assertEqual(self.texts_to(7013), [])  # staff, not a student
        message = TelegramMessage.objects.get()
        self.assertEqual((message.recipients, message.sent), (2, 2))
        self.assertEqual(message.status, TelegramMessageStatus.SENT)

    def test_a_partial_send_says_so(self):
        link(self.student, 7020)
        link(make_user("dara"), 7021)
        self.fake.queue("sendMessage", BLOCKED)
        with patch("AI.services.notification_service._spawn", side_effect=inline_spawn), \
                self.captureOnCommitCallbacks(execute=True):
            self.send(target="students")
        message = TelegramMessage.objects.get()
        self.assertEqual(message.status, TelegramMessageStatus.PARTLY)
        self.assertEqual((message.sent, message.failed), (1, 1))

    # -- checks before anything goes out ------------------------------------------
    def test_an_empty_message_is_refused_and_the_draft_kept(self):
        response = self.send(target="channel", title="Draft title", body="   ", follow=False)
        self.assertFalse(TelegramMessage.objects.exists())
        page = self.client.get(URL)
        self.assertContains(page, 'value="Draft title"')

    def test_a_button_needs_a_real_link(self):
        for label, url in (("Open", "javascript:alert(1)"), ("Open", ""), ("", "https://x.test")):
            with self.subTest(url=url):
                self.send(target="channel", button_label=label, button_url=url)
        self.assertFalse(TelegramMessage.objects.exists())
        self.assertEqual(self.fake.sent(), [])
        self.assertEqual(telegram_message_service.clean_button("Plans", "/app/pricing/"), ("Plans", "/app/pricing/"))

    def test_a_message_that_is_too_long_is_refused(self):
        self.send(target="channel", body="x" * (telegram_message_service.MAX_BODY + 1))
        self.assertFalse(TelegramMessage.objects.exists())


@override_settings(**GROUP)
class SendPermissionTests(TelegramTestCase):
    def test_content_posts_to_the_channel_but_cannot_message_students(self):
        content = staff("writer", StaffRole.CONTENT)
        link(self.student, 7030)
        self.client.force_login(content)
        html = self.client.get(URL).content.decode()
        self.assertIn('value="channel"', html)
        self.assertNotIn('value="students"', html)
        self.assertNotIn("Sokha", html)  # no student records for this role

        self.client.post(URL, {"target": "student", "student": self.student.pk, "body": "hi"})
        self.assertEqual(self.texts_to(7030), [])
        self.client.post(URL, {"target": "channel", "body": "hi"})
        self.assertEqual(TelegramMessage.objects.get().target, "channel")

    def test_support_messages_students_but_cannot_post_to_the_channel(self):
        support = staff("helper", StaffRole.SUPPORT)
        link(self.student, 7031)
        self.client.force_login(support)
        html = self.client.get(URL).content.decode()
        self.assertNotIn('value="channel"', html)
        self.assertIn('value="student"', html)

        self.client.post(URL, {"target": "channel", "body": "hi"})
        self.assertFalse(TelegramMessage.objects.filter(target="channel").exists())
        self.client.post(URL, {"target": "student", "student": self.student.pk, "body": "hi"})
        self.assertIn("hi", self.texts_to(7031)[-1])

    def test_students_cannot_open_the_page(self):
        self.client.force_login(self.student)
        response = self.client.get(URL)
        self.assertNotEqual(response.status_code, 200)
