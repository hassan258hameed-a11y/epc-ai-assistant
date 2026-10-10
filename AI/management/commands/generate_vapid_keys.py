"""Print a fresh VAPID key pair for browser notifications (Web Push).

    python manage.py generate_vapid_keys
    python manage.py generate_vapid_keys --subject mailto:admin@example.com

Copy the three lines into the server's ``.env`` and restart it. The public key
is the uncompressed P-256 point browsers subscribe with; the private key is
the raw 32-byte scalar pywebpush signs with — both URL-safe base64 without
padding.

Generate a pair once and keep it. Replacing it later silently orphans every
browser that subscribed with the old one; each is re-subscribed the next time
its student opens the app, but until then they receive nothing.
"""
from __future__ import annotations

import base64

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def generate() -> tuple[str, str]:
    """``(public_key, private_key)`` as URL-safe base64."""
    key = ec.generate_private_key(ec.SECP256R1())
    private = key.private_numbers().private_value.to_bytes(32, "big")
    public = key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint,
    )
    return _b64url(public), _b64url(private)


class Command(BaseCommand):
    help = "Print a new VAPID key pair (and subject) for browser notifications, ready for .env."

    def add_arguments(self, parser):
        parser.add_argument(
            "--subject",
            default="",
            help="Contact for push services: mailto:you@example.com or an https:// URL. "
                 "Defaults to VAPID_SUBJECT, else the support e-mail.",
        )

    def handle(self, *args, **options):
        subject = (options["subject"] or getattr(settings, "VAPID_SUBJECT", "") or "").strip()
        if not subject:
            subject = f"mailto:{settings.BRAND.get('support_email') or 'support@example.com'}"
        if not subject.startswith(("mailto:", "https://")):
            if "@" in subject:
                subject = f"mailto:{subject}"
            else:
                raise CommandError("--subject must be mailto:you@example.com or an https:// URL.")

        public, private = generate()
        if getattr(settings, "VAPID_PRIVATE_KEY", ""):
            self.stderr.write(self.style.WARNING(
                "Keys are already configured. Replacing them stops notifications on every subscribed "
                "device until its student opens the app again."
            ))
        self.stdout.write("# Browser notifications (Web Push) - add to .env, then restart the server.")
        self.stdout.write(f"VAPID_PUBLIC_KEY={public}")
        self.stdout.write(f"VAPID_PRIVATE_KEY={private}")
        self.stdout.write(f"VAPID_SUBJECT={subject}")
