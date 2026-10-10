"""E-mail delivery in development.

With no SMTP configured the verification code is only visible in the server
console, so anything that breaks that output effectively locks a developer out
of their own signup flow.
"""
from __future__ import annotations

import io
import sys

from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.test import SimpleTestCase, TestCase, override_settings

from AI.mail import UnicodeSafeConsoleEmailBackend
from AI.models import AccountStatus, OTPChannel, OTPPurpose, Role, User
from AI.services import otp_service, settings_service

CONSOLE = "AI.mail.UnicodeSafeConsoleEmailBackend"
DJANGO_CONSOLE = "django.core.mail.backends.console.EmailBackend"


def legacy_console() -> io.TextIOWrapper:
    """A stream that behaves like a Windows terminal on code page 1252."""
    return io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict", line_buffering=True)


class ConsoleBackendEncodingTests(SimpleTestCase):
    def _send(self, backend_path: str) -> str:
        message = EmailMultiAlternatives(
            "Your code", "Plain text body", "from@test.local", ["to@test.local"]
        )
        # The real template contains a sparkle glyph in its header.
        message.attach_alternative("<p>Header ✦ code 123456 — enjoy</p>", "text/html")

        stream = legacy_console()
        original = sys.stdout
        sys.stdout = stream
        try:
            with override_settings(EMAIL_BACKEND=backend_path):
                from django.core.mail import get_connection

                connection = get_connection(backend_path)
                connection.send_messages([message])
            stream.flush()
            return stream.buffer.getvalue().decode("cp1252", "replace")
        finally:
            sys.stdout = original

    def test_django_console_backend_fails_on_a_legacy_code_page(self):
        """Documents the failure this backend exists to prevent."""
        with self.assertRaises(UnicodeEncodeError):
            self._send(DJANGO_CONSOLE)

    def test_our_backend_survives_and_still_prints_the_code(self):
        output = self._send(CONSOLE)
        self.assertIn("123456", output)
        self.assertGreater(len(output), 100)

    def test_ascii_content_is_unaffected(self):
        stream = legacy_console()
        backend = UnicodeSafeConsoleEmailBackend(stream=stream)
        message = EmailMultiAlternatives("Subject", "Body 654321", "a@test.local", ["b@test.local"])
        backend.send_messages([message])
        stream.flush()
        self.assertIn("654321", stream.buffer.getvalue().decode("utf-8", "replace"))


class OTPTemplateRenderTests(SimpleTestCase):
    def test_templates_render_and_contain_the_code(self):
        context = {
            "code": "246810",
            "user": User(username="tester", full_name="Test Student"),
            "brand": {"full_name": "EPC AI Assistant", "org": "EPC", "support_email": "s@test.local"},
            "minutes": 10,
            "purpose_label": "verify your account",
            "site_url": "http://127.0.0.1:8000",
        }
        for template in ("emails/otp.txt", "emails/otp.html"):
            body = render_to_string(template, context)
            self.assertIn("246810", body, template)
            self.assertIn("EPC AI Assistant", body, template)


@override_settings(EMAIL_BACKEND=CONSOLE)
class OTPDeliveryTests(TestCase):
    def setUp(self):
        settings_service.bootstrap()
        self.user = User.objects.create_user(
            username="delivery", email="delivery@test.local", password="Sup3rSecret!x",
            role=Role.STUDENT, status=AccountStatus.PENDING,
        )

    def test_issuing_a_code_succeeds_against_a_legacy_console(self):
        stream = legacy_console()
        original = sys.stdout
        sys.stdout = stream
        try:
            otp = otp_service.issue(self.user, OTPPurpose.SIGNUP, channel=OTPChannel.EMAIL)
        finally:
            sys.stdout = original

        # A delivery failure expires the code immediately; a live code proves
        # the send worked.
        self.assertTrue(otp.is_usable)
        self.assertFalse(otp.is_expired)

    @override_settings(DEBUG=True)
    def test_the_code_is_logged_so_it_cannot_be_missed(self):
        with self.assertLogs("AI.otp", level="WARNING") as captured:
            otp_service.issue(self.user, OTPPurpose.SIGNUP, channel=OTPChannel.EMAIL)
        joined = "\n".join(captured.output)
        self.assertIn("Verification code for", joined)
        self.assertIn(self.user.username, joined)

    @override_settings(DEBUG=False, EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    def test_no_code_is_logged_outside_development(self):
        """A production log must never contain a live verification code."""
        import logging

        with self.assertLogs("AI.otp", level="WARNING") as captured:
            logging.getLogger("AI.otp").warning("sentinel so assertLogs has a record")
            otp_service.issue(self.user, OTPPurpose.SIGNUP, channel=OTPChannel.EMAIL)

        joined = "\n".join(captured.output)
        self.assertNotIn("Verification code for", joined)
