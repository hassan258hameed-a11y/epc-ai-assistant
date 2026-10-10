"""Authorisation: students must never reach admin surfaces or another user's data."""
from __future__ import annotations

from django.test import TestCase

from AI.models import AccountStatus, Conversation, Message, MessageRole, Payment, Plan, Role, User
from AI.services import payment_service, settings_service, token_service

PASSWORD = "Sup3rSecret!x"

CONSOLE_URLS = [
    "/admin/", "/admin/users/", "/admin/accounts/", "/admin/tokens/", "/admin/tokens/history/",
    "/admin/usage/", "/admin/plans/", "/admin/plans/new/", "/admin/payments/",
    "/admin/referrals/", "/admin/notifications/", "/admin/audit-logs/", "/admin/settings/",
]


class ConsoleAccessTests(TestCase):
    def setUp(self):
        settings_service.bootstrap()
        self.student = User.objects.create_user(
            username="pupil", email="pupil@test.local", password=PASSWORD,
            is_verified=True, status=AccountStatus.ACTIVE,
        )
        self.admin = User.objects.create_superuser(
            username="boss", email="boss@test.local", password=PASSWORD
        )

    def test_anonymous_visitors_are_sent_to_the_admin_login(self):
        for url in CONSOLE_URLS:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 302, url)
            self.assertIn("/admin/login/", response.headers["Location"], url)

    def test_students_are_bounced_off_every_console_page(self):
        self.client.force_login(self.student)
        for url in CONSOLE_URLS:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 302, url)
            self.assertIn("/chat/", response.headers["Location"], url)

    def test_admins_can_open_the_console(self):
        self.client.force_login(self.admin)
        for url in CONSOLE_URLS:
            self.assertEqual(self.client.get(url).status_code, 200, url)

    def test_student_cannot_grant_themselves_tokens(self):
        self.client.force_login(self.student)
        response = self.client.post(
            "/admin/tokens/",
            {"user_id": self.student.pk, "action": "add", "amount": 999_999, "reason": "nice try"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(token_service.balance_of(self.student), 0)

    def test_student_cannot_disable_another_account(self):
        victim = User.objects.create_user(username="victim", email="victim@test.local", password=PASSWORD)
        self.client.force_login(self.student)

        response = self.client.post(f"/admin/users/{victim.pk}/toggle/")
        self.assertEqual(response.status_code, 302)

        victim.refresh_from_db()
        self.assertEqual(victim.status, AccountStatus.ACTIVE)
        self.assertTrue(victim.is_active)

    def test_student_cannot_confirm_a_payment(self):
        plan = Plan.objects.create(name="Test", tokens=1_000, price=1, duration_days=30)
        payment = payment_service.create_payment(self.student, plan, "aba_payway")

        self.client.force_login(self.student)
        response = self.client.post(f"/admin/payments/{payment.reference}/action/", {"action": "confirm"})

        self.assertEqual(response.status_code, 302)
        payment.refresh_from_db()
        self.assertEqual(payment.status, "pending")
        self.assertFalse(payment.tokens_credited)
        self.assertEqual(token_service.balance_of(self.student), 0)

    def test_admin_visiting_the_student_area_is_routed_to_the_console(self):
        self.client.force_login(self.admin)
        response = self.client.get("/dashboard/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/", response.headers["Location"])


class DataIsolationTests(TestCase):
    """A user must never read another user's data by changing an id."""

    def setUp(self):
        settings_service.bootstrap()
        self.alice = User.objects.create_user(
            username="alice", email="alice@test.local", password=PASSWORD,
            is_verified=True, status=AccountStatus.ACTIVE,
        )
        self.bob = User.objects.create_user(
            username="bob", email="bob@test.local", password=PASSWORD,
            is_verified=True, status=AccountStatus.ACTIVE,
        )
        token_service.grant(self.alice, 10_000)
        token_service.grant(self.bob, 10_000)

        self.alice_chat = Conversation.objects.create(user=self.alice, title="Alice private notes")
        Message.objects.create(conversation=self.alice_chat, role=MessageRole.USER, content="my secret question")

    def test_bob_cannot_open_alices_conversation_page(self):
        self.client.force_login(self.bob)
        response = self.client.get(f"/chat/{self.alice_chat.pk}/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/chat/", response.headers["Location"])

    def test_bob_cannot_read_alices_messages_through_the_api(self):
        self.client.force_login(self.bob)
        response = self.client.get(
            f"/api/chat/{self.alice_chat.pk}/messages/", HTTP_X_REQUESTED_WITH="XMLHttpRequest"
        )
        self.assertEqual(response.status_code, 404)
        self.assertNotIn("secret question", response.content.decode())

    def test_bob_cannot_delete_alices_conversation(self):
        self.client.force_login(self.bob)
        response = self.client.post(
            f"/api/chat/{self.alice_chat.pk}/delete/", HTTP_X_REQUESTED_WITH="XMLHttpRequest"
        )
        self.assertEqual(response.status_code, 404)
        self.assertTrue(Conversation.objects.filter(pk=self.alice_chat.pk).exists())

    def test_bob_cannot_rename_alices_conversation(self):
        self.client.force_login(self.bob)
        self.client.post(
            f"/api/chat/{self.alice_chat.pk}/rename/",
            data='{"title": "hacked"}',
            content_type="application/json",
        )
        self.alice_chat.refresh_from_db()
        self.assertEqual(self.alice_chat.title, "Alice private notes")

    def test_bob_cannot_send_a_message_into_alices_conversation(self):
        self.client.force_login(self.bob)
        response = self.client.post(
            "/api/chat/send/",
            {"message": "injecting", "conversation_id": self.alice_chat.pk},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.alice_chat.messages.count(), 1)

    def test_bob_cannot_view_alices_receipt(self):
        plan = Plan.objects.create(name="Test", tokens=1_000, price=5, duration_days=30)
        payment = payment_service.create_payment(self.alice, plan, "aba_payway")
        payment_service.settle(payment, source="test")

        self.client.force_login(self.bob)
        response = self.client.get(f"/payments/{payment.reference}/receipt/")
        self.assertEqual(response.status_code, 404)

    def test_alice_can_reach_her_own_data(self):
        self.client.force_login(self.alice)
        self.assertEqual(self.client.get(f"/chat/{self.alice_chat.pk}/").status_code, 200)
        response = self.client.get(
            f"/api/chat/{self.alice_chat.pk}/messages/", HTTP_X_REQUESTED_WITH="XMLHttpRequest"
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("secret question", response.content.decode())


class UnauthenticatedAccessTests(TestCase):
    def test_student_pages_require_a_session(self):
        for url in ["/chat/", "/billing/", "/help/", "/usage/", "/notifications/", "/profile/"]:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 302, url)
            self.assertIn("/login/", response.headers["Location"], url)

    def test_chat_api_returns_401_rather_than_a_redirect(self):
        response = self.client.post(
            "/api/chat/send/", {"message": "hi"}, HTTP_X_REQUESTED_WITH="XMLHttpRequest"
        )
        self.assertEqual(response.status_code, 401)
