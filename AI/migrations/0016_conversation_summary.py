"""A rolling summary of the part of a conversation that no longer fits.

Long conversations were re-sending every message on every request, which is
both the largest avoidable cost in the product and, past a point, worse for
quality — the model reads forty turns of context to answer a question about the
last one.  The window is now sized in tokens, and whatever falls out of it is
represented by a short summary kept here instead of being silently forgotten.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [("AI", "0015_per_model_pricing")]

    operations = [
        migrations.AddField(
            model_name="conversation",
            name="summary",
            field=models.TextField(
                blank=True,
                help_text="Rolling summary of the messages that no longer fit the context window.",
            ),
        ),
        migrations.AddField(
            model_name="conversation",
            name="summary_upto_id",
            field=models.BigIntegerField(
                default=0,
                help_text="Id of the newest message the summary covers. Nothing above this is summarised twice.",
            ),
        ),
    ]
