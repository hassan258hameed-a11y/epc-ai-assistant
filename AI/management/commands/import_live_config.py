"""Load a file written by ``export_live_config`` into a fresh live database.

Run once on the server, after ``migrate`` and after creating the owner::

    python manage.py import_live_config deploy/live_config.json --owner youradmin

The configuration it carries replaces whatever the empty database seeded for
itself (default models and settings). It refuses to run once real students
exist, so it can never overwrite a live site's plans by accident.
"""
from __future__ import annotations

from pathlib import Path

from django.contrib.auth import get_user_model
from django.core import serializers
from django.core.cache import cache
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

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
    Role,
    SavedPrompt,
)

#: Cleared before loading, children before parents.
REPLACED = (PlanModelGrant, Plan, ModelTokenPool, PromoCode, ReferralCampaign, SavedPrompt, CannedReply,
            AppSetting, AIModel, AIProvider)


class Command(BaseCommand):
    help = "Import plans, models, prompts and settings exported with export_live_config."

    def add_arguments(self, parser):
        parser.add_argument("path")
        parser.add_argument("--owner", help="Username that rows needing an author are credited to "
                                            "(default: the first superuser).")
        parser.add_argument("--force", action="store_true",
                            help="Run even though students already exist (replaces plans and settings).")

    def handle(self, *args, **options):
        path = Path(options["path"])
        if not path.exists():
            raise CommandError(f"No such file: {path}")

        user_model = get_user_model()
        students = user_model.objects.exclude(role=Role.ADMIN).exclude(is_superuser=True).count()
        if students and not options["force"]:
            raise CommandError(f"This database already has {students} student(s). The import is for a fresh "
                               "site; add --force only if you mean to replace its plans and settings.")
        owner = (user_model.objects.filter(username=options["owner"]).first() if options["owner"]
                 else user_model.objects.filter(is_superuser=True).order_by("pk").first())
        if owner is None:
            raise CommandError("Create the owner account first: python manage.py createsuperuser")

        objects = list(serializers.deserialize("json", path.read_text(encoding="utf-8")))
        with transaction.atomic():
            for model in REPLACED:
                model.objects.all().delete()
            counts: dict[str, int] = {}
            for wrapped in objects:
                instance = wrapped.object
                # A link to a person was blanked on export; where one is
                # required, the owner stands in.
                for field in instance._meta.concrete_fields:
                    if field.is_relation and field.related_model is user_model \
                            and getattr(instance, field.attname) is None and not field.null:
                        setattr(instance, field.attname, owner.pk)
                wrapped.save()
                label = instance._meta.label
                counts[label] = counts.get(label, 0) + 1

            # Exactly one default model, as the catalogue expects.
            if not AIModel.objects.filter(is_default=True, is_active=True).exists():
                first = AIModel.objects.filter(is_active=True).order_by("sort_order", "id").first()
                if first:
                    AIModel.objects.filter(pk=first.pk).update(is_default=True)

        cache.clear()  # settings and model status are cached in-process
        for label, count in counts.items():
            self.stdout.write(f"  {label:28} {count}")
        self.stdout.write(self.style.SUCCESS(f"Imported {sum(counts.values())} rows from {path}."))
