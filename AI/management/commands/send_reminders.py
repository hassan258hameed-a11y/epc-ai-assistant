"""Scheduled maintenance: expiry reminders, low-balance alerts and cleanup.

Run this every hour from cron or Task Scheduler:

    python manage.py send_reminders
"""
from __future__ import annotations

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from AI.models import AccountStatus, OTPCode, RateLimitEvent, Role, TokenAccount, User
from AI.services import (
    notification_service, payment_service, settings_service, token_alert_service, trash_service,
)


class Command(BaseCommand):
    help = "Send expiry reminders and low-balance alerts, then clear expired records."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Report what would happen without sending.")
        parser.add_argument("--skip-cleanup", action="store_true", help="Leave expired records in place.")

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        settings_service.bootstrap()

        if dry_run:
            self.stdout.write(self.style.WARNING("Dry run — nothing will be sent."))
            self._preview()
            return

        announced = notification_service.publish_due()
        if announced:
            self.stdout.write(f"  announced  {announced} scheduled announcement(s)")

        # Orders on gateways that never call back (KHQR) are checked once more,
        # then anything past its window is closed — after a final gateway check.
        settled = payment_service.sweep_pending(limit=200)
        closed = payment_service.expire_stale()
        if settled or closed:
            self.stdout.write(f"  payments   {settled} confirmed, {closed} expired")

        # Chats left in Recently deleted past their 30 days go for good,
        # files included.
        purged = trash_service.purge_expired()
        if purged:
            self.stdout.write(f"  trash      {purged} chat(s) deleted for good")

        # Search by meaning: embed answers written since the last run. Searches
        # index a student's newest answers themselves; this catches the rest.
        try:
            from AI.services import embedding_service

            indexed = embedding_service.index_pending(limit=200)
            if indexed:
                self.stdout.write(f"  search     {indexed} answer(s) indexed for search by meaning")
        except Exception as exc:  # noqa: BLE001 - never stop the reminder run
            self.stdout.write(self.style.WARNING(f"  search     indexing skipped ({type(exc).__name__})"))

        expired = notification_service.expire_subscriptions()
        if expired:
            self.stdout.write(f"  expired    {expired} subscription(s)")

        reminders = notification_service.send_expiry_reminders()
        self.stdout.write(f"  reminders  {reminders['sent']} sent (checked {reminders['checked']})")

        alerts = self._low_balance_alerts()
        self.stdout.write(f"  balance    {alerts} low-token alert(s)")

        # Per-model e-mail warnings. Separate from the in-app alert above
        # because they answer a different question: that one says "you are low",
        # this one says "you are low on GPT-4o" and reaches a student who has
        # stopped opening the app. Both are de-duplicated, so running this
        # command hourly does not mean warning hourly.
        mail = token_alert_service.sweep()
        self.stdout.write(
            f"  e-mail     {mail['sent']} low-token warning(s) sent, "
            f"{mail['failed']} failed (checked {mail['checked']} wallet(s))"
        )

        # The administrators' daily digest. Claimed per admin per local day, so
        # an hourly run sends it once; it never raises.
        from AI.services import digest_service

        digest = digest_service.send_if_due()
        if digest["sent"]:
            self.stdout.write(f"  digest     sent to {digest['sent']} administrator(s)")

        if not options["skip_cleanup"]:
            self.stdout.write(f"  cleanup    {self._cleanup()} stale record(s) removed")

        self.stdout.write(self.style.SUCCESS("Reminder run complete."))

    def _low_balance_alerts(self) -> int:
        threshold = int(settings_service.get("low_token_threshold", 5000))
        accounts = (
            TokenAccount.objects.filter(
                user__role=Role.STUDENT, user__is_active=True, user__status=AccountStatus.ACTIVE,
                balance__lte=threshold,
            )
            .select_related("user")
        )
        sent = 0
        for account in accounts:
            if notification_service.check_low_balance(account.user, account.balance):
                sent += 1
        return sent

    def _cleanup(self) -> int:
        now = timezone.now()
        removed = 0

        # Consumed or long-expired verification codes.
        deleted, _ = OTPCode.objects.filter(expires_at__lt=now - timedelta(days=2)).delete()
        removed += deleted

        # Rate-limit windows that can no longer matter.
        deleted, _ = RateLimitEvent.objects.filter(created_at__lt=now - timedelta(days=1)).delete()
        removed += deleted

        return removed

    def _preview(self):
        now = timezone.now()
        threshold = int(settings_service.get("low_token_threshold", 5000))
        days = settings_service.get("expiry_reminder_days", [7, 3, 1])

        low = TokenAccount.objects.filter(
            user__role=Role.STUDENT, user__is_active=True, balance__lte=threshold
        ).count()
        self.stdout.write(f"  {low} student(s) at or below {threshold:,} tokens")
        at_risk = token_alert_service.at_risk()
        self.stdout.write(
            f"  {len(at_risk)} wallet(s) at or below "
            f"{max(token_alert_service.thresholds())}% of their allocation"
        )
        self.stdout.write(f"  warning points: {token_alert_service.thresholds()}%")
        self.stdout.write(f"  reminder days configured: {days}")
        self.stdout.write(f"  {User.objects.filter(role=Role.STUDENT, is_active=True).count()} active students")
