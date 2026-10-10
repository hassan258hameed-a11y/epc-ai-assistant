"""The Create Accounts page, and the four things it can now do that it could not.

Each control on that page that reaches the backend has a test here that
proves it reaches it: the model an opening balance lands in, a plan granted
with no payment behind it, the credentials e-mail and its log row, and the
password-change flag. Plus the live username check, which has to agree with
the form's own rules or the page will say "available" about a name the form
then refuses.
"""
from __future__ import annotations

from decimal import Decimal

from django.core import mail
from django.urls import reverse

from AI.models import AccountSource, EmailLog, EmailType, Plan, Subscription, User
from AI.services import account_service
from AI.tests.test_multi_provider import GPT, SONNET, MultiProviderTestCase


class CreateAccountsPageTests(MultiProviderTestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.admin)

    def test_the_page_renders_the_design(self):
        response = self.client.get(reverse("console:accounts"))
        self.assertEqual(response.status_code, 200)
        for text in (
            "Hand a student a working login in seconds",
            "One account",
            "Bulk import",
            "Identity",
            "Where did they come from?",
            "Generate secure password",
            "Starting balance",
            "HANDOVER CARD",
            "What happens on create",
            "Recently created by admins",
        ):
            self.assertContains(response, text)
        # Every source the page offers is a value the model accepts.
        for code, _label, _tone in response.context["sources"]:
            self.assertIn(code, AccountSource.values)

    def test_username_check_agrees_with_the_form(self):
        url = reverse("console:accounts_check_username")
        self.assertEqual(self.client.get(url, {"u": ""}).json()["code"], "empty")
        self.assertEqual(self.client.get(url, {"u": "has space"}).json()["code"], "chars")
        self.assertEqual(self.client.get(url, {"u": "x" * 151}).json()["code"], "long")
        self.assertEqual(self.client.get(url, {"u": self.student.username.upper()}).json()["code"], "taken")
        self.assertEqual(self.client.get(url, {"u": "brand.new"}).json(), {"code": "ok", "available": True})

    def test_username_check_is_for_administrators_only(self):
        self.client.force_login(self.student)
        response = self.client.get(reverse("console:accounts_check_username"), {"u": "anything"})
        self.assertNotEqual(response.status_code, 200)


class CreateAccountBackendTests(MultiProviderTestCase):
    def test_opening_balance_lands_in_the_named_model(self):
        user, _password = account_service.create_account(
            username="sokha.ny", tokens=5_000, ai_model=self.models[SONNET], created_by=self.admin
        )
        wallets = {w.ai_model.key: w.balance for w in user.ai_wallets.all()}
        self.assertEqual(wallets.get(SONNET), 5_000, "the chosen model gets the balance")
        self.assertFalse(wallets.get(GPT), "no other model is touched")

    def test_without_a_named_model_the_default_still_receives_it(self):
        user, _password = account_service.create_account(username="dara.kim", tokens=1_000, created_by=self.admin)
        default = [w for w in user.ai_wallets.all() if w.balance == 1_000]
        self.assertEqual(len(default), 1)
        self.assertTrue(default[0].ai_model.is_default)

    def test_a_plan_is_granted_without_a_payment(self):
        plan = Plan.objects.create(
            name="Starter", tokens=2_500, ai_model=self.models[GPT],
            price=Decimal("3.00"), duration_days=30, is_active=True,
        )
        user, _password = account_service.create_account(username="chanthy", plan=plan, created_by=self.admin)
        subscription = Subscription.objects.get(user=user)
        self.assertEqual(subscription.plan, plan)
        self.assertIsNone(subscription.payment, "granted by hand, so nothing was paid")
        self.assertEqual(subscription.tokens_granted, 2_500)
        # A new student also receives the free trial in the default model, so
        # the plan's tokens are measured as the difference from an account
        # created the same way without one.
        control, _ = account_service.create_account(username="chanthy.control", created_by=self.admin)
        trial = control.ai_wallets.get(ai_model=self.models[GPT]).balance
        self.assertEqual(user.ai_wallets.get(ai_model=self.models[GPT]).balance - trial, 2_500)

    def test_credentials_are_emailed_and_logged(self):
        user, password = account_service.create_account(
            username="malis.keo", email="malis@example.com", send_credentials=True, created_by=self.admin
        )
        self.assertEqual(len(mail.outbox), 1)
        sent = mail.outbox[0]
        self.assertEqual(sent.to, ["malis@example.com"])
        self.assertIn(password, sent.body, "the login is in the message")
        self.assertIn("malis.keo", sent.body)
        log = EmailLog.objects.get(user=user, email_type=EmailType.CREDENTIALS)
        self.assertEqual(log.status, "sent")
        self.assertEqual(log.recipient, "malis@example.com")

    def test_no_address_means_no_email_and_no_log(self):
        account_service.create_account(username="vibol", send_credentials=True, created_by=self.admin)
        self.assertEqual(len(mail.outbox), 0)
        self.assertFalse(EmailLog.objects.filter(email_type=EmailType.CREDENTIALS).exists())

    def test_password_change_can_be_required_for_an_admin_set_password(self):
        user, _ = account_service.create_account(
            username="rithy", password="Quartz-harbor42!", force_password_change=True, created_by=self.admin
        )
        self.assertTrue(user.must_change_password)
        user2, _ = account_service.create_account(
            username="rithy2", password="Quartz-harbor42!", created_by=self.admin
        )
        self.assertFalse(user2.must_change_password, "an admin-set password is kept unless asked otherwise")

    def test_referral_is_a_source_the_page_can_record(self):
        user, _ = account_service.create_account(username="panha", source=AccountSource.REFERRAL, created_by=self.admin)
        self.assertEqual(user.source, "referral")
        self.assertEqual(user.get_source_display(), "Referral")


class CreateAccountFormFlowTests(MultiProviderTestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.admin)

    def test_creating_from_the_page_returns_to_it_with_the_handover(self):
        response = self.client.post(
            reverse("console:accounts"),
            {
                "action": "create",
                "username": "sreyleak.chan",
                "full_name": "Sreyleak Chan",
                "email": "",
                "phone": "",
                "source": AccountSource.TELEGRAM,
                "status": "active",
                "role": "student",
                "password": "",
                "initial_tokens": "3000",
                "initial_model": self.models[SONNET].pk,
                "notes": "",
            },
        )
        # fetch_redirect_response=False: following the redirect would consume
        # the one-shot handover this test then reads.
        self.assertRedirects(response, reverse("console:accounts"), fetch_redirect_response=False)
        user = User.objects.get(username="sreyleak.chan")
        self.assertEqual(user.ai_wallets.get(ai_model=self.models[SONNET]).balance, 3_000)

        page = self.client.get(reverse("console:accounts"))
        handover = page.context["handover"]
        self.assertEqual(handover["username"], "sreyleak.chan")
        self.assertTrue(handover["password"], "a generated password is shown once, on the handover card")
        self.assertIn(self.models[SONNET].name, handover["starting"], "the chosen model is named on the card")
        self.assertContains(page, "Sreyleak Chan can sign in now")
        # The new row is marked, and only this session sees the mark.
        self.assertTrue(any(getattr(s, "is_new_here", False) for s in page.context["recent"]))

    def test_emailing_credentials_needs_an_address(self):
        response = self.client.post(
            reverse("console:accounts"),
            {
                "action": "create", "username": "no.address", "email": "", "source": AccountSource.WEB,
                "status": "active", "role": "student", "email_credentials": "on",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(username="no.address").exists())
        self.assertContains(response, "Add an e-mail address")
