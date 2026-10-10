"""Allow an application setting to hold a write-only secret.

Choices only — no column changes. It exists so the image-generation API key can
be set from the admin console and read back only by server code.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [("AI", "0019_reset_tokens_and_rebuild_packages")]

    operations = [
        migrations.AlterField(
            model_name="appsetting",
            name="value_type",
            field=models.CharField(
                choices=[
                    ("int", "Number"),
                    ("str", "Text"),
                    ("bool", "Toggle"),
                    ("json", "List / JSON"),
                    ("secret", "Secret"),
                ],
                default="str",
                max_length=8,
            ),
        ),
    ]
