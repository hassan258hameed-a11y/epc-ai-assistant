"""The console's buttons, dialogs and links reach something that works.

An audit found controls that rendered fine and did nothing: a model editor that
could not save, a Manage tokens dialog that posted to a page that only reads,
dialogs that could never open, links carrying a primary key where a model key
was expected. Page-load tests cannot see any of that, so wherever possible
these tests read the rendered page, build the request a browser would send
from it, and check what that request actually changed.
"""
from __future__ import annotations

import csv
import io
import json
from html.parser import HTMLParser

from django.contrib.sessions.models import Session
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, RequestFactory, TestCase, override_settings
from django.urls import reverse

from AI.forms import AIModelForm
from AI.models import (
    AccountStatus,
    AIModel,
    AIProvider,
    AuditAction,
    AuditLog,
    Conversation,
    ModelPoolTransaction,
    Role,
    User,
    UserAIWallet,
)
from AI.services import model_pool_service, model_scope, support_service
from AI.tests.helpers import GPT, SONNET, balance, fund, fund_pool, model_for
from AI.utils.redirects import next_url, safe_next

PASSWORD = "Sup3rSecret!x"


# ---------------------------------------------------------------------------
# Reading a page the way a browser does
# ---------------------------------------------------------------------------
class Page(HTMLParser):
    """Every form, link and tag on a rendered page."""

    def __init__(self, html: str):
        super().__init__(convert_charrefs=True)
        self.forms: list[dict] = []
        self.anchors: list[dict] = []
        self.tags: list[tuple[str, dict]] = []
        self._form = None
        self._select = None
        self._option = None
        self._textarea = None
        self._anchor = None
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.tags.append((tag, attrs))
        if tag == "form":
            self._form = {
                "id": attrs.get("id", ""),
                "action": attrs.get("action", ""),
                "method": (attrs.get("method") or "get").lower(),
                "controls": [],
            }
            self.forms.append(self._form)
        elif tag == "a":
            self._anchor = {"href": attrs.get("href", ""), "text": "", "attrs": attrs}
            self.anchors.append(self._anchor)
        if self._form is None:
            return
        if tag == "input":
            self._form["controls"].append(("input", attrs))
        elif tag == "select":
            self._select = {"name": attrs.get("name"), "attrs": attrs, "options": []}
            self._form["controls"].append(("select", self._select))
        elif tag == "option" and self._select is not None:
            self._option = {"attrs": attrs, "text": ""}
            self._select["options"].append(self._option)
        elif tag == "textarea":
            self._textarea = {"name": attrs.get("name"), "text": ""}
            self._form["controls"].append(("textarea", self._textarea))

    def handle_endtag(self, tag):
        if tag == "form":
            self._form = None
        elif tag == "select":
            self._select = None
        elif tag == "option":
            self._option = None
        elif tag == "textarea":
            self._textarea = None
        elif tag == "a":
            self._anchor = None

    def handle_data(self, data):
        if self._textarea is not None:
            self._textarea["text"] += data
        if self._option is not None:
            self._option["text"] += data
        if self._anchor is not None:
            self._anchor["text"] += data

    # -- lookups -----------------------------------------------------------
    def form(self, *, id: str = "", action: str = "", field: str = "") -> dict:
        """The form with this id, this action, or a control of this name."""
        for form in self.forms:
            names = {
                control.get("name") if kind == "input" else control["name"]
                for kind, control in form["controls"]
            }
            if (id and form["id"] == id) or (action and form["action"] == action) or (field and field in names):
                return form
        raise AssertionError(f"No form with id={id!r} action={action!r} field={field!r} on the page")

    def anchor(self, text: str) -> dict:
        for anchor in self.anchors:
            if " ".join(anchor["text"].split()) == text:
                return anchor
        raise AssertionError(f"No link reading {text!r} on the page")

    def with_attr(self, name: str) -> list[dict]:
        return [attrs for _tag, attrs in self.tags if name in attrs]


def option_values(form: dict, name: str) -> list[str]:
    for kind, control in form["controls"]:
        if kind == "select" and control["name"] == name:
            return [
                option["attrs"].get("value", option["text"].strip()) for option in control["options"]
            ]
    raise AssertionError(f"No <select name={name!r}> in the form")


def submitted(form: dict) -> dict:
    """What a browser would post from this form, untouched by the user."""
    data = {}
    for kind, control in form["controls"]:
        if kind == "input":
            name = control.get("name")
            kind_of = (control.get("type") or "text").lower()
            if not name or "disabled" in control or kind_of in {"submit", "button", "reset", "file", "image"}:
                continue
            if kind_of in {"checkbox", "radio"}:
                if "checked" in control:
                    data[name] = control.get("value") or "on"
                continue
            data[name] = control.get("value") or ""
        elif kind == "select":
            options = control["options"]
            if not control["name"] or not options:
                continue
            chosen = next((o for o in options if "selected" in o["attrs"]), options[0])
            data[control["name"]] = chosen["attrs"].get("value", chosen["text"].strip())
        else:
            data[control["name"]] = control["text"]
    return data


class ConsoleFixture(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(
            username="wiring.admin", email="wiring.admin@test.local", password=PASSWORD
        )
        self.student = User.objects.create_user(
            username="wiring.student", email="wiring.student@test.local", password=PASSWORD,
            full_name="Wiring Student", is_verified=True, status=AccountStatus.ACTIVE,
        )
        self.client.force_login(self.admin)

    def page(self, url: str, **params) -> Page:
        response = self.client.get(url, params)
        self.assertEqual(response.status_code, 200, url)
        return Page(response.content.decode())


# ---------------------------------------------------------------------------
# B1 — the AI model editor on Token management
# ---------------------------------------------------------------------------
class ModelEditorTests(ConsoleFixture):
    def editor(self) -> dict:
        return self.page(reverse("console:tokens")).form(id="modelForm")

    def test_the_editor_posts_every_field_the_form_reads(self):
        fields = set(submitted(self.editor())) - {"csrfmiddlewaretoken"}
        # Unticked checkboxes are simply absent from a real post.
        self.assertEqual(fields | {"supports_vision"}, set(AIModelForm._meta.fields))

    def test_the_editor_offers_the_real_vendors_and_reasoning_modes(self):
        form = self.editor()
        self.assertEqual(
            option_values(form, "provider_ref"),
            [str(pk) for pk in AIProvider.objects.order_by("sort_order", "name").values_list("pk", flat=True)],
        )
        self.assertEqual(option_values(form, "thinking_mode"), list(AIModel.Thinking.values))

    def test_adding_a_model_from_the_dialog_saves_it(self):
        form = self.editor()
        payload = submitted(form)
        # The two boxes an administrator has to type in; everything else is
        # exactly what the dialog opens with.
        payload.update(key="wiring-test-model", name="Wiring Test")

        response = self.client.post(reverse("console:model_create"), payload)

        self.assertEqual(response.status_code, 302)
        model = AIModel.objects.get(key="wiring-test-model")
        vendor = AIProvider.objects.order_by("sort_order", "name").first()
        self.assertEqual(model.provider_ref, vendor)
        self.assertEqual(model.provider, vendor.slug)
        self.assertEqual(model.thinking_mode, AIModel.Thinking.OFF)

    def test_editing_a_model_from_the_dialog_saves_it(self):
        target = model_for(SONNET)
        target.thinking_mode = AIModel.Thinking.ADAPTIVE
        target.save(update_fields=["thinking_mode"])

        page = self.page(reverse("console:tokens"))
        button = next(a for a in page.with_attr("data-open-model") if a["data-open-model"] == str(target.pk))
        data = json.loads(button["data-model"])

        # What the dialog's script does with that JSON before the admin sees it.
        payload = submitted(page.form(id="modelForm"))
        for name, value in data.items():
            if name in {"is_active", "supports_vision"}:
                payload.pop(name, None)
                if value:
                    payload[name] = "on"
            elif value is not None:
                payload[name] = str(value)
        payload["name"] = "Sonnet (renamed)"

        response = self.client.post(reverse("console:model_save", args=[target.pk]), payload)

        self.assertEqual(response.status_code, 302)
        target.refresh_from_db()
        self.assertEqual(target.name, "Sonnet (renamed)")
        self.assertEqual(target.provider_ref.slug, "anthropic")
        self.assertEqual(target.thinking_mode, AIModel.Thinking.ADAPTIVE)


# ---------------------------------------------------------------------------
# B2, B7, B15 — the student page
# ---------------------------------------------------------------------------
class StudentPageTests(ConsoleFixture):
    def detail_url(self):
        return reverse("console:user_detail", args=[self.student.pk])

    def test_manage_tokens_credits_the_chosen_models_wallet(self):
        sonnet = fund_pool(SONNET)
        form = self.page(self.detail_url()).form(id="grantForm")

        self.assertEqual(form["action"], reverse("console:ai_distribute_apply"))
        self.assertEqual(
            option_values(form, "model_id"),
            [str(pk) for pk in AIModel.objects.filter(is_active=True).order_by("sort_order", "name").values_list("pk", flat=True)],
        )

        payload = submitted(form)
        payload.update(model_id=str(sonnet.pk), amount="12345", reason="Support credit")
        response = self.client.post(form["action"], payload)

        self.assertRedirects(response, self.detail_url(), fetch_redirect_response=False)
        self.assertEqual(balance(self.student, SONNET), 12345)
        self.assertEqual(balance(self.student, GPT), 0)

    def test_manage_tokens_can_take_tokens_back(self):
        fund(self.student, 5000, SONNET)
        form = self.page(self.detail_url()).form(id="grantForm")
        payload = submitted(form)
        payload.update(model_id=str(model_for(SONNET).pk), action="remove", amount="2000")

        self.client.post(form["action"], payload)

        self.assertEqual(balance(self.student, SONNET), 3000)

    def test_disable_dialog_sends_a_reason(self):
        page = self.page(self.detail_url())
        form = page.form(action=reverse("console:user_toggle", args=[self.student.pk]))
        payload = submitted(form)
        self.assertIn("reason", payload)
        payload["reason"] = "Shared login"

        self.client.post(form["action"], payload)

        self.student.refresh_from_db()
        self.assertEqual(self.student.status, AccountStatus.DISABLED)
        self.assertEqual(self.student.disabled_reason, "Shared login")
        self.assertTrue(
            AuditLog.objects.filter(action=AuditAction.USER_DISABLE, description="Shared login").exists()
        )

    def test_conversations_open_their_transcript(self):
        conversation = Conversation.objects.create(user=self.student, title="Cell biology")
        response = self.client.get(self.detail_url())
        page = Page(response.content.decode())

        link = next(a for a in page.anchors if "Cell biology" in a["text"])
        self.assertEqual(link["href"], reverse("console:conversation_detail", args=[conversation.pk]))
        # The page no longer promises a privacy the transcript page does not keep.
        self.assertNotContains(response, "private to the student")
        self.assertContains(response, "full transcript")


# ---------------------------------------------------------------------------
# B3, B4 — links into the distribute page
# ---------------------------------------------------------------------------
class DistributeLinkTests(ConsoleFixture):
    def test_low_token_rows_link_to_distribute_for_that_student_and_model(self):
        fund(self.student, 10_000, GPT)
        UserAIWallet.objects.filter(user=self.student, ai_model=model_for(GPT)).update(
            balance=200, used_tokens=9_800
        )
        page = self.page(reverse("console:low_tokens"))
        target = f"{reverse('console:ai_distribute')}?student={self.student.pk}&model={GPT}"

        self.assertIn(target, [a["href"] for a in page.anchors])
        self.assertNotIn(
            reverse("console:ai_student", args=[self.student.pk]), [a["href"] for a in page.anchors]
        )

        # Following it lands on the student, with that model already picked.
        response = self.client.get(target)
        self.assertEqual(response.context["selected_student"], self.student)
        self.assertEqual(response.context["selected_model"], model_for(GPT))

    def test_card_links_carry_the_model_key(self):
        page = self.page(reverse("console:ai_tokens"))
        hrefs = [a["href"] for a in page.anchors]
        for model in AIModel.objects.all():
            self.assertIn(f"{reverse('console:ai_distribute')}?model={model.key}", hrefs)
            self.assertNotIn(f"{reverse('console:ai_distribute')}?model={model.pk}", hrefs)

    def test_distribute_preselects_the_linked_model_and_keeps_the_scope(self):
        sonnet = model_for(SONNET)
        # Select Sonnet the way the console's model selector does.
        self.client.get(reverse("console:dashboard"), {"model": SONNET})

        form = self.page(
            reverse("console:ai_distribute"), student=self.student.pk, model=SONNET
        ).form(id="grantForm")

        self.assertEqual(submitted(form)["model_id"], str(sonnet.pk))
        self.assertEqual(self.client.session[model_scope.SESSION_KEY], SONNET)

    def test_without_a_model_the_first_is_picked_as_before(self):
        form = self.page(reverse("console:ai_distribute"), student=self.student.pk).form(id="grantForm")
        first = AIModel.objects.order_by("sort_order", "name").first()
        self.assertEqual(submitted(form)["model_id"], str(first.pk))


# ---------------------------------------------------------------------------
# B5 — nothing on Token management is a dead end
# ---------------------------------------------------------------------------
class TokenManagementDeadEndTests(ConsoleFixture):
    MOVED = ("console:tokens_assign", "console:tokens_bulk_assign", "console:tokens_pool_topup")

    def test_no_form_posts_to_an_endpoint_that_only_redirects(self):
        fund(self.student, 1000)
        page = self.page(reverse("console:tokens"), tab="students")
        actions = {form["action"] for form in page.forms}
        for name in self.MOVED:
            self.assertNotIn(reverse(name), actions)
        # Dialogs with no way to open them are gone with their triggers.
        for attr in ("data-assign", "data-open-bulk", "data-open-pool"):
            self.assertEqual(page.with_attr(attr), [], attr)
        body = self.client.get(reverse("console:tokens")).content.decode()
        for dialog in ("assignModal", "bulkModal", "poolModal"):
            self.assertNotIn(f'id="{dialog}"', body)

    def test_assign_opens_the_distribute_page_for_that_student(self):
        fund(self.student, 1000)
        page = self.page(reverse("console:tokens"), tab="students")
        self.assertEqual(
            page.anchor("Assign")["href"],
            f"{reverse('console:ai_distribute')}?student={self.student.pk}",
        )

    def test_the_pool_card_tops_up_the_chosen_model(self):
        sonnet = model_for(SONNET)
        before = model_pool_service.balance(sonnet)
        url = reverse("console:tokens")
        form = self.page(url).form(action=reverse("console:ai_pool_topup"))

        payload = submitted(form)
        self.assertIn(str(sonnet.pk), option_values(form, "model_id"))
        payload.update(model_id=str(sonnet.pk), amount="250000")
        response = self.client.post(form["action"], payload)

        self.assertRedirects(response, url, fetch_redirect_response=False)
        self.assertEqual(model_pool_service.balance(sonnet), before + 250_000)

    def test_dashboard_allocate_goes_to_distribute(self):
        page = self.page(reverse("console:dashboard"))
        self.assertEqual(page.anchor("Allocate tokens")["href"], reverse("console:ai_distribute"))

    def test_ticket_tokens_button_opens_distribute_for_the_ticket_owner(self):
        ticket = support_service.create_ticket(
            self.student,
            data={"category": "other", "subject": "Need more tokens", "description": "Please top me up."},
        )
        page = self.page(reverse("console:ticket_detail", args=[ticket.reference]))
        self.assertEqual(
            page.anchor("Tokens")["href"],
            f"{reverse('console:ai_distribute')}?student={self.student.pk}",
        )


# ---------------------------------------------------------------------------
# B6 — status changes from the edit form
# ---------------------------------------------------------------------------
class UserEditStatusTests(ConsoleFixture):
    def edit(self, user, **changes):
        url = reverse("console:user_edit", args=[user.pk])
        payload = submitted(self.page(url).form(field="username"))
        payload.update(changes)
        return self.client.post(url, payload)

    def test_disabling_from_the_edit_form_goes_through_set_enabled(self):
        signed_in = Client()
        signed_in.force_login(self.student)
        student_sessions = lambda: [  # noqa: E731
            s for s in Session.objects.all()
            if str(s.get_decoded().get("_auth_user_id")) == str(self.student.pk)
        ]
        self.assertTrue(student_sessions())

        response = self.edit(self.student, status=AccountStatus.DISABLED, disable_reason="Chargeback")

        self.assertEqual(response.status_code, 302)
        self.student.refresh_from_db()
        self.assertEqual(self.student.status, AccountStatus.DISABLED)
        self.assertFalse(self.student.is_active)
        self.assertIsNotNone(self.student.disabled_at)
        self.assertEqual(self.student.disabled_reason, "Chargeback")
        self.assertEqual(student_sessions(), [])
        self.assertTrue(AuditLog.objects.filter(action=AuditAction.USER_DISABLE).exists())
        # The "before" half of the update record is the real previous state.
        update = AuditLog.objects.filter(action=AuditAction.USER_UPDATE).latest("created_at")
        self.assertIn("status=active", update.old_value)

    def test_enabling_from_the_edit_form_clears_the_disable_stamp(self):
        from AI.services import account_service

        account_service.set_enabled(self.student, False, reason="Paused")
        self.edit(self.student, status=AccountStatus.ACTIVE)

        self.student.refresh_from_db()
        self.assertTrue(self.student.is_active_account)
        self.assertIsNone(self.student.disabled_at)
        self.assertEqual(self.student.disabled_reason, "")
        self.assertTrue(AuditLog.objects.filter(action=AuditAction.USER_ENABLE).exists())

    def test_other_edits_leave_the_status_alone(self):
        self.edit(self.student, full_name="Renamed Student")
        self.student.refresh_from_db()
        self.assertEqual(self.student.full_name, "Renamed Student")
        self.assertTrue(self.student.is_active_account)
        self.assertFalse(AuditLog.objects.filter(action=AuditAction.USER_DISABLE).exists())

    def test_an_admin_cannot_disable_their_own_account(self):
        response = self.edit(self.admin, status=AccountStatus.DISABLED)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "You cannot disable your own account")
        self.admin.refresh_from_db()
        self.assertTrue(self.admin.is_active_account)

    def test_an_admin_cannot_demote_their_own_account(self):
        response = self.edit(self.admin, role=Role.STUDENT)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "You cannot remove your own administrator role")
        self.admin.refresh_from_db()
        self.assertEqual(self.admin.role, Role.ADMIN)

    def test_an_admin_can_still_demote_someone_else(self):
        other = User.objects.create_superuser(
            username="other.admin", email="other.admin@test.local", password=PASSWORD
        )
        self.edit(other, role=Role.STUDENT)
        other.refresh_from_db()
        self.assertEqual(other.role, Role.STUDENT)

    def test_the_toggle_refuses_your_own_account(self):
        self.client.post(reverse("console:user_toggle", args=[self.admin.pk]), {"reason": "oops"})
        self.admin.refresh_from_db()
        self.assertTrue(self.admin.is_active_account)


# ---------------------------------------------------------------------------
# B8 — the pool export reads the per-model ledger
# ---------------------------------------------------------------------------
class PoolExportTests(ConsoleFixture):
    def test_pool_movements_come_from_the_per_model_ledger(self):
        sonnet = model_for(SONNET)
        model_pool_service.top_up(sonnet, 4321, description="Wiring top-up")

        response = self.client.get(reverse("console:export_download", args=["pool"]))
        body = b"".join(response.streaming_content).decode("utf-8")
        rows = list(csv.reader(io.StringIO(body)))

        self.assertEqual(rows[0][:3], ["id", "model", "action"])
        matching = [row for row in rows[1:] if "Wiring top-up" in row]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0][1], sonnet.name)
        self.assertEqual(len(rows) - 1, ModelPoolTransaction.objects.count())


# ---------------------------------------------------------------------------
# B9, B10 — account import
# ---------------------------------------------------------------------------
class AccountImportTests(ConsoleFixture):
    def upload(self):
        csv_file = SimpleUploadedFile(
            "students.csv", b"username,full_name,password\nwiring.one,Wiring One,\n", content_type="text/csv"
        )
        return self.client.post(reverse("console:accounts_import_preview"), {"file": csv_file})

    def test_the_sample_csv_is_for_administrators_only(self):
        url = reverse("console:accounts_sample")
        self.assertEqual(Client().get(url).status_code, 302)

        student = Client()
        student.force_login(self.student)
        response = student.get(url)
        self.assertEqual(response.status_code, 302)
        self.assertNotIn("text/csv", response.get("Content-Type", ""))

        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/csv", response["Content-Type"])

    def test_imported_passwords_are_shown_once_then_cleared(self):
        self.upload()
        response = self.client.post(reverse("console:accounts_import_confirm"))
        self.assertRedirects(response, reverse("console:accounts_import_result"), fetch_redirect_response=False)

        result = self.client.get(reverse("console:accounts_import_result"))
        self.assertEqual(result.status_code, 200)
        self.assertContains(result, "wiring.one")
        self.assertEqual(len(result.context["created"]), 1)
        self.assertIn("no-store", result["Cache-Control"])
        self.assertNotIn("bulk_import_result", self.client.session)

        again = self.client.get(reverse("console:accounts_import_result"))
        self.assertRedirects(again, reverse("console:accounts"), fetch_redirect_response=False)

    def test_a_preview_can_be_discarded(self):
        self.upload()
        self.assertIn("bulk_import_rows", self.client.session)

        page = self.page(reverse("console:accounts"))
        form = page.form(action=reverse("console:accounts_import_clear"))
        self.client.post(form["action"], submitted(form))

        self.assertNotIn("bulk_import_rows", self.client.session)
        self.assertFalse(User.objects.filter(username="wiring.one").exists())


# ---------------------------------------------------------------------------
# B11 — open redirects
# ---------------------------------------------------------------------------
class SafeNextTests(ConsoleFixture):
    EVIL = ("https://evil.example/", "//evil.example/steal", "javascript:alert(1)")

    def test_the_helper_keeps_local_paths_and_drops_everything_else(self):
        rf = RequestFactory()
        self.assertEqual(safe_next(rf.post("/x/", {"next": "/admin/users/"}), "console:ai_tokens"), "/admin/users/")
        for target in self.EVIL:
            request = rf.post("/x/", {"next": target})
            self.assertEqual(next_url(request), "")
            self.assertEqual(safe_next(request, "console:ai_tokens"), reverse("console:ai_tokens"))
        self.assertEqual(
            safe_next(rf.post("/x/"), "console:user_detail", pk=7), reverse("console:user_detail", args=[7])
        )

    def assertStaysHome(self, response):
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].startswith("/"), response["Location"])
        self.assertFalse(response["Location"].startswith("//"), response["Location"])

    def test_every_next_in_the_console_is_checked(self):
        gpt = fund_pool(GPT)
        for target in self.EVIL:
            with self.subTest(target=target):
                self.assertStaysHome(self.client.post(
                    reverse("console:ai_distribute_apply"),
                    {"user_id": self.student.pk, "model_id": gpt.pk, "action": "add", "amount": 10, "next": target},
                ))
                self.assertStaysHome(self.client.post(
                    reverse("console:ai_toggle_access", args=[self.student.pk, gpt.pk]), {"next": target}
                ))
                self.assertStaysHome(self.client.post(
                    reverse("console:ai_recalculate", args=[self.student.pk, gpt.pk]), {"next": target}
                ))
                self.assertStaysHome(self.client.post(
                    reverse("console:ai_pool_topup"), {"model_id": gpt.pk, "amount": 5, "next": target}
                ))
                self.assertStaysHome(self.client.post(
                    reverse("console:user_toggle", args=[self.student.pk]), {"next": target}
                ))


# ---------------------------------------------------------------------------
# B12, B13 — the dashboard says what it counts
# ---------------------------------------------------------------------------
class DashboardBriefingTests(ConsoleFixture):
    def briefing(self) -> str:
        body = self.client.get(reverse("console:dashboard")).content.decode()
        start = body.index('class="brief-text"')
        return " ".join(body[start:body.index("</p>", start)].split())

    def test_the_first_sentence_counts_enabled_accounts(self):
        text = self.briefing()
        self.assertIn("1 of 1 student account is enabled", text)
        self.assertNotIn("active today", text)

    def test_healthy_only_when_nobody_is_running_low(self):
        fund(self.student, 100_000)
        self.assertIn("Balances look healthy", self.briefing())

        UserAIWallet.objects.filter(user=self.student, ai_model=model_for(GPT)).update(
            balance=1_000, used_tokens=99_000
        )
        text = self.briefing()
        self.assertNotIn("look healthy", text)
        self.assertIn("1 student is running low", text)

    def test_nothing_handed_out_is_not_called_healthy(self):
        text = self.briefing()
        self.assertNotIn("look healthy", text)
        self.assertIn("No tokens have been handed out yet", text)

    @override_settings(OPENAI_API_KEY="", ANTHROPIC_API_KEY="")
    def test_provider_warning_names_each_missing_vendor(self):
        body = self.client.get(reverse("console:dashboard")).content.decode()
        for slug in ("openai", "anthropic"):
            active = AIModel.objects.filter(is_active=True, provider=slug, api_key_setting="")
            if active.exists():
                self.assertIn(f"{slug.upper()}_API_KEY", body)
                self.assertIn(active.first().name, body)

    @override_settings(OPENAI_API_KEY="sk-test")
    def test_a_configured_vendor_is_not_reported(self):
        body = self.client.get(reverse("console:dashboard")).content.decode()
        self.assertNotIn("OPENAI_API_KEY", body)
