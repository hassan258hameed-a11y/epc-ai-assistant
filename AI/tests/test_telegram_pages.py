"""Telegram's pages: the console's group card, alert switches and linked list,
the announcement composer's group switch, and the student's Settings card.

No test here talks to Telegram: ``requests.post`` inside the service is the
same :class:`FakeTelegram` the integration tests use, extended to answer for
the community group. Every page is also checked for pictographs — the owner
asked for clean SVG icons only, never emoji.
"""
from __future__ import annotations

import json
import re
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from django.utils.html import escape

from AI.models import (
    Announcement,
    AuditAction,
    AuditLog,
    NotificationLevel,
    StaffRole,
    TelegramDelivery,
    TelegramDeliveryStatus,
)
from AI.services import settings_service, staff_permissions, telegram_service
from AI.tests.test_staff_roles import make_staff
from AI.tests.test_telegram_integration import PASSWORD, TG, FakeTelegram, link, make_user
from AI.views import console_telegram

GROUP_ID = "-1001234567"
GROUP_URL = "https://t.me/+abcDEF123"
GROUP = dict(TG, TELEGRAM_GROUP_ID=GROUP_ID, TELEGRAM_GROUP_URL=GROUP_URL)

#: Pictographs and emoji. Arrows (U+2190–U+21FF) and the ⌘ shortcut glyph
#: (U+2318) are typography, not pictures, and stay allowed.
PICTOGRAPH = re.compile(
    "[⌀-⌗⌙-⏿①-⓿■-◿☀-➿⬀-⯿"
    "️\U0001f000-\U0001faff]"
)
ALERT_KEYS = console_telegram.ALERT_KEYS


class GroupFakeTelegram(FakeTelegram):
    """The integration fake, answering getChat / getChatMember for the group."""

    group_member = {"status": "member", "user": {"id": 999}}

    def _default(self, method: str, payload: dict) -> dict:
        if str(payload.get("chat_id")) == GROUP_ID:
            if method == "getChat":
                return {"ok": True, "result": {"id": int(GROUP_ID), "type": "supergroup",
                                               "title": "EPC Community"}}
            if method == "getChatMember":
                return {"ok": True, "result": dict(self.group_member)}
        return super()._default(method, payload)


def pictographs(html: str) -> list[str]:
    return sorted(set(PICTOGRAPH.findall(html)))


def section(html: str, element_id: str) -> str:
    """The markup of one ``<section id=…>`` (they do not nest on these pages)."""
    start = html.index(f'id="{element_id}"')
    return html[start:html.index("</section>", start)]


@override_settings(**GROUP)
class PageCase(TestCase):
    def setUp(self):
        cache.clear()
        settings_service.bootstrap()
        self.fake = GroupFakeTelegram()
        patcher = patch("AI.services.telegram_service.requests.post", side_effect=self.fake)
        self.post = patcher.start()
        self.addCleanup(patcher.stop)
        sleeper = patch("AI.services.telegram_service.time.sleep")
        sleeper.start()
        self.addCleanup(sleeper.stop)
        self.owner = make_staff("owner.tg", StaffRole.OWNER)
        self.student = make_user("sokha")
        self.url = reverse("console:telegram")
        self.action_url = reverse("console:telegram_action")

    def page(self, user=None, **params) -> str:
        self.client.force_login(user or self.owner)
        response = self.client.get(self.url, params)
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def act(self, action: str, user=None, **data):
        self.client.force_login(user or self.owner)
        return self.client.post(self.action_url, {"action": action, **data})


# ---------------------------------------------------------------------------
# Console → Telegram: the community group card
# ---------------------------------------------------------------------------
class GroupCardTests(PageCase):
    @override_settings(TELEGRAM_GROUP_ID="", TELEGRAM_GROUP_URL="")
    def test_not_configured(self):
        card = section(self.page(), "group")
        self.assertIn("Community group", card)
        self.assertIn("Not configured", card)
        self.assertIn("TELEGRAM_GROUP_ID", card)
        self.assertNotIn('value="test_group"', card)
        # Nobody asked Telegram about a group that isn't set.
        self.assertFalse([p for _m, p in self.fake.calls if str(p.get("chat_id")) == GROUP_ID])

    @override_settings(TELEGRAM_BOT_TOKEN="", TELEGRAM_GROUP_ID=GROUP_ID)
    def test_without_the_bot_it_asks_for_the_bot_first(self):
        card = section(self.page(), "group")
        self.assertIn("Not configured", card)
        self.assertIn("Configure the bot first", card)
        self.assertEqual(self.fake.calls, [])

    def test_configured_shows_the_live_status_and_the_settings(self):
        card = section(self.page(), "group")
        for text in ("EPC Community", "Supergroup", "Can post", "Deletes own posts only",
                     f'<span class="tg-code">{GROUP_ID}</span>', "TELEGRAM_GROUP_ID in .env", GROUP_URL,
                     'value="test_group"', "Check group permissions"):
            self.assertIn(text, card)
        asked = [(m, p["chat_id"]) for m, p in self.fake.calls if m in ("getChat", "getChatMember")]
        self.assertIn(("getChat", GROUP_ID), asked)
        self.assertIn(("getChatMember", GROUP_ID), asked)

    def test_an_administrator_bot_can_delete(self):
        self.fake.group_member = {"status": "administrator", "can_delete_messages": True, "user": {"id": 999}}
        card = section(self.page(), "group")
        self.assertIn("Can delete", card)
        self.assertIn("Administrator", card)

    def test_a_group_the_bot_cannot_reach_is_a_problem(self):
        self.fake.queue("getChat", {"ok": True, "result": {"id": -1001234567890, "type": "channel",
                                                            "title": "AI Assistant Helps"}},
                        {"ok": False, "error_code": 400, "description": "Bad Request: chat not found"})
        card = section(self.page(), "group")
        self.assertIn("Problem", card)
        self.assertIn("chat not found", card)

    def test_the_status_is_cached_and_re_check_asks_again(self):
        self.page()
        calls = len(self.fake.calls)
        self.page()
        self.assertEqual(len(self.fake.calls), calls, "a reload within a minute is served from the cache")
        self.page(refresh="1")
        self.assertGreater(len(self.fake.calls), calls)

    def test_a_detected_group_shows_its_id_with_a_copy_button(self):
        settings_service.set_value(telegram_service.DETECTED_GROUP_KEY, json.dumps({
            "id": "-100555", "title": "Class 12B", "username": "", "type": "supergroup",
            "status": "member", "can_post": True, "at": "2026-09-29T10:00:00",
        }))
        card = section(self.page(), "group")
        self.assertIn("Detected group", card)
        self.assertIn("Class 12B", card)
        self.assertIn('data-tg-copy="-100555"', card)
        self.assertIn('href="#i-copy"', card)

    def test_a_migrated_group_is_explained(self):
        settings_service.set_value(telegram_service.DETECTED_GROUP_KEY, json.dumps({
            "id": "-100777", "title": "", "username": "", "type": "supergroup",
            "status": "migrated", "can_post": True, "at": "2026-09-29T10:00:00",
        }))
        card = section(self.page(), "group")
        self.assertIn("upgraded to a supergroup", card)
        self.assertIn('data-tg-copy="-100777"', card)

    def test_no_hint_when_the_detected_group_is_the_one_set(self):
        settings_service.set_value(telegram_service.DETECTED_GROUP_KEY, json.dumps({
            "id": GROUP_ID, "title": "EPC Community", "username": "", "type": "supergroup",
            "status": "member", "can_post": True, "at": "2026-09-29T10:00:00",
        }))
        self.assertNotIn("Detected group", section(self.page(), "group"))

    def test_the_checklist_and_the_setup_guide_cover_the_group(self):
        html = self.page()
        for text in ("TELEGRAM_GROUP_URL", "Create the community group", "changes its id",
                     "privacy mode doesn't stop it posting"):
            self.assertIn(text, html)

    def test_group_posts_are_labelled_in_the_delivery_log(self):
        telegram_service.post_to_group("hello", kind="test")
        log = section(self.page(), "deliveries")
        self.assertIn('<span class="badge-brand">Group</span>', log)


class TestGroupActionTests(PageCase):
    def test_test_group_posts_to_the_group_and_is_audited(self):
        response = self.act("test_group")
        self.assertRedirects(response, self.url, fetch_redirect_response=False)
        post = self.fake.sent()[-1]
        self.assertEqual(post["chat_id"], GROUP_ID)
        self.assertIn("<b>Test from the", post["text"])
        self.assertIn("posting to this group works", post["text"])
        self.assertEqual(pictographs(post["text"]), [], "Telegram messages are plain bold text, no emoji")
        delivery = TelegramDelivery.objects.get()
        self.assertEqual((delivery.target, delivery.kind, delivery.status), ("group", "test", "sent"))
        self.assertTrue(AuditLog.objects.filter(action=AuditAction.SETTINGS_UPDATE,
                                                description="Sent a test post to the Telegram group").exists())
        page = self.client.get(self.url)
        self.assertContains(page, "Test posted to the group")

    def test_a_failed_group_post_says_why(self):
        self.fake.queue("sendMessage", {"ok": False, "error_code": 403,
                                        "description": "Forbidden: bot is not a member of the group chat"})
        self.act("test_group")
        self.assertContains(self.client.get(self.url), "bot is not a member of the group chat")
        self.assertEqual(TelegramDelivery.objects.get().status, TelegramDeliveryStatus.FAILED)

    def test_a_migrated_group_points_to_the_new_id(self):
        self.fake.queue("sendMessage", {"ok": False, "error_code": 400,
                                        "description": "Bad Request: group chat was upgraded to a supergroup chat",
                                        "parameters": {"migrate_to_chat_id": -100999888}})
        self.act("test_group")
        page = self.client.get(self.url)
        self.assertContains(page, "its new id is -100999888")
        self.assertContains(page, 'data-tg-copy="-100999888"')

    @override_settings(TELEGRAM_GROUP_ID="")
    def test_test_group_needs_a_group(self):
        response = self.act("test_group")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.fake.sent(), [])
        self.assertContains(self.client.get(self.url), "Set TELEGRAM_GROUP_ID first")

    def test_the_channel_test_has_no_emoji_either(self):
        self.act("test_channel")
        text = self.fake.sent()[-1]["text"]
        self.assertTrue(text.startswith("<b>Test from the"))
        self.assertEqual(pictographs(text), [])

    def test_check_reports_a_group_the_bot_cannot_post_in(self):
        self.fake.group_member = {"status": "restricted", "can_send_messages": False, "user": {"id": 999}}
        response = self.act("check")
        page = self.client.get(response.url)
        self.assertContains(page, "cannot send messages")

    def test_check_refreshes_the_group_status(self):
        self.page()  # cached: the bot is a plain member
        self.fake.group_member = {"status": "administrator", "can_delete_messages": True, "user": {"id": 999}}
        self.act("check")
        page = self.client.get(self.url).content.decode()
        self.assertIn("Can delete", section(page, "group"))
        self.assertIn("Checked just now", page)


# ---------------------------------------------------------------------------
# Console → Telegram: which automatic alerts reach Telegram
# ---------------------------------------------------------------------------
class AlertSwitchTests(PageCase):
    def test_the_card_lists_the_master_and_every_category(self):
        card = section(self.page(), "alerts")
        for key in ALERT_KEYS:
            self.assertIn(f'name="{key}"', card)
            self.assertIn(escape(settings_service.DEFAULTS[key][3]), card)
            self.assertIn(escape(settings_service.DEFAULTS[key][4]), card)
        self.assertIn("Sign-in codes are not affected", card)
        self.assertIn('value="alert_settings"', card)
        self.assertIn("5 of 5 on", card)

    def test_saving_writes_all_six_keys_and_is_audited(self):
        self.act("alert_settings", telegram_alerts_enabled="on", telegram_send_payment="on",
                 telegram_send_system="on")
        stored = {key: settings_service.get(key) for key in ALERT_KEYS}
        self.assertEqual(stored, {
            "telegram_alerts_enabled": True, "telegram_send_payment": True, "telegram_send_tokens": False,
            "telegram_send_expiry": False, "telegram_send_system": True, "telegram_send_referral": False,
        })
        entry = AuditLog.objects.get(action=AuditAction.SETTINGS_UPDATE, actor=self.owner)
        self.assertIn("Telegram alerts", entry.description)
        self.assertIn("Tokens off", entry.description)
        self.assertIn("Referral rewards off", entry.description)
        self.assertEqual(entry.target_label, "Telegram")
        self.assertIn("2 of 5 on", section(self.page(), "alerts"))

    def test_the_master_off_pauses_the_categories_but_keeps_them_editable(self):
        self.act("alert_settings", telegram_send_payment="on", telegram_send_tokens="on")
        self.assertFalse(settings_service.get("telegram_alerts_enabled"))
        self.assertTrue(settings_service.get("telegram_send_payment"))
        card = section(self.page(), "alerts")
        self.assertIn("Paused", card)
        self.assertIn("is-paused", card)
        switch = re.search(r'<input[^>]*name="telegram_send_payment"[^>]*>', card).group(0)
        self.assertIn("checked", switch)
        self.assertNotIn("disabled", switch)
        from AI.services import notification_service

        self.assertFalse(notification_service.telegram_category_on("payment"))

    def test_saving_works_before_the_bot_is_set_up(self):
        with override_settings(TELEGRAM_BOT_TOKEN=""):
            self.act("alert_settings", telegram_alerts_enabled="on")
        self.assertTrue(settings_service.get("telegram_alerts_enabled"))
        self.assertFalse(settings_service.get("telegram_send_tokens"))

    def test_saving_nothing_new_is_still_recorded(self):
        self.act("alert_settings", **{key: "on" for key in ALERT_KEYS})
        self.assertEqual(AuditLog.objects.get(actor=self.owner).description,
                         "Telegram alerts: saved, nothing changed")


# ---------------------------------------------------------------------------
# Console → Telegram: who has connected
# ---------------------------------------------------------------------------
class LinkedListTests(PageCase):
    def test_recently_linked_students_with_their_state(self):
        link(self.student, 7101, username="sok_tg")
        blocked = make_user("dara")
        account = link(blocked, 7102, username="dara_tg")
        account.blocked_at = timezone.now()
        account.last_delivery_at = timezone.now()
        account.save(update_fields=["blocked_at", "last_delivery_at"])
        card = section(self.page(), "linked")
        self.assertIn("Students on Telegram", card)
        self.assertIn(reverse("console:user_detail", args=[self.student.pk]), card)
        self.assertIn("@sok_tg", card)
        self.assertIn("@dara_tg", card)
        self.assertIn("Blocked the bot", card)
        self.assertIn("Reachable", card)
        self.assertIn("Nothing yet", card)
        # Newest first.
        self.assertLess(card.index("@dara_tg"), card.index("@sok_tg"))

    def test_more_than_twenty_are_paged(self):
        for number in range(23):
            link(make_user(f"pupil{number:02d}"), 8000 + number, username=f"pupil{number:02d}")
        card = section(self.page(), "linked")
        self.assertEqual(card.count('class="row"'), 20)
        self.assertIn("Page 1 of 2", card)
        self.assertIn("linked=2", card)
        second = section(self.page(linked="2"), "linked")
        self.assertEqual(second.count('class="row"'), 3)
        # A bad page number is just the first page.
        self.assertIn("Page 1 of 2", section(self.page(linked="abc"), "linked"))

    def test_roles_without_student_records_do_not_see_names(self):
        link(self.student, 7103, username="sok_tg")
        content = make_staff("content.tg", StaffRole.CONTENT)
        html = self.page(content)
        self.assertNotIn('id="linked"', html)
        self.assertNotIn("@sok_tg", html)
        support = make_staff("support.tg", StaffRole.SUPPORT)
        self.assertIn("@sok_tg", section(self.page(support), "linked"))

    def test_nobody_linked_yet(self):
        self.assertIn("Nobody has connected yet", section(self.page(), "linked"))


# ---------------------------------------------------------------------------
# Who may press the new buttons
# ---------------------------------------------------------------------------
class PermissionTests(PageCase):
    def test_the_new_actions_belong_to_operations(self):
        for action in ("test_group", "alert_settings"):
            area = staff_permissions.area_of("console:telegram_action", post={"action": action})
            self.assertEqual(area, "operations", action)

    def test_roles_without_operations_get_403(self):
        for role in (StaffRole.CONTENT, StaffRole.SUPPORT, StaffRole.FINANCE, ""):
            user = make_staff(f"no.ops.{role or 'blank'}", role)
            with self.subTest(role=role or "no role"):
                self.assertEqual(self.act("test_group", user).status_code, 403)
                self.assertEqual(self.act("alert_settings", user).status_code, 403)
        self.assertEqual(self.fake.sent(), [])
        self.assertTrue(settings_service.get("telegram_send_payment"), "nothing was switched off")
        self.assertFalse(AuditLog.objects.filter(description__startswith="Telegram alerts").exists())

    def test_operations_may_use_them(self):
        ops = make_staff("ops.tg", StaffRole.OPERATIONS)
        self.assertEqual(self.act("test_group", ops).status_code, 302)
        self.assertEqual(self.act("alert_settings", ops, telegram_alerts_enabled="on").status_code, 302)
        self.assertEqual(self.fake.sent()[-1]["chat_id"], GROUP_ID)

    def test_other_roles_see_the_switches_read_only_and_no_forms_they_cannot_use(self):
        content = make_staff("content.ro", StaffRole.CONTENT)
        html = self.page(content)
        card = section(html, "alerts")
        self.assertNotIn("<form", card)
        self.assertIn("Operations and owners can change these", card)
        switch = re.search(r'<input[^>]*name="telegram_alerts_enabled"[^>]*>', card).group(0)
        self.assertIn("disabled", switch)
        self.assertNotIn('value="test_group"', html)


# ---------------------------------------------------------------------------
# Console → Announcements
# ---------------------------------------------------------------------------
class ComposerTests(PageCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.owner)
        self.compose_url = reverse("console:notifications")

    def switch(self, html: str) -> str:
        return re.search(r'<input[^>]*name="telegram_group"[^>]*>', html, re.S).group(0)

    def test_the_composer_offers_the_group(self):
        html = self.client.get(self.compose_url).content.decode()
        self.assertIn("Post to the Telegram group", html)
        self.assertNotIn("disabled", self.switch(html))
        self.assertIn('for="anTgGroup"', html)

    @override_settings(TELEGRAM_GROUP_ID="")
    def test_without_a_group_the_switch_is_disabled_with_a_way_to_set_it_up(self):
        html = self.client.get(self.compose_url).content.decode()
        self.assertIn("disabled", self.switch(html))
        self.assertIn("Set <code>TELEGRAM_GROUP_ID</code> to switch this on.", html)
        self.assertIn(f'href="{self.url}#group"', html)

    @override_settings(TELEGRAM_BOT_TOKEN="")
    def test_without_the_bot_one_note_covers_both_posts(self):
        html = self.client.get(self.compose_url).content.decode()
        self.assertIn("disabled", self.switch(html))
        self.assertEqual(html.count("Telegram setup</a>"), 1)
        self.assertIn("The Telegram bot isn't configured", html)

    def test_publishing_with_the_group_switch_shows_its_status(self):
        self.client.post(self.compose_url, {"title": "Exam week", "body": "Revise.", "level": "info",
                                            "audience": "all", "telegram_group": "on"})
        announcement = Announcement.objects.get()
        self.assertTrue(announcement.telegram_group)
        self.assertIn(GROUP_ID, [p["chat_id"] for p in self.fake.sent()])
        self.assertContains(self.client.get(self.compose_url), "TELEGRAM GROUP &middot; SENT")

    def test_a_failed_group_post_shows_the_chip_and_its_error(self):
        Announcement.objects.create(
            title="Maintenance", body="Sunday", level=NotificationLevel.INFO, is_published=True,
            published_at=timezone.now(), telegram_channel=False, telegram_group=True,
            group_status="failed", group_error="Forbidden: bot was kicked from the supergroup chat",
        )
        html = self.client.get(self.compose_url).content.decode()
        chip = re.search(r'<span[^>]*an-tg-chip is-failed[^>]*>TELEGRAM GROUP &middot; FAILED</span>', html)
        self.assertIsNotNone(chip)
        self.assertIn('title="Forbidden: bot was kicked from the supergroup chat"', chip.group(0))
        self.assertIn("Group: Forbidden: bot was kicked", html)


# ---------------------------------------------------------------------------
# The student's side
# ---------------------------------------------------------------------------
class StudentPageTests(PageCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.student)

    def test_settings_links_the_group(self):
        html = self.client.get("/app/settings/", HTTP_X_APP_PANEL="1").content.decode()
        card = section(html, "telegram")
        self.assertIn("Join our Telegram group", card)
        self.assertIn(f'href="{GROUP_URL}"', card)
        self.assertIn("Join our channel", card)

    @override_settings(TELEGRAM_GROUP_URL="")
    def test_no_group_link_without_an_invite_link(self):
        html = self.client.get("/app/settings/", HTTP_X_APP_PANEL="1").content.decode()
        self.assertNotIn("Join our Telegram group", html)

    def test_help_lists_the_bot_channel_and_group_when_it_is_given_them(self):
        from AI.views import panels

        original = panels._help_context

        def with_telegram(user, form=None):
            context = original(user, form)
            context.update({"telegram_bot_url": telegram_service.bot_url(),
                            "telegram_channel_url": telegram_service.channel_url(),
                            "telegram_group_url": telegram_service.group_url()})
            return context

        with patch("AI.views.panels._help_context", side_effect=with_telegram):
            html = self.client.get("/app/help/", HTTP_X_APP_PANEL="1").content.decode()
        for text in ("Alerts from our bot", "Our channel", "Community group", GROUP_URL,
                     "https://t.me/aiassistanthelps", "/app/settings/#telegram"):
            self.assertIn(text, html)


# ---------------------------------------------------------------------------
# Clean SVG icons only, never emoji
# ---------------------------------------------------------------------------
class NoPictographTests(PageCase):
    def test_the_console_telegram_page(self):
        link(self.student, 7201)
        settings_service.set_value(telegram_service.DETECTED_GROUP_KEY, json.dumps({
            "id": "-100555", "title": "Class", "username": "", "type": "supergroup",
            "status": "member", "can_post": True, "at": "2026-09-29T10:00:00",
        }))
        self.assertEqual(pictographs(self.page()), [])
        self.act("alert_settings")  # the paused state
        self.assertEqual(pictographs(self.page()), [])

    @override_settings(TELEGRAM_BOT_TOKEN="", TELEGRAM_GROUP_ID="", TELEGRAM_CHANNEL_ID="")
    def test_the_console_telegram_page_unconfigured(self):
        self.assertEqual(pictographs(self.page()), [])

    def test_the_announcement_composer(self):
        self.client.force_login(self.owner)
        self.assertEqual(pictographs(self.client.get(reverse("console:notifications")).content.decode()), [])

    def test_student_settings_and_the_other_panels(self):
        self.client.force_login(self.student)
        for url in ("/app/settings/", "/app/help/", "/app/models/"):
            with self.subTest(url=url):
                self.assertEqual(pictographs(self.client.get(url).content.decode()), [])
        link(self.student, 7202)
        self.assertEqual(pictographs(self.client.get("/app/settings/").content.decode()), [])

    def test_the_models_table_draws_a_check_icon(self):
        self.client.force_login(self.student)
        html = self.client.get("/app/models/", HTTP_X_APP_PANEL="1").content.decode()
        self.assertNotIn("✓", html)
        if "cmp-table" in html and 'aria-label="Yes"' in html:
            self.assertIn('href="#i-check"', html)

    def test_the_sign_in_page_and_the_emails(self):
        self.client.logout()
        self.assertEqual(pictographs(self.client.get("/login/").content.decode()), [])
        from pathlib import Path

        from django.conf import settings

        for name in ("low_tokens.html", "notice.html", "otp.html"):
            with self.subTest(email=name):
                source = (Path(settings.BASE_DIR) / "templates" / "emails" / name).read_text(encoding="utf-8")
                self.assertEqual(pictographs(source), [])

    def test_the_prompt_tile_uses_the_star_icon(self):
        from pathlib import Path

        from django.conf import settings

        source = (Path(settings.BASE_DIR) / "templates" / "chat" / "chat.html").read_text(encoding="utf-8")
        self.assertNotIn("★", source)
        self.assertIn('<use href="#i-star"/>', source)

    def test_the_test_message_itself(self):
        text = console_telegram._test_text("posting to this group")
        self.assertEqual(pictographs(text), [])
        self.assertTrue(text.startswith("<b>"))
