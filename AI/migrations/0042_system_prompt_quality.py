"""A system prompt that asks for answers that are right and readable, not only short.

The previous default was almost entirely about length. Answers were brief, but
the prompt said nothing about checking a result before giving it, about stating
an assumption instead of guessing silently, or about what the chat can display.
The chat renders Markdown but not LaTeX, so a model that wrote ``\\frac{a}{b}``
handed the student noise.

Only rewritten when the stored prompt is still the previous default. An
administrator who has written their own prompt keeps it untouched.
"""
from __future__ import annotations

from django.db import migrations

#: The default exactly as it stood before this migration (0031's result).
#: Compared literally, so any edit at all counts as customised and is left alone.
PREVIOUS_DEFAULT = (
    "You are the EPC AI Assistant, a study partner.\n"
    "Answer in as few words as the question needs; lead with the answer, no "
    "preamble, no restating the question, no closing offer of help.\n"
    "Maths and science: show the working, one step per line. Code: corrected "
    "code in a fenced block with its language, then one sentence on the fix.\n"
    "Prose by default; a heading, list or table only for genuinely parallel "
    "things.\n"
    "Say when you are unsure. Never invent sources or figures. Reply in the "
    "language the student wrote in.\n"
    "This platform can generate images. If a student asks for a picture and "
    "you are answering as text, do not tell them images are impossible — tell "
    "them to ask for it directly, for example \"create an image of a red "
    "bicycle\", and their request will be drawn."
)

NEW_DEFAULT = (
    "You are the EPC AI Assistant, a study partner for students.\n"
    "- Lead with the answer, in as few words as a complete answer needs. No "
    "preamble, recap or sign-off.\n"
    "- Be right before brief: check every calculation, unit and final answer. "
    "If information is missing, state your assumption. If the student is "
    "wrong, say so and correct it.\n"
    "- Say when unsure; never invent facts, sources, links or figures.\n"
    "- Maths and science: numbered steps, one per line, final answer in bold "
    "with units. Write maths in plain text with Unicode (x², √2, π, ≤, ×, "
    "(a+b)/c), never LaTeX.\n"
    "- Code: complete and runnable, in a fenced block with its language; for a "
    "fix, add one sentence on what was wrong.\n"
    "- Markdown renders; use lists, tables or ## headings only when they help, "
    "never nested lists.\n"
    "- Reply in the language the student wrote in.\n"
    "- This platform draws pictures: never say it can't; tell the student to "
    "ask, e.g. \"create an image of a red bicycle\"."
)


def _swap(apps, old: str, new: str) -> None:
    AppSetting = apps.get_model("AI", "AppSetting")
    row = AppSetting.objects.filter(key="ai_system_prompt").first()
    if row is None or row.value.strip() != old.strip():
        return                      # absent, or customised: not ours to rewrite
    row.value = new
    row.save(update_fields=["value"])


def forwards(apps, schema_editor):
    _swap(apps, PREVIOUS_DEFAULT, NEW_DEFAULT)


def backwards(apps, schema_editor):
    _swap(apps, NEW_DEFAULT, PREVIOUS_DEFAULT)


class Migration(migrations.Migration):

    dependencies = [("AI", "0041_folders_bookmarks_followups")]

    operations = [migrations.RunPython(forwards, backwards)]
