"""Add the current-allocation cycle to token accounts.

Existing accounts are seeded from their lifetime figures so nobody's displayed
balance jumps: the cycle simply starts as "everything so far".
"""
from django.db import migrations, models


def seed_cycles(apps, schema_editor):
    TokenAccount = apps.get_model("AI", "TokenAccount")
    TokenAccount.objects.update(
        cycle_allocated=models.F("total_allocated"),
        cycle_consumed=models.F("total_consumed"),
    )


class Migration(migrations.Migration):
    dependencies = [("AI", "0004_seed_models_and_prompt")]

    operations = [
        migrations.AddField(
            model_name="tokenaccount",
            name="cycle_allocated",
            field=models.BigIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="tokenaccount",
            name="cycle_consumed",
            field=models.BigIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="tokenaccount",
            name="cycle_started_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.RunPython(seed_cycles, migrations.RunPython.noop),
    ]
