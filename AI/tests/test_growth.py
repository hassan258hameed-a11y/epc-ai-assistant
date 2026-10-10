"""Referrals, free trial, admin-created accounts and bulk import."""
from __future__ import annotations

import io

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase

from AI.models import (
    AccountSource,
    AIModel,
    AccountStatus,
    AuditAction,
    AuditLog,
    Referral,
    Role,
    TokenTransaction,
    TransactionType,
    User,
)
from AI.services import (
    account_service,
    model_service,
    referral_service,
    settings_service,
    token_service,
    wallet_service,
)
from AI.utils.uploads import InvalidUpload

PASSWORD = "Sup3rSecret!x"


def make_user(username: str, **extra) -> User:
    defaults = {
        "email": f"{username}@test.local",
        "is_verified": True,
        "status": AccountStatus.ACTIVE,
        "role": Role.STUDENT,
    }
    defaults.update(extra)
    return User.objects.create_user(username=username, password=PASSWORD, **defaults)


class ReferralTests(TestCase):
    def setUp(self):
        settings_service.bootstrap()
        settings_service.set_value("referral_enabled", True)
        settings_service.set_value("referral_bonus_referrer", 10_000)
        settings_service.set_value("referral_bonus_referred", 5_000)
        settings_service.set_value("free_trial_enabled", False)

        self.referrer = make_user("inviter")
        self.newcomer = make_user("newcomer")

    def test_referral_pays_both_sides_once_verified(self):
        referral_service.register_referral(self.newcomer, self.referrer)
        referral_service.reward_referral(self.newcomer)

        self.assertEqual(token_service.balance_of(self.referrer), 10_000)
        self.assertEqual(token_service.balance_of(self.newcomer), 5_000)

        referral = Referral.objects.get(referred_user=self.newcomer)
        self.assertEqual(referral.status, Referral.Status.REWARDED)
        self.assertEqual(referral.referrer_bonus, 10_000)
        self.assertEqual(referral.referred_bonus, 5_000)

    def test_bonus_cannot_be_claimed_twice(self):
        referral_service.register_referral(self.newcomer, self.referrer)
        for _ in range(3):
            referral_service.reward_referral(self.newcomer)

        self.assertEqual(token_service.balance_of(self.referrer), 10_000)
        self.assertEqual(token_service.balance_of(self.newcomer), 5_000)
        self.assertEqual(
            TokenTransaction.objects.filter(transaction_type=TransactionType.REFERRAL_BONUS).count(), 2
        )

    def test_self_referral_is_refused(self):
        result = referral_service.register_referral(self.referrer, self.referrer)
        self.assertIsNone(result)
        self.assertFalse(Referral.objects.exists())

    def test_a_user_can_only_be_referred_once(self):
        another = make_user("another")
        referral_service.register_referral(self.newcomer, self.referrer)
        referral_service.register_referral(self.newcomer, another)

        self.assertEqual(Referral.objects.filter(referred_user=self.newcomer).count(), 1)
        self.assertEqual(Referral.objects.get(referred_user=self.newcomer).referrer, self.referrer)

    def test_code_validation_rejects_unknown_codes(self):
        referrer, error = referral_service.validate_code("NOTREAL9")
        self.assertIsNone(referrer)
        self.assertTrue(error)

    def test_code_validation_rejects_self_referral_by_email(self):
        referrer, error = referral_service.validate_code(self.referrer.referral_code, email=self.referrer.email)
        self.assertIsNone(referrer)
        self.assertIn("yourself", error.lower())

    def test_empty_code_is_valid_since_referral_is_optional(self):
        referrer, error = referral_service.validate_code("")
        self.assertIsNone(referrer)
        self.assertEqual(error, "")

    def test_signup_with_a_referral_link_records_the_referrer(self):
        settings_service.set_value("require_verification", False)
        self.client.post(
            "/signup/",
            {
                "full_name": "Invited Student", "email": "invited@test.local", "username": "invited",
                "password": PASSWORD, "referral_code": self.referrer.referral_code, "accept_terms": "on",
            },
        )
        invited = User.objects.get(username="invited")
        self.assertEqual(invited.referred_by, self.referrer)
        self.assertTrue(Referral.objects.filter(referred_user=invited).exists())

    def test_disabled_programme_pays_nothing(self):
        settings_service.set_value("referral_enabled", False)
        referral_service.register_referral(self.newcomer, self.referrer)
        referral_service.reward_referral(self.newcomer)

        self.assertEqual(token_service.balance_of(self.referrer), 0)

    def test_stats_summarise_a_referrers_activity(self):
        referral_service.register_referral(self.newcomer, self.referrer)
        referral_service.reward_referral(self.newcomer)

        stats = referral_service.stats_for(self.referrer)
        self.assertEqual(stats["total"], 1)
        self.assertEqual(stats["rewarded"], 1)
        self.assertEqual(stats["tokens_earned"], 10_000)


class FreeTrialTests(TestCase):
    def setUp(self):
        settings_service.bootstrap()
        settings_service.set_value("free_trial_enabled", True)
        settings_service.set_value("free_trial_tokens", 20_000)
        self.user = make_user("trialist")

    def test_welcome_grant_goes_only_to_the_default_model_once(self):
        """The trial is GPT-5 Mini tokens and nothing else, granted once.

        Other models, even ones granted automatically elsewhere, start at zero
        until a plan or an administrator funds them.
        """
        default = model_service.default_model()
        self.assertEqual(default.key, "gpt-5-mini")
        others = list(AIModel.objects.filter(is_active=True).exclude(pk=default.pk))
        self.assertTrue(any(m.auto_grant for m in others), "only means anything with other auto-grant models")

        self.assertEqual(referral_service.grant_welcome_tokens(self.user), 20_000)
        self.assertEqual(referral_service.grant_welcome_tokens(self.user), 0)
        self.assertEqual(referral_service.grant_welcome_tokens(self.user), 0)

        self.assertEqual(wallet_service.balance_of(self.user, default), 20_000)
        for ai_model in others:
            self.assertEqual(wallet_service.balance_of(self.user, ai_model), 0, ai_model.key)

        self.assertEqual(token_service.balance_of(self.user), 20_000)
        self.assertEqual(
            TokenTransaction.objects.filter(
                user=self.user, transaction_type=TransactionType.FREE_TRIAL
            ).count(),
            1,
        )

    def test_the_trial_follows_the_default_model(self):
        haiku = AIModel.objects.get(key="claude-haiku-4-5")
        model_service.make_default(haiku)
        referral_service.grant_welcome_tokens(self.user)
        self.assertEqual(wallet_service.balance_of(self.user, haiku), 20_000)
        self.assertEqual(token_service.balance_of(self.user), 20_000)

    def test_disabled_trial_grants_nothing(self):
        settings_service.set_value("free_trial_enabled", False)
        fresh = make_user("notrial")
        self.assertEqual(referral_service.grant_welcome_tokens(fresh), 0)
        self.assertEqual(token_service.balance_of(fresh), 0)

    def test_onboard_applies_trial_and_referral_together(self):
        settings_service.set_value("referral_bonus_referred", 7_500)
        referrer = make_user("sponsor")
        referral_service.register_referral(self.user, referrer)

        # The trial is one grant, in the default model.
        result = referral_service.onboard(self.user)
        self.assertEqual(result["free_trial_tokens"], 20_000)
        self.assertEqual(result["referral_bonus"], 7_500)
        self.assertEqual(token_service.balance_of(self.user), 20_000 + 7_500)


class AdminAccountTests(TestCase):
    def setUp(self):
        settings_service.bootstrap()
        settings_service.set_value("free_trial_enabled", False)
        self.admin = User.objects.create_superuser(
            username="creator", email="creator@test.local", password=PASSWORD
        )

    def test_account_creation_generates_a_password_and_grants_tokens(self):
        user, password = account_service.create_account(
            username="tg.customer", full_name="Telegram Customer",
            source=AccountSource.TELEGRAM, tokens=50_000, created_by=self.admin,
        )

        self.assertTrue(password)
        self.assertTrue(user.check_password(password))
        self.assertTrue(user.must_change_password)
        self.assertEqual(user.source, AccountSource.TELEGRAM)
        self.assertEqual(user.created_by, self.admin)
        self.assertEqual(token_service.balance_of(user), 50_000)
        self.assertTrue(AuditLog.objects.filter(action=AuditAction.USER_CREATE).exists())

    def test_supplied_password_is_used_and_hashed(self):
        user, password = account_service.create_account(
            username="fixed.pass", password="ChosenPass!2024", created_by=self.admin
        )
        self.assertEqual(password, "ChosenPass!2024")
        self.assertTrue(user.check_password("ChosenPass!2024"))
        self.assertNotEqual(user.password, "ChosenPass!2024")
        self.assertFalse(user.must_change_password)

    def test_disabling_clears_the_session_and_blocks_login(self):
        user, password = account_service.create_account(username="tobedisabled", created_by=self.admin)
        account_service.set_enabled(user, False, actor=self.admin, reason="Policy breach")

        user.refresh_from_db()
        self.assertEqual(user.status, AccountStatus.DISABLED)
        self.assertFalse(user.is_active)
        self.assertEqual(user.active_session_key, "")
        self.assertIsNotNone(user.disabled_at)
        self.assertTrue(AuditLog.objects.filter(action=AuditAction.USER_DISABLE).exists())

        logged_in = self.client.login(username="tobedisabled", password=password)
        self.assertFalse(logged_in)

    def test_enabling_restores_access(self):
        user, password = account_service.create_account(username="restore.me", created_by=self.admin)
        account_service.set_enabled(user, False, actor=self.admin)
        account_service.set_enabled(user, True, actor=self.admin)

        user.refresh_from_db()
        self.assertEqual(user.status, AccountStatus.ACTIVE)
        self.assertTrue(user.is_active)
        self.assertEqual(user.disabled_reason, "")

    def test_password_reset_returns_a_new_password_and_ends_sessions(self):
        user, original = account_service.create_account(username="reset.target", created_by=self.admin)
        new_password = account_service.reset_password(user, actor=self.admin)

        user.refresh_from_db()
        self.assertNotEqual(new_password, original)
        self.assertTrue(user.check_password(new_password))
        self.assertFalse(user.check_password(original))
        self.assertEqual(user.active_session_key, "")


class BulkImportTests(TestCase):
    def setUp(self):
        settings_service.bootstrap()
        settings_service.set_value("free_trial_enabled", False)
        self.admin = User.objects.create_superuser(
            username="importer", email="importer@test.local", password=PASSWORD
        )

    @staticmethod
    def csv_file(text: str, name: str = "accounts.csv") -> SimpleUploadedFile:
        return SimpleUploadedFile(name, text.encode("utf-8"), content_type="text/csv")

    def test_valid_rows_are_imported(self):
        upload = self.csv_file(
            "username,full_name,email,source,tokens\n"
            "import.one,One Student,one@test.local,telegram,10000\n"
            "import.two,Two Student,two@test.local,tiktok,20000\n"
        )
        rows = account_service.parse_import(upload)
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row.is_valid for row in rows))

        result = account_service.run_import(rows, actor=self.admin)
        self.assertEqual(result["created_count"], 2)

        user = User.objects.get(username="import.one")
        self.assertEqual(user.source, AccountSource.TELEGRAM)
        self.assertEqual(token_service.balance_of(user), 10_000)
        self.assertTrue(AuditLog.objects.filter(action=AuditAction.BULK_IMPORT).exists())

    def test_duplicate_username_in_the_file_is_flagged(self):
        rows = account_service.parse_import(
            self.csv_file("username\ndupe.user\ndupe.user\n")
        )
        self.assertTrue(rows[0].is_valid)
        self.assertFalse(rows[1].is_valid)
        self.assertIn("Duplicate", rows[1].errors[0])

    def test_existing_username_is_flagged(self):
        make_user("already.here")
        rows = account_service.parse_import(self.csv_file("username\nalready.here\n"))
        self.assertFalse(rows[0].is_valid)

    def test_missing_username_is_flagged(self):
        rows = account_service.parse_import(self.csv_file("username,email\n,someone@test.local\n"))
        self.assertFalse(rows[0].is_valid)

    def test_invalid_rows_are_never_created(self):
        rows = account_service.parse_import(
            self.csv_file("username,tokens\ngood.row,1000\nbad row!,notanumber\n")
        )
        result = account_service.run_import(rows, actor=self.admin)

        self.assertEqual(result["created_count"], 1)
        self.assertEqual(result["failed"], 1)
        self.assertTrue(User.objects.filter(username="good.row").exists())
        self.assertFalse(User.objects.filter(username="bad row!").exists())

    def test_weak_password_in_the_file_is_flagged(self):
        rows = account_service.parse_import(self.csv_file("username,password\nweak.pw,123\n"))
        self.assertFalse(rows[0].is_valid)

    def test_generated_passwords_are_returned_once(self):
        rows = account_service.parse_import(self.csv_file("username\ngen.pass\n"))
        result = account_service.run_import(rows, actor=self.admin)

        created = result["created"][0]
        self.assertTrue(created["password"])
        self.assertTrue(User.objects.get(username="gen.pass").check_password(created["password"]))

    def test_a_non_spreadsheet_upload_is_rejected(self):
        binary = SimpleUploadedFile("payload.bin", bytes([0xFF, 0xD8, 0xFF, 0x00, 0x9C]), content_type="text/csv")
        with self.assertRaises(InvalidUpload):
            account_service.parse_import(binary)

    def test_empty_file_is_rejected(self):
        with self.assertRaises(InvalidUpload):
            account_service.parse_import(self.csv_file("username\n"))

    def test_xlsx_import_is_supported(self):
        from openpyxl import Workbook

        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["username", "full_name", "tokens"])
        sheet.append(["excel.user", "Excel Student", 15000])

        buffer = io.BytesIO()
        workbook.save(buffer)
        buffer.seek(0)

        upload = SimpleUploadedFile(
            "accounts.xlsx", buffer.read(),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        rows = account_service.parse_import(upload)
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0].is_valid)

        account_service.run_import(rows, actor=self.admin)
        self.assertEqual(token_service.balance_of(User.objects.get(username="excel.user")), 15_000)
