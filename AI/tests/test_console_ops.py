"""System health and data exports — the two administrator-only operations pages.

Both surfaces exist to be trusted in production, so the tests here care about
three things beyond "does it render": a student can never reach them, a broken
probe degrades to a visible warning instead of a 500, and an export never
carries a field nobody asked for.
"""
from __future__ import annotations

import csv
import io
from datetime import timedelta
from unittest import mock

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from AI.models import (
    AccountStatus,
    AIModel,
    AIUsageLog,
    AuditAction,
    AuditLog,
    ModelPoolAction,
    ModelPoolTransaction,
    ModelTokenPool,
    Plan,
    User,
)
from AI.services import export_service, health_service
from AI.tests.helpers import GPT, SONNET

PASSWORD = "Sup3rSecret!x"


def _read_csv(response) -> list[list[str]]:
    body = b"".join(response.streaming_content).decode("utf-8")
    return list(csv.reader(io.StringIO(body)))


class OpsAccessTests(TestCase):
    """Neither page has a student-facing counterpart, so neither may leak one."""

    def setUp(self):
        self.admin = User.objects.create_superuser(
            username="ops.admin", email="ops@test.local", password=PASSWORD
        )
        self.student = User.objects.create_user(
            username="ops.student", email="student@test.local", password=PASSWORD,
            is_verified=True, status=AccountStatus.ACTIVE,
        )
        self.urls = [
            reverse("console:health"),
            reverse("console:health_data"),
            reverse("console:exports"),
            reverse("console:export_download", args=["students"]),
        ]
        cache.clear()

    def test_a_student_is_turned_away_from_every_ops_url(self):
        self.client.force_login(self.student)
        for url in self.urls:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 302, url)
            self.assertIn("/chat/", response["Location"], url)

    def test_a_signed_out_visitor_is_sent_to_the_admin_sign_in(self):
        for url in self.urls:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 302, url)
            self.assertIn("/admin/login/", response["Location"], url)

    def test_an_administrator_reaches_all_of_them(self):
        self.client.force_login(self.admin)
        for url in self.urls:
            self.assertEqual(self.client.get(url).status_code, 200, url)


class HealthReportTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(
            username="health.admin", email="health@test.local", password=PASSWORD
        )
        self.client.force_login(self.admin)
        cache.clear()

    def test_the_report_covers_every_group_and_ranks_the_worst_first(self):
        report = health_service.report()
        self.assertEqual(
            [g.key for g in report["groups"]],
            ["security", "provider", "pool", "integrations", "queues"],
        )
        self.assertGreater(report["check_count"], 10)
        self.assertIn(report["overall"], {health_service.OK, health_service.WARN, health_service.FAIL})
        # The summary counts must agree with the checks themselves.
        failing = [c for g in report["groups"] for c in g.checks if c.status != health_service.OK]
        self.assertEqual(len(report["failing"]), len(failing))

    @override_settings(DEBUG=True)
    def test_debug_mode_is_reported_as_a_failure(self):
        report = health_service.report()
        debug = next(c for g in report["groups"] for c in g.checks if c.key == "debug")
        self.assertEqual(debug.status, health_service.FAIL)
        self.assertEqual(report["overall"], health_service.FAIL)

    @override_settings(DEBUG=False, SECRET_KEY="a-real-key-that-is-not-the-default", SESSION_COOKIE_SECURE=True)
    def test_a_hardened_deployment_passes_the_security_checks(self):
        report = health_service.report()
        security = next(g for g in report["groups"] if g.key == "security")
        for key in ("debug", "secret_key", "cookies"):
            check = next(c for c in security.checks if c.key == key)
            self.assertEqual(check.status, health_service.OK, key)

    def test_a_failing_probe_becomes_a_visible_check_not_a_500(self):
        """A health page that crashes is worse than one reporting bad news."""
        with mock.patch.object(health_service, "_provider", side_effect=RuntimeError("boom")):
            report = health_service.report()
        provider = next(g for g in report["groups"] if g.key == "provider")
        self.assertEqual(provider.status, health_service.FAIL)
        self.assertEqual(provider.checks[0].value, "Could not be read")
        self.assertIn("RuntimeError", provider.checks[0].detail)

        # And the page still renders.
        self.assertEqual(self.client.get(reverse("console:health")).status_code, 200)

    def test_no_secret_value_ever_reaches_the_page(self):
        from django.conf import settings

        response = self.client.get(reverse("console:health"))
        body = response.content.decode()
        self.assertNotIn(settings.SECRET_KEY, body)
        # Every vendor's key, not just the first one that existed.
        for secret in (settings.OPENAI_API_KEY, settings.ANTHROPIC_API_KEY):
            if secret:
                self.assertNotIn(secret, body)
        self.assertNotIn("sk-ant-", body)
        self.assertNotIn("sk-proj-", body)

    def test_a_high_failure_rate_is_flagged_against_the_provider_that_failed(self):
        """One vendor's outage must be visible, not averaged into the other's."""
        student = User.objects.create_user(
            username="burner", email="burner@test.local", password=PASSWORD
        )
        for index in range(10):
            AIUsageLog.objects.create(
                user=student, model="gpt-5-mini", provider="openai", success=index < 5,
                error_code="rate_limit" if index >= 5 else "", latency_ms=900,
            )
        # Anthropic is answering perfectly at the same time.
        for _ in range(5):
            AIUsageLog.objects.create(
                user=student, model="claude-sonnet-5", provider="anthropic",
                success=True, latency_ms=700,
            )

        report = health_service.report()
        checks = {c.key: c for g in report["groups"] for c in g.checks}

        failing = checks["provider_errors:openai"]
        self.assertEqual(failing.status, health_service.FAIL)
        self.assertIn("50.0%", failing.value)

        healthy = checks["provider_errors:anthropic"]
        self.assertEqual(healthy.status, health_service.OK)
        self.assertIn("0.0%", healthy.value)

        self.assertEqual(report["errors"][0]["code"], "rate_limit")
        self.assertEqual(report["errors"][0]["count"], 5)

    def test_the_json_endpoint_mirrors_the_page(self):
        response = self.client.get(reverse("console:health_data"))
        data = response.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["check_count"], len(data["checks"]))
        self.assertIn(data["overall"], {"ok", "warn", "fail"})

    def test_the_navigation_badge_is_cached(self):
        health_service.alert_count(force=True)
        with mock.patch.object(health_service, "report") as report:
            self.assertIsInstance(health_service.alert_count(), int)
            report.assert_not_called()

    def test_the_badge_survives_a_broken_report(self):
        cache.clear()
        with mock.patch.object(health_service, "report", side_effect=RuntimeError("boom")):
            self.assertEqual(health_service.alert_count(force=True), 0)


class PoolRunwayTests(TestCase):
    """The supply forecast, which is per model because supply is per model."""

    def setUp(self):
        cache.clear()
        self.admin = User.objects.create_superuser(
            username="pool.admin", email="pool@test.local", password=PASSWORD
        )
        self.student = User.objects.create_user(
            username="pool.student", email="ps@test.local", password=PASSWORD
        )
        self.model = AIModel.objects.get(key=GPT)
        self.other = AIModel.objects.get(key=SONNET)

    def _set_balance(self, model, amount):
        ModelTokenPool.objects.update_or_create(
            ai_model=model, defaults={"balance": amount, "total_added": amount}
        )

    def _hand_out(self, model, amount, *, days_ago=0):
        entry = ModelPoolTransaction.objects.create(
            ai_model=model, amount=-amount, action=ModelPoolAction.DISTRIBUTE,
            student=self.student, balance_after=0,
        )
        if days_ago:
            ModelPoolTransaction.objects.filter(pk=entry.pk).update(
                created_at=timezone.now() - timedelta(days=days_ago)
            )
        return entry

    def test_no_distribution_means_no_forecast(self):
        self.assertIsNone(health_service.pool_runway(self.model)["runway_days"])

    def test_the_forecast_divides_the_balance_by_the_daily_rate(self):
        self._set_balance(self.model, 90_000)
        # 30,000 tokens handed out over the 30-day window = 1,000 a day.
        self._hand_out(self.model, 30_000)

        runway = health_service.pool_runway(self.model)
        self.assertEqual(runway["distributed"], 30_000)
        self.assertEqual(runway["per_day"], 1_000)
        self.assertEqual(runway["runway_days"], 90)

    def test_one_models_distribution_does_not_affect_another_forecast(self):
        self._set_balance(self.model, 90_000)
        self._set_balance(self.other, 90_000)
        self._hand_out(self.model, 30_000)

        self.assertEqual(health_service.pool_runway(self.model)["runway_days"], 90)
        self.assertIsNone(health_service.pool_runway(self.other)["runway_days"])

    def test_movements_outside_the_window_are_ignored(self):
        self._set_balance(self.model, 90_000)
        self._hand_out(self.model, 30_000, days_ago=90)

        self.assertIsNone(health_service.pool_runway(self.model)["runway_days"])

    def test_every_model_gets_a_forecast(self):
        rows = health_service.pool_runways()
        self.assertEqual(
            [row["model"].key for row in rows],
            list(AIModel.objects.order_by("sort_order", "name").values_list("key", flat=True)),
        )


class ExportTests(TestCase):
    def setUp(self):
        cache.clear()
        self.admin = User.objects.create_superuser(
            username="export.admin", email="export@test.local", password=PASSWORD
        )
        self.student = User.objects.create_user(
            username="export.student", email="es@test.local", password=PASSWORD,
            full_name="Exported Student", is_verified=True, status=AccountStatus.ACTIVE,
        )
        Plan.objects.create(name="Starter", tokens=50_000, price=5, duration_days=30)
        self.client.force_login(self.admin)

    def test_every_dataset_streams_a_well_formed_file(self):
        for key, dataset in export_service.DATASETS.items():
            response = self.client.get(reverse("console:export_download", args=[key]))
            self.assertEqual(response.status_code, 200, key)
            self.assertTrue(response.streaming, key)
            self.assertIn("text/csv", response["Content-Type"], key)
            self.assertIn(f'filename="epc_{key}', response["Content-Disposition"], key)

            rows = _read_csv(response)
            self.assertEqual(rows[0], dataset.headers, key)
            for row in rows[1:]:
                self.assertEqual(len(row), len(dataset.headers), f"{key}: ragged row")

    def test_the_students_file_carries_the_student_but_no_credential(self):
        response = self.client.get(reverse("console:export_download", args=["students"]))
        rows = _read_csv(response)
        body = "\n".join(",".join(r) for r in rows)

        self.assertIn("Exported Student", body)
        # The guarantee that matters: an explicit column list means a password
        # hash or session key can never be swept in by accident.
        self.assertNotIn(self.student.password, body)
        self.assertNotIn("pbkdf2", body)
        self.assertNotIn("argon2", body)
        for forbidden in ("password", "active_session_key", "session"):
            self.assertNotIn(forbidden, rows[0])

    def test_a_date_range_narrows_the_file(self):
        old = AIUsageLog.objects.create(user=self.student, model="gpt-5-mini", total_tokens=10)
        AIUsageLog.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(days=400))
        AIUsageLog.objects.create(user=self.student, model="gpt-5-mini", total_tokens=20)

        everything = _read_csv(self.client.get(reverse("console:export_download", args=["usage"])))
        self.assertEqual(len(everything), 3)  # header + two rows

        today = timezone.localtime().date()
        narrowed = _read_csv(
            self.client.get(
                reverse("console:export_download", args=["usage"]),
                {"from": today.isoformat(), "to": today.isoformat()},
            )
        )
        self.assertEqual(len(narrowed), 2)  # header + today's row only

    def test_the_end_of_a_range_includes_its_whole_day(self):
        """'Up to the 31st' means the 31st, not midnight at its start."""
        log = AIUsageLog.objects.create(user=self.student, model="gpt-5-mini", total_tokens=10)
        late = timezone.localtime().replace(hour=23, minute=58, second=0, microsecond=0)
        AIUsageLog.objects.filter(pk=log.pk).update(created_at=late)

        today = timezone.localtime().date().isoformat()
        rows = _read_csv(
            self.client.get(
                reverse("console:export_download", args=["usage"]), {"from": today, "to": today}
            )
        )
        self.assertEqual(len(rows), 2)

    def test_every_download_is_written_to_the_audit_trail(self):
        self.client.get(
            reverse("console:export_download", args=["students"]), {"from": "2020-01-01"}
        )
        entry = AuditLog.objects.filter(action=AuditAction.DATA_EXPORT).latest("created_at")
        self.assertEqual(entry.actor, self.admin)
        self.assertEqual(entry.new_value, "students")
        self.assertEqual(entry.target_label, "Students")
        self.assertIn("2020-01-01", entry.description)
        # Exporting everyone's data reads as consequential, not routine.
        self.assertEqual(entry.tone, "danger")

    def test_an_unknown_dataset_is_refused_rather_than_crashing(self):
        response = self.client.get(reverse("console:export_download", args=["not_a_dataset"]))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/exports/", response["Location"])

    def test_a_backwards_range_is_rejected(self):
        response = self.client.get(
            reverse("console:export_download", args=["students"]),
            {"from": "2026-12-31", "to": "2026-01-01"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertFalse(AuditLog.objects.filter(action=AuditAction.DATA_EXPORT).exists())

    def test_a_malformed_date_is_ignored_rather_than_raising(self):
        response = self.client.get(
            reverse("console:export_download", args=["students"]), {"from": "yesterday"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertGreater(len(_read_csv(response)), 1)

    def test_the_page_shows_a_row_count_for_each_dataset(self):
        response = self.client.get(reverse("console:exports"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(response.context["counts"]), set(export_service.DATASETS))
        self.assertEqual(response.context["counts"]["students"], 1)

    def test_the_file_is_never_cached_by_a_proxy(self):
        response = self.client.get(reverse("console:export_download", args=["students"]))
        self.assertIn("no-store", response["Cache-Control"])
        self.assertIn("private", response["Cache-Control"])
