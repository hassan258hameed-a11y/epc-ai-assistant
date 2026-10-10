"""export_live_config / import_live_config: settings travel, people and history don't."""
from __future__ import annotations

import json
import tempfile
from io import StringIO
from pathlib import Path

from django.core.management import CommandError, call_command
from django.test import TestCase

from AI.models import AIModel, AppSetting, Plan, Role, SavedPrompt, User
from AI.services import model_service, settings_service

PASSWORD = "Sup3rSecret!x"


class LiveConfigTests(TestCase):
    def setUp(self):
        settings_service.bootstrap()
        model_service.ensure_seeded()
        self.tmp = Path(tempfile.mkdtemp()) / "live_config.json"
        settings_service.set_value("free_trial_tokens", 3000)
        AIModel.objects.filter(key="gpt-5-mini").update(tokens_consumed=12345, request_count=9)
        self.model = AIModel.objects.get(key="gpt-5-mini")
        Plan.objects.create(name="Starter", price=5, tokens=100_000, ai_model=self.model, is_active=True)
        self.student = User.objects.create_user(username="tester", email="t@test.local", password=PASSWORD,
                                                role=Role.STUDENT)
        SavedPrompt.objects.create(title="Library prompt", body="Explain it simply")
        SavedPrompt.objects.create(user=self.student, title="My own prompt", body="private")

    def export(self) -> list[dict]:
        call_command("export_live_config", output=str(self.tmp), stdout=StringIO())
        return json.loads(self.tmp.read_text(encoding="utf-8"))

    def test_the_export_holds_configuration_and_no_people(self):
        rows = self.export()
        models = {row["model"] for row in rows}
        self.assertIn("AI.plan", models)
        self.assertIn("AI.appsetting", models)
        self.assertNotIn("AI.user", models)
        text = self.tmp.read_text(encoding="utf-8")
        self.assertNotIn("t@test.local", text)
        self.assertNotIn("My own prompt", text)  # a student's own prompt stays behind
        self.assertIn("Library prompt", text)
        gpt = next(r for r in rows if r["model"] == "AI.aimodel" and r["fields"]["key"] == "gpt-5-mini")
        self.assertEqual((gpt["fields"]["tokens_consumed"], gpt["fields"]["request_count"]), (0, 0))

    def test_import_into_a_fresh_database(self):
        self.export()
        # The live site: nobody but the owner, and its own seeded defaults.
        User.objects.all().delete()
        Plan.objects.all().delete()
        settings_service.set_value("free_trial_tokens", 1)
        owner = User.objects.create_superuser(username="owner", email="o@test.local", password=PASSWORD)

        call_command("import_live_config", str(self.tmp), stdout=StringIO())
        self.assertTrue(Plan.objects.filter(name="Starter", is_active=True).exists())
        self.assertEqual(settings_service.get("free_trial_tokens"), 3000)
        self.assertEqual(model_service.default_model().key, "gpt-5-mini")
        self.assertEqual(AIModel.objects.get(key="gpt-5-mini").tokens_consumed, 0)
        self.assertEqual(User.objects.count(), 1)
        self.assertTrue(AppSetting.objects.exists())
        # Running it twice changes nothing.
        call_command("import_live_config", str(self.tmp), stdout=StringIO())
        self.assertEqual(Plan.objects.filter(name="Starter").count(), 1)
        self.assertEqual(User.objects.get(), owner)

    def test_it_refuses_a_site_that_already_has_students(self):
        self.export()
        User.objects.create_superuser(username="owner", email="o@test.local", password=PASSWORD)
        with self.assertRaises(CommandError):
            call_command("import_live_config", str(self.tmp), stdout=StringIO())

    def test_it_needs_an_owner_first(self):
        self.export()
        User.objects.all().delete()
        with self.assertRaises(CommandError):
            call_command("import_live_config", str(self.tmp), stdout=StringIO())
