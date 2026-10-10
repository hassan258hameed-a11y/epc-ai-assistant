"""Give every model default daily/weekly/monthly limits, and apply them.

Before this, only a purchased package carried ceilings. Tokens an
administrator handed out — which is how every student holds Fable, Opus,
GPT-4o, GPT-6 Astra and GPT-5 Sol — had none, so on those models the three
meters never appeared and never moved.

The defaults are the ceilings of the standard 100,000-token package every
model is sold in (9,000 a day, 35,000 a week, 100,000 a month), so a granted
token is limited exactly like a bought one. An administrator can change them
per model, or set one to 0 to lift that window, on the models page.

Existing live allocations that carry no ceilings are given the model's
defaults. Their counters are kept, and each window still rolls from its own
activation instant, so nobody is charged retroactively for a window that has
already ended.
"""
from __future__ import annotations

from django.db import migrations

DAILY, WEEKLY, MONTHLY = 9_000, 35_000, 100_000


def forwards(apps, schema_editor):
    AIModel = apps.get_model("AI", "AIModel")
    PackageAllocation = apps.get_model("AI", "PackageAllocation")

    AIModel.objects.filter(
        default_daily_limit=0, default_weekly_limit=0, default_monthly_limit=0,
    ).update(
        default_daily_limit=DAILY, default_weekly_limit=WEEKLY, default_monthly_limit=MONTHLY,
    )

    for model in AIModel.objects.all():
        PackageAllocation.objects.filter(
            ai_model_id=model.pk, status="active",
            daily_limit=0, weekly_limit=0, monthly_limit=0,
        ).update(
            daily_limit=model.default_daily_limit,
            weekly_limit=model.default_weekly_limit,
            monthly_limit=model.default_monthly_limit,
        )


def backwards(apps, schema_editor):
    """Leave the allocations as they are; the model fields go with 0038."""


class Migration(migrations.Migration):

    dependencies = [("AI", "0038_model_default_limits")]

    operations = [migrations.RunPython(forwards, backwards)]
