"""A starter set of featured prompts, so the library is not empty on day one.

Written for students, since that is who uses this product. Every one of them is
an ordinary row an administrator can edit, hide or delete on the Prompt library
page; nothing reads them by title.
"""
from __future__ import annotations

from django.db import migrations

PROMPTS = [
    ("Explain it simply", "Study",
     "Explain the following concept as if I am meeting it for the first time. "
     "Start with a one-sentence summary, then a simple example, then the common "
     "mistakes people make:\n\n"),
    ("Quiz me", "Study",
     "Write 5 multiple-choice questions to test my understanding of the topic "
     "below. Put the answers, with a one-line explanation each, at the end:\n\n"),
    ("Summarise my notes", "Study",
     "Summarise these notes into clear bullet points grouped by theme, and list "
     "anything that looks unclear or missing:\n\n"),
    ("Check my essay", "Writing",
     "Review this paragraph for clarity, grammar and argument. Show the "
     "corrected version first, then explain the three most important changes:\n\n"),
    ("Debug my code", "Coding",
     "Find the bug in this code. Explain what is wrong, why it happens, and show "
     "the fixed version:\n\n"),
    ("Step-by-step solution", "Maths",
     "Solve this problem step by step, explaining the reasoning at each step so "
     "I can do a similar one on my own:\n\n"),
]


def forwards(apps, schema_editor):
    SavedPrompt = apps.get_model("AI", "SavedPrompt")
    for order, (title, category, body) in enumerate(PROMPTS, start=1):
        SavedPrompt.objects.get_or_create(
            user=None, title=title,
            defaults={
                "category": category, "body": body, "is_featured": True,
                "is_active": True, "sort_order": order * 10,
            },
        )


def backwards(apps, schema_editor):
    SavedPrompt = apps.get_model("AI", "SavedPrompt")
    SavedPrompt.objects.filter(user=None, title__in=[p[0] for p in PROMPTS]).delete()


class Migration(migrations.Migration):

    dependencies = [("AI", "0036_feedback_and_prompt_library")]

    operations = [migrations.RunPython(forwards, backwards)]
