"""Staff roles: each administrator reaches only the console areas their role allows.

The matrix lives in AI/services/staff_permissions.py and is enforced by
AI.middleware.StaffAreaMiddleware. These tests check the map is complete, that
each role gets 200s and 403s where it should (pages, form posts and JSON),
that the navigation and shared pages only offer what a role can use, and the
Staff page's own rules.
"""
from __future__ import annotations

import json
from html.parser import HTMLParser

from django.contrib.auth.hashers import check_password
from django.test import TestCase
from django.urls import URLPattern, URLResolver, get_resolver, reverse

from AI.models import (
    AccountStatus,
    AuditAction,
    AuditLog,
    Plan,
    Role,
    StaffRole,
    Subscription,
    User,
)
from AI.services import payment_service, settings_service, staff_permissions, support_service
from AI.tests.helpers import GPT, balance, fund_pool, model_for

PASSWORD = "Sup3rSecret!x"
XHR = {"HTTP_X_REQUESTED_WITH": "XMLHttpRequest"}
#: The student page's Manage tokens button (the page script names the selector too).
GRANT_BUTTON = 'btn-sm" data-open-grant>'


def console_route_names() -> set[str]:
    """Every named route inside the console namespace, read from the resolver."""
    names: set[str] = set()

    def walk(patterns, namespace):
        for pattern in patterns:
            if isinstance(pattern, URLResolver):
                inner = pattern.namespace or namespace
                walk(pattern.url_patterns, inner)
            elif isinstance(pattern, URLPattern) and pattern.name and namespace == "console":
                names.add(f"console:{pattern.name}")

    walk(get_resolver().url_patterns, "")
    return names


def make_staff(username: str, role: str = "", **extra) -> User:
    return User.objects.create_user(
        username=username, email=f"{username}@staff.test", password=PASSWORD,
        full_name=username.replace(".", " ").title(),
        role=Role.ADMIN, staff_role=role, status=AccountStatus.ACTIVE, is_verified=True, **extra,
    )


# ---------------------------------------------------------------------------
# The map itself
# ---------------------------------------------------------------------------
class RouteMapTests(TestCase):
    def test_every_console_route_has_an_area(self):
        routes = console_route_names()
        self.assertGreater(len(routes), 50)  # the walk really found the console
        missing = sorted(routes - set(staff_permissions.ROUTE_AREAS))
        self.assertEqual(missing, [], "Add these routes to ROUTE_AREAS in AI/services/staff_permissions.py")

    def test_the_map_names_no_route_that_does_not_exist(self):
        stale = sorted(set(staff_permissions.ROUTE_AREAS) - console_route_names())
        self.assertEqual(stale, [])

    def test_every_area_named_anywhere_is_a_real_area(self):
        areas = staff_permissions.ALL_AREAS
        self.assertLessEqual(set(staff_permissions.ROUTE_AREAS.values()), areas)
        for role, reach in staff_permissions.ROLE_AREAS.items():
            self.assertLessEqual(reach, areas, role)
        for table in (staff_permissions.ACTION_AREAS, staff_permissions.KWARG_AREAS):
            for route, (_field, mapping) in table.items():
                self.assertIn(route, staff_permissions.ROUTE_AREAS)
                self.assertLessEqual(set(mapping.values()), areas, route)
        self.assertLessEqual(staff_permissions.USER_TARGET_ROUTES, set(staff_permissions.ROUTE_AREAS))

    def test_every_role_has_a_row_and_the_owner_has_everything(self):
        self.assertEqual(set(staff_permissions.ROLE_AREAS), set(StaffRole.values))
        self.assertEqual(staff_permissions.ROLE_AREAS[StaffRole.OWNER], staff_permissions.ALL_AREAS)
        for role in StaffRole.values:
            self.assertLessEqual(staff_permissions.EVERYONE, staff_permissions.ROLE_AREAS[role])

    def test_only_owners_reach_staff_and_settings(self):
        for role, reach in staff_permissions.ROLE_AREAS.items():
            if role != StaffRole.OWNER:
                self.assertNotIn("staff", reach, role)
                self.assertNotIn("settings", reach, role)

    def test_an_unmapped_route_is_owner_only(self):
        self.assertEqual(staff_permissions.area_of("console:not_a_real_route"), "staff")


class RoleOfTests(TestCase):
    def test_superuser_is_always_an_owner(self):
        root = User.objects.create_superuser(username="root.ro", email="root.ro@test.local", password=PASSWORD)
        User.objects.filter(pk=root.pk).update(staff_role=StaffRole.SUPPORT)
        root.refresh_from_db()
        self.assertEqual(staff_permissions.role_of(root), StaffRole.OWNER)
        self.assertTrue(staff_permissions.can(root, "settings"))

    def test_an_admin_without_a_role_gets_only_what_everyone_has(self):
        admin = make_staff("blank.ro")
        self.assertEqual(staff_permissions.role_of(admin), staff_permissions.UNASSIGNED)
        self.assertEqual(staff_permissions.areas_for(admin), staff_permissions.EVERYONE)
        self.assertFalse(staff_permissions.can(admin, "students"))

    def test_students_have_no_role_and_no_areas(self):
        student = User.objects.create_user(username="pupil.ro", email="pupil.ro@test.local", password=PASSWORD)
        self.assertIsNone(staff_permissions.role_of(student))
        self.assertEqual(staff_permissions.areas_for(student), frozenset())

    def test_each_role_reaches_its_areas(self):
        self.assertTrue(staff_permissions.can(make_staff("s.ro", StaffRole.SUPPORT), "support"))
        self.assertFalse(staff_permissions.can(make_staff("s2.ro", StaffRole.SUPPORT), "finance"))
        self.assertTrue(staff_permissions.can(make_staff("f.ro", StaffRole.FINANCE), "finance"))
        self.assertFalse(staff_permissions.can(make_staff("c.ro", StaffRole.CONTENT), "students"))
        self.assertTrue(staff_permissions.can(make_staff("o.ro", StaffRole.OPERATIONS), "tokens"))


# ---------------------------------------------------------------------------
# Who gets a page and who gets a 403
# ---------------------------------------------------------------------------
class RoleFixture(TestCase):
    def setUp(self):
        settings_service.bootstrap()
        self.owner = make_staff("owner.one", StaffRole.OWNER)
        self.support = make_staff("support.one", StaffRole.SUPPORT)
        self.finance = make_staff("finance.one", StaffRole.FINANCE)
        self.content = make_staff("content.one", StaffRole.CONTENT)
        self.operations = make_staff("ops.one", StaffRole.OPERATIONS)
        self.blank = make_staff("blank.one")
        self.student = User.objects.create_user(
            username="pupil.one", email="pupil.one@test.local", password=PASSWORD,
            full_name="Pupil One", is_verified=True, status=AccountStatus.ACTIVE,
        )

    def as_(self, user):
        self.client.logout()
        self.client.force_login(user)
        return self.client

    def status(self, user, name, *args, **params):
        return self.as_(user).get(reverse(name, args=args), params).status_code


class PageAccessTests(RoleFixture):
    MATRIX = {
        "support": {
            200: ["console:dashboard", "console:users", "console:tickets", "console:conversations",
                  "console:canned_replies", "console:low_tokens", "console:telegram"],
            403: ["console:payments", "console:plans", "console:tokens", "console:ai_tokens",
                  "console:notifications", "console:health", "console:settings", "console:staff",
                  "console:audit_logs", "console:accounts", "console:reports"],
        },
        "finance": {
            200: ["console:dashboard", "console:payments", "console:plans", "console:promos",
                  "console:subscriptions", "console:reports", "console:referrals", "console:users",
                  "console:model_costs", "console:gateways"],
            403: ["console:tickets", "console:conversations", "console:notifications", "console:tokens",
                  "console:health", "console:settings", "console:staff", "console:accounts"],
        },
        "content": {
            200: ["console:dashboard", "console:notifications", "console:prompt_library", "console:quality",
                  "console:telegram"],
            403: ["console:users", "console:payments", "console:tickets", "console:tokens",
                  "console:health", "console:settings", "console:staff", "console:low_tokens"],
        },
        "operations": {
            200: ["console:dashboard", "console:health", "console:model_status", "console:emails",
                  "console:exports", "console:tokens", "console:ai_tokens", "console:usage_limits",
                  "console:usage", "console:users"],
            403: ["console:payments", "console:tickets", "console:notifications", "console:settings",
                  "console:staff", "console:audit_logs", "console:accounts"],
        },
        "owner": {
            200: ["console:dashboard", "console:staff", "console:settings", "console:audit_logs",
                  "console:payments", "console:tickets", "console:accounts", "console:tokens"],
            403: [],
        },
        "blank": {
            200: ["console:dashboard", "console:telegram"],
            403: ["console:users", "console:payments", "console:tickets", "console:tokens",
                  "console:notifications", "console:staff"],
        },
    }

    def test_each_role_reaches_its_pages_and_no_others(self):
        for who, expected in self.MATRIX.items():
            user = getattr(self, who)
            for code, names in expected.items():
                for name in names:
                    with self.subTest(role=who, route=name):
                        self.assertEqual(self.status(user, name), code)

    def test_the_403_page_is_the_console_saying_ask_an_owner(self):
        response = self.as_(self.support).get(reverse("console:payments"))
        self.assertEqual(response.status_code, 403)
        html = response.content.decode()
        self.assertIn("You don&#x27;t have access to this area", html)
        self.assertIn("ask an owner", html)
        self.assertIn("Finance", html)                   # the area
        self.assertIn(self.owner.display_name, html)      # who to ask
        self.assertIn('id="consoleRail"', html)           # still inside the console

    def test_xhr_gets_json_not_a_page(self):
        response = self.as_(self.support).get(reverse("console:health_data"), **XHR)
        self.assertEqual(response.status_code, 403)
        payload = json.loads(response.content)
        self.assertEqual(payload["code"], "forbidden_area")
        self.assertEqual(payload["area"], "operations")
        self.assertFalse(payload["ok"])

    def test_an_owner_who_is_not_a_superuser_reaches_everything(self):
        for name in ("console:staff", "console:settings", "console:health", "console:notifications"):
            self.assertEqual(self.status(self.owner, name), 200, name)

    def test_the_audit_export_is_for_owners(self):
        self.assertEqual(self.status(self.operations, "console:export_download", "audit"), 403)
        self.assertEqual(self.status(self.operations, "console:export_download", "tickets"), 200)
        self.assertEqual(self.status(self.owner, "console:export_download", "audit"), 200)


class NonAdminTests(RoleFixture):
    def test_anonymous_visitors_still_go_to_the_admin_login(self):
        self.client.logout()
        for name in ("console:staff", "console:payments", "console:dashboard"):
            response = self.client.get(reverse(name))
            self.assertEqual(response.status_code, 302, name)
            self.assertIn("/admin/login/", response["Location"], name)

    def test_students_still_bounce_to_the_chat(self):
        for name in ("console:staff", "console:payments", "console:dashboard"):
            response = self.as_(self.student).get(reverse(name))
            self.assertEqual(response.status_code, 302, name)
            self.assertIn("/chat/", response["Location"], name)

    def test_the_admin_sign_in_page_is_untouched(self):
        self.client.logout()
        self.assertEqual(self.client.get(reverse("auth:admin_login")).status_code, 200)


# ---------------------------------------------------------------------------
# Posts are blocked as well as pages
# ---------------------------------------------------------------------------
class PostBlockingTests(RoleFixture):
    def test_support_cannot_confirm_a_payment(self):
        plan = Plan.objects.create(name="Role plan", tokens=1_000, price=1, duration_days=30)
        payment = payment_service.create_payment(self.student, plan, "aba_payway")
        response = self.as_(self.support).post(
            reverse("console:payment_action", args=[payment.reference]), {"action": "confirm"}
        )
        self.assertEqual(response.status_code, 403)
        payment.refresh_from_db()
        self.assertEqual(payment.status, "pending")

    def test_support_cannot_hand_out_tokens(self):
        model = fund_pool(GPT)
        response = self.as_(self.support).post(reverse("console:ai_distribute_apply"), {
            "model": model.pk, "student": self.student.pk, "user_id": self.student.pk,
            "action": "add", "amount": 5_000, "reason": "nope",
        })
        self.assertEqual(response.status_code, 403)
        self.assertEqual(balance(self.student), 0)

    def test_content_cannot_disable_a_student(self):
        response = self.as_(self.content).post(reverse("console:user_toggle", args=[self.student.pk]))
        self.assertEqual(response.status_code, 403)
        self.student.refresh_from_db()
        self.assertTrue(self.student.is_active_account)

    def test_finance_cannot_message_a_student_but_can_grant_a_plan(self):
        response = self.as_(self.finance).post(
            reverse("console:student_message", args=[self.student.pk]), {"title": "Hi", "body": "Hello"}
        )
        self.assertEqual(response.status_code, 403)
        plan = Plan.objects.create(name="Granted", tokens=0, price=5, duration_days=30, ai_model=model_for(GPT))
        response = self.as_(self.finance).post(
            reverse("console:student_grant_plan", args=[self.student.pk]), {"plan_id": plan.pk}
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Subscription.objects.filter(user=self.student, plan=plan).exists())

    def test_support_cannot_grant_a_plan(self):
        plan = Plan.objects.create(name="Not yours", tokens=0, price=5, duration_days=30)
        response = self.as_(self.support).post(
            reverse("console:student_grant_plan", args=[self.student.pk]), {"plan_id": plan.pk}
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(Subscription.objects.filter(user=self.student).exists())

    def test_the_bulk_bar_is_split_by_what_it_does(self):
        client = self.as_(self.support)
        tokens = client.post(reverse("console:users_bulk"), {
            "action": "tokens", "ids": [self.student.pk], "confirm": "1", "model": model_for(GPT).pk, "amount": "500",
        })
        self.assertEqual(tokens.status_code, 403)
        export = client.post(reverse("console:users_bulk"), {"action": "export", "ids": [self.student.pk]})
        self.assertEqual(export.status_code, 403)
        tag = client.post(reverse("console:users_bulk"), {
            "action": "tag", "ids": [self.student.pk], "confirm": "1", "add_tag": "vip",
        })
        self.assertEqual(tag.status_code, 302)
        self.student.refresh_from_db()
        self.assertIn("vip", self.student.tags)

    def test_expiring_access_reminds_for_support_but_extends_only_for_finance(self):
        plan = Plan.objects.create(name="Expiring", tokens=0, price=5, duration_days=30)
        from django.utils import timezone

        sub = Subscription.objects.create(
            user=self.student, plan=plan, starts_at=timezone.now(), ends_at=timezone.now() + timezone.timedelta(days=2),
        )
        url = reverse("console:expiring_action", args=[sub.pk])
        self.assertEqual(self.as_(self.support).post(url, {"action": "extend", "days": 30}).status_code, 403)
        self.assertEqual(self.as_(self.support).post(url, {"action": "remind"}).status_code, 302)
        self.assertEqual(self.as_(self.finance).post(url, {"action": "extend", "days": 30}).status_code, 302)

    def test_everyone_can_manage_their_own_sign_in_codes_but_not_the_bot(self):
        client = self.as_(self.content)
        own = client.post(reverse("console:telegram_action"), {"action": "login_otp", "enabled": "on"})
        self.assertEqual(own.status_code, 302)
        self.content.refresh_from_db()
        self.assertTrue(self.content.login_otp_enabled)
        setup = client.post(reverse("console:telegram_action"), {"action": "register_webhook"})
        self.assertEqual(setup.status_code, 403)

    def test_xhr_posts_get_json_too(self):
        response = self.as_(self.content).post(
            reverse("console:user_toggle", args=[self.student.pk]), **XHR
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(json.loads(response.content)["code"], "forbidden_area")


class StaffAccountProtectionTests(RoleFixture):
    def test_support_cannot_open_or_touch_an_administrator(self):
        client = self.as_(self.support)
        self.assertEqual(client.get(reverse("console:user_detail", args=[self.owner.pk])).status_code, 403)
        response = client.post(reverse("console:user_reset_password", args=[self.owner.pk]))
        self.assertEqual(response.status_code, 403)
        self.owner.refresh_from_db()
        self.assertTrue(check_password(PASSWORD, self.owner.password))
        self.assertEqual(client.post(reverse("console:user_toggle", args=[self.finance.pk])).status_code, 403)
        self.finance.refresh_from_db()
        self.assertTrue(self.finance.is_active_account)

    def test_support_can_still_help_a_student(self):
        client = self.as_(self.support)
        self.assertEqual(client.get(reverse("console:user_detail", args=[self.student.pk])).status_code, 200)
        response = client.post(reverse("console:user_reset_password", args=[self.student.pk]))
        self.assertEqual(response.status_code, 302)

    def test_an_owner_can_open_a_colleague(self):
        self.assertEqual(self.status(self.owner, "console:user_detail", self.support.pk), 200)

    def test_only_an_owner_makes_someone_an_administrator(self):
        payload = {
            "username": self.student.username, "full_name": "Pupil One", "email": self.student.email,
            "phone": "", "source": self.student.source, "role": Role.ADMIN, "status": AccountStatus.ACTIVE,
            "notes": "",
        }
        response = self.as_(self.support).post(reverse("console:user_edit", args=[self.student.pk]), payload)
        self.assertEqual(response.status_code, 403)
        self.student.refresh_from_db()
        self.assertEqual(self.student.role, Role.STUDENT)

        payload["role"] = Role.STUDENT
        payload["full_name"] = "Pupil Renamed"
        response = self.as_(self.support).post(reverse("console:user_edit", args=[self.student.pk]), payload)
        self.assertEqual(response.status_code, 302)
        self.student.refresh_from_db()
        self.assertEqual(self.student.full_name, "Pupil Renamed")


class SearchTests(RoleFixture):
    def setUp(self):
        super().setUp()
        self.student.full_name = "Zebra Student"
        self.student.save(update_fields=["full_name"])
        support_service.create_ticket(self.student, data={
            "category": "other", "subject": "Zebra trouble", "description": "Something about zebras."})

    def groups(self, user, q="zebra"):
        response = self.as_(user).get(reverse("console:search"), {"q": q}, **XHR)
        self.assertEqual(response.status_code, 200)
        return {group["label"] for group in json.loads(response.content)["groups"]}

    def test_results_only_list_what_the_role_can_open(self):
        self.assertEqual(self.groups(self.owner), {"Students", "Support tickets"})
        self.assertEqual(self.groups(self.support), {"Students", "Support tickets"})
        self.assertEqual(self.groups(self.finance), {"Students"})
        self.assertEqual(self.groups(self.content), set())

    def test_staff_accounts_are_left_out_for_non_owners(self):
        response = self.as_(self.support).get(reverse("console:search"), {"q": "owner.one"}, **XHR)
        self.assertEqual(json.loads(response.content)["groups"], [])
        response = self.as_(self.owner).get(reverse("console:search"), {"q": "owner.one"}, **XHR)
        self.assertEqual(len(json.loads(response.content)["groups"]), 1)


# ---------------------------------------------------------------------------
# What each role is shown
# ---------------------------------------------------------------------------
class NavigationTests(RoleFixture):
    def html(self, user, name="console:dashboard", *args):
        response = self.as_(user).get(reverse(name, args=args))
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_the_sidebar_only_lists_reachable_pages(self):
        cases = {
            "support": (["console:users", "console:tickets", "console:conversations"],
                        ["console:payments", "console:settings", "console:staff", "console:tokens",
                         "console:notifications", "console:accounts"]),
            "finance": (["console:payments", "console:plans", "console:users", "console:referrals"],
                        ["console:tickets", "console:settings", "console:staff", "console:notifications"]),
            "content": (["console:notifications", "console:prompt_library", "console:quality"],
                        ["console:users", "console:payments", "console:tickets", "console:staff"]),
            "operations": (["console:health", "console:tokens", "console:emails", "console:exports"],
                           ["console:payments", "console:tickets", "console:settings", "console:staff"]),
            "owner": (["console:staff", "console:settings", "console:audit_logs", "console:payments"], []),
        }
        for who, (shown, hidden) in cases.items():
            html = self.html(getattr(self, who))
            for name in shown:
                with self.subTest(role=who, shown=name):
                    self.assertIn(f'href="{reverse(name)}"', html)
            for name in hidden:
                with self.subTest(role=who, hidden=name):
                    self.assertNotIn(f'href="{reverse(name)}"', html)

    def test_every_link_in_the_sidebar_opens(self):
        """The ⌘K palette is built from these links, so they must all be reachable."""
        import re

        for who in ("support", "finance", "content", "operations", "blank"):
            user = getattr(self, who)
            html = self.html(user)
            rail = html[html.index('id="consoleRail"'):html.index("</aside>", html.index('id="consoleRail"'))]
            for href in set(re.findall(r'href="(/admin/[^"#?]*)"', rail)):
                with self.subTest(role=who, href=href):
                    self.assertEqual(self.client.get(href).status_code, 200)

    def test_the_bell_only_offers_what_the_role_handles(self):
        finance = self.html(self.finance)
        self.assertIn(f'href="{reverse("console:payments")}?status=pending"', finance)
        self.assertNotIn(f'href="{reverse("console:tickets")}?status=open"', finance)
        support = self.html(self.support)
        self.assertIn(f'href="{reverse("console:tickets")}?status=open"', support)
        self.assertNotIn(f'href="{reverse("console:payments")}?status=pending"', support)

    def test_the_dashboard_hides_money_and_actions_from_other_roles(self):
        content = self.html(self.content)
        self.assertNotIn("Recent payments", content)
        self.assertNotIn(reverse("console:ai_distribute"), content)
        self.assertNotIn(reverse("console:accounts"), content)
        self.assertNotIn(reverse("console:audit_logs"), content)
        finance = self.html(self.finance)
        self.assertIn("Recent payments", finance)
        owner = self.html(self.owner)
        self.assertIn(reverse("console:accounts"), owner)
        self.assertIn(reverse("console:audit_logs"), owner)

    def test_the_sidebar_names_the_role(self):
        self.assertIn(">Support</span>", self.html(self.support))
        self.assertIn("no console role yet", self.html(self.blank))

    def test_a_student_page_only_offers_the_role_its_own_actions(self):
        detail = ("console:user_detail", self.student.pk)
        finance = self.html(self.finance, *detail)
        self.assertIn('data-open-modal="planModal"', finance)
        self.assertNotIn(GRANT_BUTTON, finance)
        self.assertNotIn('data-open-modal="messageModal"', finance)
        self.assertNotIn(reverse("console:user_edit", args=[self.student.pk]), finance)
        self.assertNotIn(reverse("console:user_reset_password", args=[self.student.pk]), finance)

        support = self.html(self.support, *detail)
        self.assertIn('data-open-modal="messageModal"', support)
        self.assertIn(reverse("console:user_edit", args=[self.student.pk]), support)
        self.assertIn(reverse("console:student_note_add", args=[self.student.pk]), support)
        self.assertNotIn('data-open-modal="planModal"', support)
        self.assertNotIn(GRANT_BUTTON, support)
        self.assertNotIn(reverse("console:student_grant_plan", args=[self.student.pk]), support)

        operations = self.html(self.operations, *detail)
        self.assertIn(GRANT_BUTTON, operations)
        self.assertNotIn('data-open-modal="planModal"', operations)
        self.assertNotIn(reverse("console:student_note_add", args=[self.student.pk]), operations)

        owner = self.html(self.owner, *detail)
        for marker in (GRANT_BUTTON, 'data-open-modal="planModal"', 'data-open-modal="messageModal"'):
            self.assertIn(marker, owner)


# ---------------------------------------------------------------------------
# The Staff page
# ---------------------------------------------------------------------------
class StaffPageTests(RoleFixture):
    def test_it_lists_every_administrator_and_no_student(self):
        root = User.objects.create_superuser(username="root.staff", email="root.staff@test.local", password=PASSWORD)
        response = self.as_(self.owner).get(reverse("console:staff"))
        members = {member.username for member in response.context["members"]}
        self.assertEqual(members, {"owner.one", "support.one", "finance.one", "content.one", "ops.one",
                                   "blank.one", root.username})
        self.assertNotContains(response, "pupil.one")
        self.assertContains(response, "What each role can open")
        self.assertContains(response, "no role yet")

    def test_an_owner_creates_a_staff_account_with_a_one_time_password(self):
        client = self.as_(self.owner)
        response = client.post(reverse("console:staff_create"), {
            "username": "new.finance", "full_name": "New Finance", "email": "new.finance@staff.test",
            "staff_role": StaffRole.FINANCE,
        })
        self.assertRedirects(response, reverse("console:staff"), fetch_redirect_response=False)
        member = User.objects.get(username="new.finance")
        self.assertEqual(member.role, Role.ADMIN)
        self.assertEqual(member.staff_role, StaffRole.FINANCE)
        self.assertTrue(member.must_change_password)
        self.assertEqual(member.created_by, self.owner)
        self.assertTrue(AuditLog.objects.filter(
            action=AuditAction.USER_CREATE, target_id=str(member.pk), actor=self.owner).exists())
        self.assertTrue(AuditLog.objects.filter(
            action=AuditAction.USER_UPDATE, target_id=str(member.pk), new_value="staff_role=finance").exists())

        page = client.get(reverse("console:staff"))
        password = page.context["handover"]["password"]
        self.assertTrue(check_password(password, member.password))
        self.assertContains(page, password)
        again = client.get(reverse("console:staff"))
        self.assertIsNone(again.context["handover"])
        self.assertNotContains(again, password)

    def test_a_taken_username_or_email_creates_nothing(self):
        client = self.as_(self.owner)
        before = User.objects.count()
        client.post(reverse("console:staff_create"), {
            "username": "SUPPORT.one", "full_name": "Dup", "email": "fresh@staff.test", "staff_role": "support"})
        client.post(reverse("console:staff_create"), {
            "username": "fresh.one", "full_name": "Dup", "email": "pupil.one@test.local", "staff_role": "support"})
        client.post(reverse("console:staff_create"), {
            "username": "fresh.two", "full_name": "Bad role", "email": "fresh2@staff.test", "staff_role": "king"})
        self.assertEqual(User.objects.count(), before)

    def test_changing_a_role_is_audited_and_takes_effect_at_once(self):
        response = self.as_(self.owner).post(
            reverse("console:staff_role", args=[self.support.pk]), {"staff_role": StaffRole.FINANCE})
        self.assertEqual(response.status_code, 302)
        self.support.refresh_from_db()
        self.assertEqual(self.support.staff_role, StaffRole.FINANCE)
        entry = AuditLog.objects.get(action=AuditAction.USER_UPDATE, target_id=str(self.support.pk))
        self.assertEqual((entry.old_value, entry.new_value), ("staff_role=support", "staff_role=finance"))
        self.assertEqual(self.status(self.support, "console:payments"), 200)
        self.assertEqual(self.status(self.support, "console:tickets"), 403)

    def test_a_role_can_be_given_to_an_admin_who_had_none(self):
        self.as_(self.owner).post(reverse("console:staff_role", args=[self.blank.pk]), {"staff_role": "content"})
        self.blank.refresh_from_db()
        self.assertEqual(self.blank.staff_role, StaffRole.CONTENT)
        self.assertEqual(self.status(self.blank, "console:notifications"), 200)

    def test_the_last_owner_cannot_demote_themselves(self):
        response = self.as_(self.owner).post(
            reverse("console:staff_role", args=[self.owner.pk]), {"staff_role": StaffRole.SUPPORT}, follow=True)
        self.owner.refresh_from_db()
        self.assertEqual(self.owner.staff_role, StaffRole.OWNER)
        self.assertContains(response, "You are the last owner")
        self.assertFalse(AuditLog.objects.filter(action=AuditAction.USER_UPDATE, target_id=str(self.owner.pk)).exists())

    def test_with_another_owner_you_may_step_down(self):
        make_staff("owner.two", StaffRole.OWNER)
        response = self.as_(self.owner).post(
            reverse("console:staff_role", args=[self.owner.pk]), {"staff_role": StaffRole.SUPPORT})
        self.assertRedirects(response, reverse("console:dashboard"), fetch_redirect_response=False)
        self.owner.refresh_from_db()
        self.assertEqual(self.owner.staff_role, StaffRole.SUPPORT)
        self.assertEqual(self.status(self.owner, "console:staff"), 403)

    def test_a_disabled_owner_does_not_count_as_the_other_owner(self):
        spare = make_staff("owner.gone", StaffRole.OWNER)
        spare.status = AccountStatus.DISABLED
        spare.is_active = False
        spare.save(update_fields=["status", "is_active"])
        self.as_(self.owner).post(reverse("console:staff_role", args=[self.owner.pk]), {"staff_role": "support"})
        self.owner.refresh_from_db()
        self.assertEqual(self.owner.staff_role, StaffRole.OWNER)

    def test_a_superuser_stays_an_owner(self):
        root = User.objects.create_superuser(username="root.keep", email="root.keep@test.local", password=PASSWORD)
        self.as_(self.owner).post(reverse("console:staff_role", args=[root.pk]), {"staff_role": StaffRole.CONTENT})
        root.refresh_from_db()
        self.assertEqual(staff_permissions.role_of(root), StaffRole.OWNER)
        self.assertNotEqual(root.staff_role, StaffRole.CONTENT)

    def test_you_cannot_disable_yourself(self):
        make_staff("owner.two", StaffRole.OWNER)
        self.as_(self.owner).post(reverse("console:staff_toggle", args=[self.owner.pk]))
        self.owner.refresh_from_db()
        self.assertTrue(self.owner.is_active_account)

    def test_disabling_and_enabling_staff_is_audited(self):
        client = self.as_(self.owner)
        client.post(reverse("console:staff_toggle", args=[self.content.pk]), {"reason": "Left the team"})
        self.content.refresh_from_db()
        self.assertFalse(self.content.is_active_account)
        self.assertEqual(self.content.disabled_reason, "Left the team")
        self.assertTrue(AuditLog.objects.filter(action=AuditAction.USER_DISABLE, target_id=str(self.content.pk)).exists())
        client.post(reverse("console:staff_toggle", args=[self.content.pk]))
        self.content.refresh_from_db()
        self.assertTrue(self.content.is_active_account)
        self.assertTrue(AuditLog.objects.filter(action=AuditAction.USER_ENABLE, target_id=str(self.content.pk)).exists())

    def test_students_are_not_staff_here(self):
        client = self.as_(self.owner)
        self.assertEqual(client.post(reverse("console:staff_role", args=[self.student.pk]),
                                     {"staff_role": "owner"}).status_code, 404)
        self.assertEqual(client.post(reverse("console:staff_toggle", args=[self.student.pk])).status_code, 404)
        self.student.refresh_from_db()
        self.assertEqual(self.student.role, Role.STUDENT)
        self.assertEqual(self.student.staff_role, "")

    def test_nobody_but_an_owner_can_use_it(self):
        before = User.objects.count()
        for who in ("support", "finance", "content", "operations", "blank"):
            user = getattr(self, who)
            with self.subTest(role=who):
                client = self.as_(user)
                self.assertEqual(client.get(reverse("console:staff")).status_code, 403)
                self.assertEqual(client.post(reverse("console:staff_create"), {
                    "username": f"sneaky.{who}", "full_name": "Sneaky", "email": f"sneaky.{who}@x.test",
                    "staff_role": "owner"}).status_code, 403)
                self.assertEqual(client.post(reverse("console:staff_role", args=[user.pk]),
                                             {"staff_role": "owner"}).status_code, 403)
        self.assertEqual(User.objects.count(), before)
        self.support.refresh_from_db()
        self.assertEqual(self.support.staff_role, StaffRole.SUPPORT)

    def test_the_page_prints_the_enforced_matrix(self):
        """The page prints the matrix the middleware enforces."""
        response = self.as_(self.owner).get(reverse("console:staff"))
        rows = {row["area"].key: row["cells"] for row in response.context["matrix"]}
        roles = list(staff_permissions.ROLES)
        self.assertTrue(rows["finance"][roles.index(StaffRole.FINANCE)])
        self.assertFalse(rows["finance"][roles.index(StaffRole.SUPPORT)])
        self.assertEqual(set(rows), staff_permissions.ALL_AREAS)


# ---------------------------------------------------------------------------
# Every control a role is shown must work for that role
# ---------------------------------------------------------------------------
class Controls(HTMLParser):
    """The console links and forms on a page, the way a browser would use them."""

    def __init__(self, html: str):
        super().__init__(convert_charrefs=True)
        self.hrefs: set[str] = set()
        self.forms: list[dict] = []
        self._form = None
        self._action_select = False
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "a" and a.get("href"):
            self.hrefs.add(a["href"])
        elif tag == "form":
            self._form = {"action": a.get("action") or "", "method": (a.get("method") or "get").lower(),
                          "actions": set(), "post": {}}
            self.forms.append(self._form)
        elif self._form is not None:
            name = a.get("name")
            if a.get("data-bulk-act"):
                self._form["actions"].add(a["data-bulk-act"])
            if name == "action" and tag in ("button", "input") and a.get("type") != "hidden":
                self._form["actions"].add(a.get("value") or "")
            elif name == "action" and tag == "input":
                self._form["post"]["action"] = a.get("value") or ""
            elif name == "action" and tag == "select":
                self._action_select = True
            elif tag == "option" and self._action_select and a.get("value"):
                self._form["actions"].add(a["value"])
            elif name == "role" and tag == "select":
                self._form["post"]["role"] = Role.STUDENT  # the value it shows for a student
            elif name and tag == "input" and a.get("type") == "hidden":
                self._form["post"][name] = a.get("value") or ""

    def handle_endtag(self, tag):
        if tag == "select":
            self._action_select = False
        elif tag == "form":
            self._form = None


class VisibleControlsWorkTests(RoleFixture):
    """A link or button a role can see must not lead to the 403 page.

    Each representative page is read as the role sees it; every console href
    on it is requested, and every form is judged by the same check the
    middleware applies — once per `action` the form can submit.
    """

    def setUp(self):
        super().setUp()
        from django.utils import timezone

        from AI.models import UserAIWallet

        model = model_for(GPT)
        UserAIWallet.objects.create(user=self.student, ai_model=model, balance=100, allocated_tokens=10_000,
                                    used_tokens=9_900, lifetime_allocated=10_000, lifetime_used=9_900)
        plan = Plan.objects.create(name="Controls plan", tokens=1_000, price=3, duration_days=30, ai_model=model)
        self.payment = payment_service.create_payment(self.student, plan, "aba_payway")
        Subscription.objects.create(user=self.student, plan=plan, starts_at=timezone.now(),
                                    ends_at=timezone.now() + timezone.timedelta(days=3))
        self.ticket = support_service.create_ticket(self.student, data={
            "category": "other", "subject": "Controls", "description": "Checking the buttons."})

    def pages(self):
        s, t, p = self.student.pk, self.ticket.reference, self.payment.reference
        return [
            "/admin/", "/admin/users/", f"/admin/users/{s}/", f"/admin/users/{s}/edit/", "/admin/users/?role=admin",
            "/admin/students/low-tokens/", "/admin/students/expiring/", "/admin/telegram/",
            f"/admin/tickets/{t}/", "/admin/tickets/", "/admin/payments/", f"/admin/payments/{p}/",
            "/admin/subscriptions/", "/admin/models/costs/", "/admin/referrals/", "/admin/payment-gateways/",
            "/admin/quality/", "/admin/health/", "/admin/exports/", "/admin/model-status/", "/admin/usage-limits/",
            "/admin/tokens/", "/admin/notifications/",
        ]

    def assert_controls_work(self, who: str):
        from urllib.parse import urljoin, urlsplit

        from django.http import QueryDict
        from django.urls import Resolver404, resolve

        user = getattr(self, who)
        client = self.as_(user)
        opened, statuses = 0, {}
        for url in self.pages():
            response = client.get(url)
            if response.status_code != 200:
                continue  # not one of this role's pages
            opened += 1
            controls = Controls(response.content.decode())
            for href in controls.hrefs:
                path = urlsplit(urljoin(url, href)).path
                if not path.startswith("/admin/"):
                    continue
                if path not in statuses:
                    statuses[path] = client.get(path).status_code
                with self.subTest(role=who, page=url, link=path):
                    self.assertNotEqual(statuses[path], 403)
            for form in controls.forms:
                target = urlsplit(urljoin(url, form["action"])).path if form["action"] else urlsplit(url).path
                try:
                    match = resolve(target)
                except Resolver404:
                    continue
                if match.namespace != "console":
                    continue
                if form["method"] == "get":
                    with self.subTest(role=who, page=url, form=target):
                        self.assertNotEqual(client.get(target).status_code, 403)
                    continue
                for action in (form["actions"] or {None}):
                    post = QueryDict(mutable=True)
                    post.update(form["post"])
                    if action is not None:
                        post["action"] = action
                    decision = staff_permissions.check(user, match.view_name, post=post, kwargs=match.kwargs)
                    with self.subTest(role=who, page=url, form=match.view_name, action=action):
                        self.assertTrue(decision.allowed)
        self.assertGreater(opened, 1, who)

    def test_support(self):
        self.assert_controls_work("support")

    def test_finance(self):
        self.assert_controls_work("finance")

    def test_content(self):
        self.assert_controls_work("content")

    def test_operations(self):
        self.assert_controls_work("operations")

    def test_no_role_yet(self):
        self.assert_controls_work("blank")

    def test_the_fixtures_really_show_the_controls(self):
        """Guard the guard: the owner sees the buttons the other roles must not."""
        html = self.as_(self.owner).get(reverse("console:low_tokens")).content.decode()
        self.assertIn(reverse("console:low_token_warn", args=[self.student.ai_wallets.first().pk]), html)
        html = self.as_(self.owner).get(reverse("console:expiring_tokens")).content.decode()
        self.assertIn('value="extend"', html)
        html = self.as_(self.owner).get(reverse("console:telegram")).content.decode()
        self.assertIn('value="login_otp"', html)
        html = self.as_(self.owner).get(reverse("console:ticket_detail", args=[self.ticket.reference])).content.decode()
        self.assertIn(reverse("console:payment_detail", args=[self.payment.reference]), html)

    def test_support_edits_a_student_without_seeing_the_role(self):
        page = self.as_(self.support).get(reverse("console:user_edit", args=[self.student.pk]))
        html = page.content.decode()
        self.assertNotIn('<select name="role"', html)
        self.assertIn('type="hidden" name="role" value="student"', html)
        response = self.client.post(reverse("console:user_edit", args=[self.student.pk]), {
            "username": self.student.username, "full_name": "Edited By Support", "email": self.student.email,
            "phone": "", "source": self.student.source, "role": Role.STUDENT,
            "status": AccountStatus.ACTIVE, "notes": "",
        })
        self.assertEqual(response.status_code, 302)
        self.student.refresh_from_db()
        self.assertEqual(self.student.full_name, "Edited By Support")
        owner_html = self.as_(self.owner).get(reverse("console:user_edit", args=[self.student.pk])).content.decode()
        self.assertIn('<select name="role"', owner_html)
        self.assertNotIn('type="hidden" name="role"', owner_html)
