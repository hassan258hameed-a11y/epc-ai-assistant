"""The application shell: panels, routing, and the authorisation around them.

The chat is the product, so every other section is a region of it.  These tests
pin the two things that makes true: a panel answers a soft navigation with a
fragment and a cold load with the whole shell, and no amount of typing URLs
gets a student into somebody else's area.
"""
from __future__ import annotations

from django.test import TestCase
from django.urls import reverse

from AI.models import AccountStatus, AIModel, Plan, Role, User
from AI.services import settings_service, wallet_service
from AI.tests.helpers import GPT, fund, model_for

PASSWORD = "Sup3rSecret!x"

#: Every panel, and a string that only its own template produces.
PANELS = {
    "student:usage": "Balances by model",
    "student:pricing": "Tokens are bought per model",
    "student:help": "Ask us anything",
    "student:profile": "Your details",
    "student:settings": "Appearance",
    "student:billing": "Total spent",
    "student:notifications": "Alerts about your balance",
}


def make_student(username="panels", **extra) -> User:
    defaults = {
        "email": f"{username}@test.local",
        "full_name": "Panel Student",
        "is_verified": True,
        "status": AccountStatus.ACTIVE,
        "role": Role.STUDENT,
    }
    defaults.update(extra)
    return User.objects.create_user(username=username, password=PASSWORD, **defaults)


class PanelRenderingTests(TestCase):
    def setUp(self):
        settings_service.bootstrap()
        self.student = make_student()
        fund(self.student, 5_000)
        self.client.force_login(self.student)

    def test_a_cold_load_brings_the_whole_shell(self):
        """A refresh or a bookmark has to land somewhere usable, not on a fragment."""
        for name, marker in PANELS.items():
            with self.subTest(panel=name):
                response = self.client.get(reverse(name))
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, marker)
                self.assertContains(response, 'id="rail"')
                self.assertContains(response, 'id="panelHost"')
                self.assertContains(response, 'class="panel-on"')

    def test_a_soft_navigation_gets_the_fragment_alone(self):
        """The shell is already on screen; sending it again would replace it with itself."""
        for name, marker in PANELS.items():
            with self.subTest(panel=name):
                response = self.client.get(reverse(name), HTTP_X_APP_PANEL="1")
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, marker)
                self.assertNotContains(response, 'id="rail"')
                self.assertNotContains(response, "<!doctype html>")

    def test_the_chat_itself_is_not_a_panel(self):
        response = self.client.get(reverse("student:chat"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="panelHost"')
        self.assertNotContains(response, 'class="panel-on"')

    def test_the_old_standalone_pages_lead_into_the_shell(self):
        """Existing links keep working — they just resolve to where the content lives now."""
        for path, target in (
            ("/dashboard/", "/app/usage/"),
            ("/usage/", "/app/usage/"),
            ("/help/", "/app/help/"),
            ("/billing/", "/app/billing/"),
            ("/profile/", "/app/profile/"),
            ("/plans/", "/app/pricing/"),
            ("/notifications/", "/app/notifications/"),
        ):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response.headers["Location"], target)

    def test_a_query_string_survives_the_redirect(self):
        response = self.client.get("/usage/?page=2")
        self.assertEqual(response.headers["Location"], "/app/usage/?page=2")


class PanelAuthorisationTests(TestCase):
    """Hiding a link is not a permission. Every panel re-checks on the server."""

    def setUp(self):
        settings_service.bootstrap()
        self.student = make_student("guarded")

    def test_a_signed_out_visitor_is_sent_to_the_login_page(self):
        for name in PANELS:
            with self.subTest(panel=name):
                response = self.client.get(reverse(name))
                self.assertEqual(response.status_code, 302)
                self.assertIn("/login/", response.headers["Location"])

    def test_a_signed_out_soft_navigation_is_refused_as_json(self):
        response = self.client.get(reverse("student:usage"), HTTP_X_APP_PANEL="1",
                                   HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(response.status_code, 401)

    def test_a_student_cannot_reach_the_admin_console_by_typing_the_url(self):
        self.client.force_login(self.student)
        for path in ("/admin/", "/admin/users/", "/admin/ai-tokens/", "/admin/settings/",
                     "/admin/payments/", "/admin/health/"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response.headers["Location"], reverse("student:chat"))

    def test_an_administrator_is_kept_out_of_the_student_shell(self):
        admin = User.objects.create_superuser(
            username="boss", email="boss@test.local", password=PASSWORD
        )
        self.client.force_login(admin)
        for name in list(PANELS) + ["student:chat"]:
            with self.subTest(panel=name):
                response = self.client.get(reverse(name))
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response.headers["Location"], reverse("console:dashboard"))

    def test_a_disabled_account_is_signed_out_of_every_panel(self):
        self.client.force_login(self.student)
        self.student.status = AccountStatus.DISABLED
        self.student.is_active = False
        self.student.save(update_fields=["status", "is_active"])

        response = self.client.get(reverse("student:usage"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response.headers["Location"])


class PanelFormTests(TestCase):
    """A form inside a panel has to report its own outcome, in the panel.

    The whole point of the shell is that you never leave it — so the "saved"
    confirmation has to come back inside the fragment that replaces the form.
    A redirect-and-forget would drop it on the floor.
    """

    def setUp(self):
        settings_service.bootstrap()
        self.student = make_student("saver")
        self.client.force_login(self.student)

    def test_saving_preferences_answers_with_the_confirmation(self):
        response = self.client.post(
            reverse("student:settings"),
            {"action": "preferences", "email_enabled": "on"},
            HTTP_X_APP_PANEL="1",
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Appearance")          # the panel came back
        self.assertContains(response, "preferences have been saved")

    def test_saving_a_profile_answers_with_the_confirmation(self):
        response = self.client.post(
            reverse("student:profile"),
            {"action": "profile", "full_name": "Renamed Student", "email": "saver@test.local"},
            HTTP_X_APP_PANEL="1",
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "profile has been updated")
        self.student.refresh_from_db()
        self.assertEqual(self.student.full_name, "Renamed Student")

    def test_a_rejected_form_comes_back_with_its_errors_and_the_status_to_match(self):
        response = self.client.post(
            reverse("student:profile"),
            {"action": "profile", "full_name": "", "email": "not-an-email"},
            HTTP_X_APP_PANEL="1",
        )
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, "check the highlighted fields", status_code=400)
        self.assertContains(response, "is-bad", status_code=400)

    def test_a_support_request_confirms_inside_the_help_panel(self):
        response = self.client.post(
            reverse("student:submit_request"),
            {
                "full_name": "Panel Student",
                "email": "saver@test.local",
                "category": "chatbot",
                "subject": "The sidebar will not close",
                "description": "It stays half open on my phone after I tap a link.",
            },
            HTTP_X_APP_PANEL="1",
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "request has been sent")
        self.assertContains(response, "Ask us anything")


class PricingPanelTests(TestCase):
    def setUp(self):
        settings_service.bootstrap()
        self.student = make_student("shopper")
        self.client.force_login(self.student)

    def test_every_model_on_sale_is_offered_with_three_packages(self):
        """What is offered, is offered properly.

        Not every active model is on sale: one whose tokens the administrator
        distributes by hand has no active packages, and the switch correctly
        omits it rather than opening on an empty group. So the rule is about
        the models that *are* offered, not about the whole catalogue.
        """
        response = self.client.get(reverse("student:pricing"), HTTP_X_APP_PANEL="1")
        groups = response.context["groups"]

        on_sale = set(
            Plan.objects.filter(is_active=True, ai_model__isnull=False)
            .values_list("ai_model__key", flat=True)
        )
        self.assertEqual({group["key"] for group in groups}, on_sale)
        self.assertTrue(groups, "at least one model must be purchasable")

        for group in groups:
            with self.subTest(model=group["key"]):
                self.assertEqual(len(group["plans"]), 3)
                # A package has to say which model's tokens it sells, or buying
                # one credits a balance the student was not topping up.
                for plan in group["plans"]:
                    self.assertEqual(plan.ai_model.key, group["key"])

    def test_a_model_with_no_active_packages_is_not_offered(self):
        """An admin-distributed model must not open on an empty pricing group.

        The chat picker still lists it and tells the student to contact the
        administrator, which is the route to getting some.
        """
        offered = {
            group["key"]
            for group in self.client.get(
                reverse("student:pricing"), HTTP_X_APP_PANEL="1"
            ).context["groups"]
        }
        for model in AIModel.objects.filter(is_active=True):
            has_packages = Plan.objects.filter(is_active=True, ai_model=model).exists()
            with self.subTest(model=model.key):
                self.assertEqual(model.key in offered, has_packages)

    def test_a_dearer_model_sells_fewer_tokens_for_the_same_money(self):
        """Identical amounts at identical prices would either give Sonnet away
        or make GPT-5 Mini a bad deal."""
        def tokens_at(key, price):
            return Plan.objects.get(ai_model__key=key, price=price).tokens

        self.assertGreater(tokens_at("gpt-5-mini", 5), tokens_at("claude-haiku-4-5", 5))
        self.assertGreater(tokens_at("claude-haiku-4-5", 5), tokens_at("claude-sonnet-5", 5))

    def test_a_bigger_package_is_better_value_per_token(self):
        for key in ("gpt-5-mini", "claude-haiku-4-5", "claude-sonnet-5"):
            with self.subTest(model=key):
                plans = list(Plan.objects.filter(ai_model__key=key, is_active=True).order_by("price"))
                rates = [plan.price_per_million for plan in plans]
                self.assertEqual(rates, sorted(rates, reverse=True))

    def test_the_focused_model_is_carried_through_from_the_warning(self):
        response = self.client.get(
            reverse("student:pricing") + "?model=claude-sonnet-5", HTTP_X_APP_PANEL="1"
        )
        self.assertEqual(response.context["focus_model"], "claude-sonnet-5")
        self.assertContains(response, 'data-focus="1"')


class UsagePanelTests(TestCase):
    def setUp(self):
        settings_service.bootstrap()
        self.student = make_student("spender")
        fund(self.student, 4_000, GPT)
        self.client.force_login(self.student)

    def test_balances_are_reported_per_model_and_never_summed(self):
        response = self.client.get(reverse("student:usage"), HTTP_X_APP_PANEL="1")
        wallets = {row["key"]: row for row in response.context["wallets"]}

        self.assertEqual(wallets[GPT]["balance"], 4_000)
        self.assertEqual(wallets["claude-sonnet-5"]["balance"], 0)
        self.assertTrue(wallets["claude-sonnet-5"]["is_empty"])
        self.assertFalse(wallets[GPT]["is_empty"])

    def test_a_balance_at_the_threshold_is_flagged_as_low(self):
        threshold = int(settings_service.get("low_token_threshold"))
        student = make_student("nearlyout")
        fund(student, threshold, GPT)
        self.client.force_login(student)

        response = self.client.get(reverse("student:usage"), HTTP_X_APP_PANEL="1")
        row = next(r for r in response.context["wallets"] if r["key"] == GPT)
        self.assertTrue(row["is_low"], "at the threshold, not just below it")
        self.assertFalse(row["is_empty"])
        self.assertContains(response, "Upgrade plan")


class FreeGrantTests(TestCase):
    def setUp(self):
        settings_service.bootstrap()

    def test_a_new_account_can_send_its_first_message_on_gpt_5_mini_only(self):
        """The grant is only meaningful if it clears the floor a request has to pass."""
        from django.conf import settings as django_settings

        grant = int(settings_service.get("free_trial_tokens"))
        self.assertEqual(grant, 3000)
        self.assertGreaterEqual(grant, django_settings.AI_MIN_REQUEST_TOKENS)

        student = make_student("fresh")
        from AI.services import referral_service

        referral_service.grant_welcome_tokens(student)
        # GPT-5 Mini holds the whole trial; every other model starts at zero.
        for ai_model in AIModel.objects.filter(is_active=True):
            with self.subTest(model=ai_model.key):
                expected = grant if ai_model.key == "gpt-5-mini" else 0
                self.assertEqual(wallet_service.balance_of(student, ai_model), expected)

    def test_a_new_student_opens_the_chat_on_gpt_5_mini(self):
        from AI.services import referral_service

        student = make_student("first.login")
        referral_service.grant_welcome_tokens(student)
        self.client.force_login(student)
        page = self.client.get("/chat/")
        self.assertEqual(page.context["active_model"].key, "gpt-5-mini")
        # The browser's remembered model is per account, never browser-wide.
        self.assertContains(page, f"modelMemory: 'lumen-model:{student.pk}'")
        self.assertNotContains(page, "localStorage.getItem('lumen-model')")

    def test_the_grant_does_not_touch_another_student(self):
        first, second = make_student("one"), make_student("two")
        from AI.services import referral_service

        referral_service.grant_welcome_tokens(first)
        self.assertEqual(wallet_service.balance_of(second, model_for(GPT)), 0)
