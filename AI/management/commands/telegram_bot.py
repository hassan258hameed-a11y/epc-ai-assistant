"""Telegram bot helper.

Two ways to receive updates:

* ``--webhook`` registers ``SITE_URL/webhooks/telegram/`` with Telegram — the
  production route, and the only one that needs no long-running process.
* ``--poll`` long-polls for updates instead, which is what you want on a laptop
  where Telegram cannot reach your machine.

    python manage.py telegram_bot --poll
    python manage.py telegram_bot --webhook
    python manage.py telegram_bot --status
    python manage.py telegram_bot --set-commands

Both ways ask Telegram for the same update types (``telegram_service.
ALLOWED_UPDATES``): messages, and ``my_chat_member`` — how Telegram reports a
student blocking the bot, and the bot being made a channel administrator.
Polling only works while no webhook is registered; ``--delete-webhook`` first.
"""
from __future__ import annotations

import time

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from AI.services import telegram_service
from AI.services.telegram_service import TelegramError


class Command(BaseCommand):
    help = "Register the Telegram webhook, or long-poll for updates in development."

    def add_arguments(self, parser):
        parser.add_argument("--poll", action="store_true", help="Long-poll for updates (development).")
        parser.add_argument("--webhook", action="store_true", help="Register the production webhook.")
        parser.add_argument("--delete-webhook", action="store_true", help="Remove the registered webhook.")
        parser.add_argument("--status", action="store_true", help="Show the bot configuration.")
        parser.add_argument("--set-commands", action="store_true", help="Register the bot's command menu.")

    def handle(self, *args, **options):
        if not telegram_service.is_configured():
            raise CommandError("TELEGRAM_BOT_TOKEN is not set. Add it to your .env and try again.")

        if options["status"]:
            return self._status()
        if options["webhook"]:
            return self._set_webhook()
        if options["delete_webhook"]:
            return self._delete_webhook()
        if options["set_commands"]:
            return self._set_commands()
        if options["poll"]:
            return self._poll()

        self.stdout.write("Choose one of --poll, --webhook, --delete-webhook, --set-commands or --status.")

    # ------------------------------------------------------------------
    def _status(self):
        try:
            me = telegram_service._api("getMe", {})
            info = telegram_service._api("getWebhookInfo", {})
        except TelegramError as exc:
            raise CommandError(str(exc))

        self.stdout.write(self.style.SUCCESS(f"Bot: @{me.get('username')} ({me.get('first_name')})"))
        self.stdout.write(f"  Webhook URL : {info.get('url') or '(none — using long polling)'}")
        self.stdout.write(f"  Pending     : {info.get('pending_update_count', 0)}")
        if info.get("last_error_message"):
            self.stdout.write(self.style.WARNING(f"  Last error  : {info['last_error_message']}"))
        self.stdout.write(f"  Deep links  : https://t.me/{me.get('username')}?start=<token>")

    def _set_commands(self):
        try:
            telegram_service.set_commands()
        except TelegramError as exc:
            raise CommandError(str(exc))
        names = ", ".join(f"/{name}" for name, _ in telegram_service.BOT_COMMANDS)
        self.stdout.write(self.style.SUCCESS(f"Command menu registered: {names}"))

    def _set_webhook(self):
        url = telegram_service.webhook_url()
        if url.startswith("http://"):
            self.stdout.write(self.style.WARNING("Telegram requires HTTPS — this URL will be rejected."))
        if not settings.TELEGRAM_WEBHOOK_SECRET and not settings.DEBUG:
            raise CommandError(
                "Set TELEGRAM_WEBHOOK_SECRET first: with DEBUG off the webhook refuses every call without it."
            )
        try:
            telegram_service.set_webhook(url)
        except TelegramError as exc:
            raise CommandError(str(exc))
        self.stdout.write(self.style.SUCCESS(f"Webhook registered: {url}"))
        if not settings.TELEGRAM_WEBHOOK_SECRET:
            self.stdout.write(self.style.WARNING("Set TELEGRAM_WEBHOOK_SECRET so callbacks can be authenticated."))

    def _delete_webhook(self):
        try:
            telegram_service._api("deleteWebhook", {})
        except TelegramError as exc:
            raise CommandError(str(exc))
        self.stdout.write(self.style.SUCCESS("Webhook removed — long polling can now be used."))

    def _poll(self):
        self.stdout.write(self.style.SUCCESS("Polling for Telegram updates. Press Ctrl+C to stop."))
        offset = None
        while True:
            try:
                updates = telegram_service.get_updates(offset=offset, timeout=25)
                for update in updates:
                    offset = update["update_id"] + 1
                    status = telegram_service.handle_update(update)
                    source = update.get("message") or update.get("my_chat_member") or {}
                    chat = source.get("chat") or {}
                    label = chat.get("username") or chat.get("title") or "-"
                    self.stdout.write(
                        f"  {status:<14} {chat.get('type', '?'):<10} chat={chat.get('id')} ({label})"
                    )
            except TelegramError as exc:
                self.stdout.write(self.style.WARNING(f"  {exc} — retrying in 5s"))
                time.sleep(5)
            except KeyboardInterrupt:
                self.stdout.write("\nStopped.")
                return
            except Exception as exc:  # pragma: no cover
                self.stdout.write(self.style.ERROR(f"  Unexpected error: {exc}"))
                time.sleep(5)
