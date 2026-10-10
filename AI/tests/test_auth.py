"""Registration, login, sessions, OTP and account status."""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import Client, TestCase
from django.utils import timezone

from AI.models import AccountStatus, AIModel, OTPCode, OTPPurpose, RateLimitEvent, Role, TokenTransaction, User, hash_code
from AI.services import otp_service, settings_service, token_service
from AI.services.otp_service import OTPError

PASSWORD = "Sup3rSecret!x"


def make_student(username="student", **extra) -> User:
    defaults = {
        "email": f"{username}@test.local",
        "full_name": "Test Student",
        "is_verified": True,
        "status": AccountStatus.ACTIVE,
        "role": Role.STUDENT,
    }
    defaults.update(extra)
    return User.objects.create_user(username=username, password=PASSWORD, **defaults)


class RegistrationTests(TestCase):
    def setUp(self):
        settings_service.bootstrap()

    def test_signup_creates_a_pending_account_and_sends_a_code(self):
        response = self.client.post(
            "/signup/",
            {
                "full_name": "New Student",
                "email": "new@test.local",
                "username": "new.student",
                "password": PASSWORD,
                "accept_terms": "on",
            },
        )
        self.assertRedirects(response, "/otp/", fetch_redirect_response=False)

        user = User.objects.get(username="new.student")
        self.assertEqual(user.status, AccountStatus.PENDING)
        self.assertFalse(user.is_verified)
        self.assertTrue(user.referral_code)
        self.assertTrue(OTPCode.objects.filter(user=user, purpose=OTPPurpose.SIGNUP).exists())
        self.assertEqual(len(mail.outbox), 1)

    def test_password_is_hashed_never_stored_in_the_clear(self):
        self.client.post(
            "/signup/",
            {"full_name": "Hash Me", "email": "hash@test.local", "username": "hash.me",
             "password": PASSWORD, "accept_terms": "on"},
        )
        user = User.objects.get(username="hash.me")
        self.assertNotEqual(user.password, PASSWORD)
        self.assertTrue(user.password.startswith("pbkdf2_"))
        self.assertTrue(user.check_password(PASSWORD))

    def test_duplicate_username_is_rejected(self):
        make_student("taken")
        response = self.client.post(
            "/signup/",
            {"full_name": "Copy Cat", "email": "copy@test.local", "username": "taken",
             "password": PASSWORD, "accept_terms": "on"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(User.objects.filter(username="taken").count(), 1)

    def test_weak_password_is_rejected(self):
        response = self.client.post(
            "/signup/",
            {"full_name": "Weak", "email": "weak@test.local", "username": "weak.user",
             "password": "12345678", "accept_terms": "on"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(username="weak.user").exists())

    def test_terms_must_be_accepted(self):
        response = self.client.post(
            "/signup/",
            {"full_name": "No Terms", "email": "noterms@test.local", "username": "no.terms", "password": PASSWORD},
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(username="no.terms").exists())


class LoginTests(TestCase):
    def setUp(self):
        settings_service.bootstrap()
        self.user = make_student("logintest")

    def test_valid_credentials_sign_the_user_in(self):
        """A student lands on the chat — it is the product, not a dashboard."""
        response = self.client.post("/login/", {"identifier": "logintest", "password": PASSWORD})
        self.assertRedirects(response, "/chat/", fetch_redirect_response=False)
        self.assertEqual(int(self.client.session["_auth_user_id"]), self.user.pk)

    def test_login_works_with_the_email_address(self):
        response = self.client.post("/login/", {"identifier": "logintest@test.local", "password": PASSWORD})
        self.assertRedirects(response, "/chat/", fetch_redirect_response=False)

    def test_wrong_password_is_refused(self):
        response = self.client.post("/login/", {"identifier": "logintest", "password": "not-the-password"})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_disabled_account_cannot_sign_in(self):
        self.user.status = AccountStatus.DISABLED
        self.user.is_active = False
        self.user.save(update_fields=["status", "is_active"])

        response = self.client.post("/login/", {"identifier": "logintest", "password": PASSWORD})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_login_is_rate_limited(self):
        for _ in range(9):
            self.client.post("/login/", {"identifier": "logintest", "password": "wrong"})

        # The window is now exhausted, so even a correct password is refused.
        response = self.client.post("/login/", {"identifier": "logintest", "password": PASSWORD})
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertTrue(RateLimitEvent.objects.filter(scope="login").exists())

    def test_admin_is_redirected_to_the_console(self):
        admin = User.objects.create_superuser(username="root", email="root@test.local", password=PASSWORD)
        response = self.client.post("/login/", {"identifier": "root", "password": PASSWORD})
        self.assertRedirects(response, "/admin/", fetch_redirect_response=False)
        self.assertTrue(admin.is_admin_user)

    def test_logout_clears_the_session(self):
        self.client.post("/login/", {"identifier": "logintest", "password": PASSWORD})
        self.client.post("/logout/")
        self.assertNotIn("_auth_user_id", self.client.session)
        self.user.refresh_from_db()
        self.assertEqual(self.user.active_session_key, "")


class SingleSessionTests(TestCase):
    def setUp(self):
        settings_service.bootstrap()
        settings_service.set_value("single_session", True)
        self.user = make_student("onesession")

    def test_signing_in_elsewhere_ends_the_first_session(self):
        # /chat/ is where a sign-in lands, so it is what a live session can see.
        first = Client()
        first.post("/login/", {"identifier": "onesession", "password": PASSWORD})
        self.assertEqual(first.get("/chat/").status_code, 200)

        second = Client()
        second.post("/login/", {"identifier": "onesession", "password": PASSWORD})
        self.assertEqual(second.get("/chat/").status_code, 200)

        # The original session is now stale and gets bounced to the login page.
        response = first.get("/chat/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response.headers["Location"])


class AccountStatusTests(TestCase):
    def setUp(self):
        settings_service.bootstrap()
        self.user = make_student("statustest")

    def test_disabling_mid_session_signs_the_user_out(self):
        self.client.post("/login/", {"identifier": "statustest", "password": PASSWORD})
        self.assertEqual(self.client.get("/chat/").status_code, 200)

        self.user.status = AccountStatus.DISABLED
        self.user.is_active = False
        self.user.save(update_fields=["status", "is_active"])

        response = self.client.get("/chat/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response.headers["Location"])

    def test_disabled_account_cannot_reach_the_chat_api(self):
        self.client.force_login(self.user)
        self.user.status = AccountStatus.DISABLED
        self.user.is_active = False
        self.user.save(update_fields=["status", "is_active"])

        response = self.client.post(
            "/api/chat/send/", {"message": "hello"}, HTTP_X_REQUESTED_WITH="XMLHttpRequest"
        )
        self.assertIn(response.status_code, (401, 403))


class OTPTests(TestCase):
    def setUp(self):
        settings_service.bootstrap()
        self.user = make_student("otptest", is_verified=False, status=AccountStatus.PENDING)

    def test_code_is_stored_hashed_not_in_plain_text(self):
        otp_service.issue(self.user, OTPPurpose.SIGNUP)
        record = OTPCode.objects.get(user=self.user)
        self.assertEqual(len(record.code_hash), 64)
        self.assertNotIn(record.code_hash, mail.outbox[0].body)

    def test_correct_code_verifies_and_is_consumed(self):
        otp_service.issue(self.user, OTPPurpose.SIGNUP)
        record = OTPCode.objects.get(user=self.user)

        code = self._extract_code()
        self.assertTrue(record.matches(code))

        otp_service.verify(self.user, OTPPurpose.SIGNUP, code)
        record.refresh_from_db()
        self.assertIsNotNone(record.consumed_at)

    def test_a_code_cannot_be_reused(self):
        otp_service.issue(self.user, OTPPurpose.SIGNUP)
        code = self._extract_code()
        otp_service.verify(self.user, OTPPurpose.SIGNUP, code)

        with self.assertRaises(OTPError):
            otp_service.verify(self.user, OTPPurpose.SIGNUP, code)

    def test_expired_code_is_rejected(self):
        otp = otp_service.issue(self.user, OTPPurpose.SIGNUP)
        code = self._extract_code()

        otp.expires_at = timezone.now() - timezone.timedelta(minutes=1)
        otp.save(update_fields=["expires_at"])

        with self.assertRaises(OTPError) as ctx:
            otp_service.verify(self.user, OTPPurpose.SIGNUP, code)
        self.assertEqual(ctx.exception.code, "expired")

    def test_attempts_are_capped(self):
        otp_service.issue(self.user, OTPPurpose.SIGNUP)
        for _ in range(OTPCode.MAX_ATTEMPTS):
            with self.assertRaises(OTPError):
                otp_service.verify(self.user, OTPPurpose.SIGNUP, "000000")

        # Even the right code is refused once the attempt budget is spent.
        with self.assertRaises(OTPError):
            otp_service.verify(self.user, OTPPurpose.SIGNUP, self._extract_code())

    def test_issuing_a_new_code_invalidates_the_previous_one(self):
        otp_service.issue(self.user, OTPPurpose.SIGNUP)
        first_code = self._extract_code()
        otp_service.issue(self.user, OTPPurpose.SIGNUP)

        with self.assertRaises(OTPError):
            otp_service.verify(self.user, OTPPurpose.SIGNUP, first_code)

    def test_verifying_activates_the_account_and_grants_the_free_trial(self):
        settings_service.set_value("free_trial_tokens", 12_345)
        settings_service.set_value("free_trial_enabled", True)

        self.client.post("/login/", {"identifier": "otptest", "password": PASSWORD})
        code = self._extract_code()
        response = self.client.post("/otp/", {"code": code})

        self.user.refresh_from_db()
        self.assertTrue(self.user.is_verified)
        self.assertEqual(self.user.status, AccountStatus.ACTIVE)
        # One grant, in the default model (GPT-5 Mini); every other model is zero.
        self.assertEqual(token_service.balance_of(self.user), 12_345)
        self.assertEqual(response.status_code, 302)

    def _extract_code(self) -> str:
        """Pull the numeric code out of the most recent OTP e-mail."""
        body = mail.outbox[-1].body
        for token in body.replace("\n", " ").split():
            cleaned = token.strip(".,:")
            if cleaned.isdigit() and len(cleaned) >= 4:
                return cleaned
        raise AssertionError("No OTP code found in the outbox")


class PasswordResetTests(TestCase):
    def setUp(self):
        settings_service.bootstrap()
        self.user = make_student("resettest")

    def test_reset_flow_changes_the_password(self):
        self.client.post("/forgot-password/", {"identifier": "resettest"})
        self.assertEqual(len(mail.outbox), 1)

        code = None
        for token in mail.outbox[-1].body.replace("\n", " ").split():
            cleaned = token.strip(".,:")
            if cleaned.isdigit() and len(cleaned) >= 4:
                code = cleaned
                break

        self.client.post("/otp/", {"code": code})
        response = self.client.post(
            "/reset-password/", {"password": "BrandNewPass!9", "confirm_password": "BrandNewPass!9"}
        )
        self.assertRedirects(response, "/login/", fetch_redirect_response=False)

        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("BrandNewPass!9"))

    def test_unknown_account_does_not_leak_its_existence(self):
        response = self.client.post("/forgot-password/", {"identifier": "nobody-here"}, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(mail.outbox), 0)
