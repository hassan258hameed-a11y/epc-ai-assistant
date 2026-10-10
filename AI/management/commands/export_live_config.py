"""Write the platform's configuration — no people, no history — to a JSON file.

For starting a live site with a clean database::

    python manage.py export_live_config            # here: writes deploy/live_config.json
    python manage.py import_live_config deploy/live_config.json   # on the server, after migrate

Carried over: AI providers and models (usage counters reset), each model's
token supply, plans and their model grants, the prompt library, saved
replies, promo codes, referral campaigns and every platform setting.
Left behind: students, staff, chats, payments, tokens held, logs and
anything else that belongs to a person or happened in testing.
"""
from __future__ import annotations

import json
from pathlib import Path

from django.conf import settings
from django.core import serializers
from django.core.management.base import BaseCommand

from AI.models import (
    AIModel,
    AIProvider,
    AppSetting,
    CannedReply,
    ModelTokenPool,
    Plan,
    PlanModelGrant,
    PromoCode,
    ReferralCampaign,
    SavedPrompt,
)

#: Settings that describe this machine's Telegram chats, not the platform.
SKIP_SETTINGS = {"telegram_detected_channel", "telegram_detected_group"}

#: A model's running totals start again on the live site.
MODEL_COUNTERS = {
    "tokens_consumed": 0, "input_tokens_total": 0, "output_tokens_total": 0,
    "request_count": 0, "failure_count": 0, "last_used_at": None,
}


def _rows(queryset, *, reset=None, adjust=None) -> list[dict]:
    """Serialise ``queryset`` with every link to a person blanked.

    Nobody from this database exists on the live site, so "created by" and
    "updated by" become empty rather than pointing at a missing account.
    """
    from django.contrib.auth import get_user_model

    user_model = get_user_model()
    people = [f.name for f in queryset.model._meta.get_fields()
              if f.concrete and f.is_relation and f.related_model is user_model]
    rows = json.loads(serializers.serialize("json", queryset))
    for row in rows:
        fields = row["fields"]
        for name in people:
            if name in fields:
                fields[name] = [] if isinstance(fields[name], list) else None
        for name, value in (reset or {}).items():
            if name in fields:
                fields[name] = value
        if adjust:
            adjust(fields)
    return rows


def _pool(fields: dict) -> None:
    # The supply that is left becomes the live site's starting supply: what
    # testing handed out to test accounts is not counted as distributed.
    fields["total_added"] = fields.get("balance", 0)
    fields["total_distributed"] = 0
    fields["total_reclaimed"] = 0


class Command(BaseCommand):
    help = "Export plans, models, prompts and settings (no users or history) for a fresh live database."

    def add_arguments(self, parser):
        parser.add_argument("--output", default=str(Path(settings.BASE_DIR) / "deploy" / "live_config.json"))

    def handle(self, *args, **options):
        payload: list[dict] = []
        payload += _rows(AIProvider.objects.all())
        payload += _rows(AIModel.objects.all(), reset=MODEL_COUNTERS)
        payload += _rows(ModelTokenPool.objects.all(), adjust=_pool)
        payload += _rows(Plan.objects.all())
        payload += _rows(PlanModelGrant.objects.all())
        # The prompt library only: a student's own saved prompts stay behind.
        payload += _rows(SavedPrompt.objects.filter(user__isnull=True), reset={"use_count": 0})
        payload += _rows(CannedReply.objects.all())
        payload += _rows(PromoCode.objects.all(), reset={"times_used": 0, "uses": 0, "redeemed_count": 0})
        payload += _rows(ReferralCampaign.objects.all())
        payload += _rows(AppSetting.objects.exclude(key__in=SKIP_SETTINGS))

        out = Path(options["output"])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, indent=1, default=str), encoding="utf-8")

        counts: dict[str, int] = {}
        for row in payload:
            counts[row["model"]] = counts.get(row["model"], 0) + 1
        for model, count in counts.items():
            self.stdout.write(f"  {model:28} {count}")
        self.stdout.write(self.style.SUCCESS(f"Wrote {len(payload)} rows to {out}"))
